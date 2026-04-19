from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from liveanalyst.api_football import APIFootballClient
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.prematch import _MAX_CONFIDENCE, telegram_prematch_message
from liveanalyst.telegram import TelegramSender

log = logging.getLogger(__name__)

POLL_INTERVAL_S    = 900   # check every 15 minutes
WINDOW_EARLY_MIN   = 75    # start checking 75 min before kickoff
WINDOW_LATE_MIN    = 30    # stop checking 30 min before kickoff

# Base impact per position
_POSITION_IMPACT = {"G": 0.12, "D": 0.06, "M": 0.08, "F": 0.10}
_DEFAULT_IMPACT  = 0.07

# Questionable players count at 40% of a confirmed absence
_DOUBTFUL_FACTOR = 0.40

# Diminishing returns: 1st player = 100%, 2nd = 65%, 3rd = 45%, 4th+ = 25%
_DIMINISH = [1.0, 0.65, 0.45, 0.25]

# Hard cap: no team can lose more than this from absences alone
_MAX_TEAM_PENALTY = 0.22

_MIN_DELTA_TO_SEND = 0.05  # only send correction if confidence shifts ≥5%


def _team_penalty(absent: list[dict]) -> float:
    """Penalty for a team's absences with diminishing returns and a hard cap."""
    # Confirmed absences first, then doubtful
    ordered = sorted(absent, key=lambda p: 0 if p["status"] == "out" else 1)
    total = 0.0
    for i, p in enumerate(ordered):
        base   = _POSITION_IMPACT.get(p["position"], _DEFAULT_IMPACT)
        factor = _DIMINISH[i] if i < len(_DIMINISH) else _DIMINISH[-1]
        if p["status"] == "doubtful":
            base *= _DOUBTFUL_FACTOR
        total += base * factor
    return min(total, _MAX_TEAM_PENALTY)

_LEAGUE_NAMES = {
    39:  "פרמייר ליג 🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    140: "לה ליגה 🇪🇸",
    78:  "בונדסליגה 🇩🇪",
    135: "סרייה א 🇮🇹",
    61:  "ליג 1 🇫🇷",
}


class LineupWorker:
    def __init__(self, settings: Settings, db: Database, api: APIFootballClient, telegram: TelegramSender):
        self.settings = settings
        self.db       = db
        self.api      = api
        self.telegram = telegram
        self._last_run_at: datetime | None = None
        # fixture_ids already checked+sent today
        self._checked: set[int] = set()
        self._checked_date: str = ""
        self._league_injuries: dict[int, list[dict]] = {}  # league_id → injuries list
        self._injuries_date: str = ""

    def _refresh_league_injuries(self, today: str) -> None:
        """Fetch all injuries per league once per day — replaces per-fixture calls."""
        if self._injuries_date == today:
            return
        self._league_injuries = {}
        for league_id in self.settings.league_ids:
            entries = self.api.get_league_injuries(league_id, self.settings.season, today)
            self._league_injuries[league_id] = entries
            log.info("lineup_worker: fetched %d injury entries for league=%s", len(entries), league_id)
        self._injuries_date = today

    def run_once(self, now: datetime) -> None:
        if self._last_run_at and (now - self._last_run_at).total_seconds() < POLL_INTERVAL_S:
            return
        self._last_run_at = now

        today = now.strftime("%Y-%m-%d")
        if today != self._checked_date:
            self._checked.clear()
            self._checked_date = today

        self._refresh_league_injuries(today)

        fixtures = self.api.get_scheduled_fixtures(today, self.settings.league_ids, self.settings.season)

        for fixture in fixtures:
            fixture_id = fixture["fixture"]["id"]
            if fixture_id in self._checked:
                continue

            kickoff = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))
            minutes_to_kickoff = (kickoff - now).total_seconds() / 60

            if not (WINDOW_LATE_MIN <= minutes_to_kickoff <= WINDOW_EARLY_MIN):
                continue

            self._checked.add(fixture_id)
            self._process_fixture(fixture, now)

    def _process_fixture(self, fixture: dict, now: datetime) -> None:
        fixture_id = fixture["fixture"]["id"]
        league_id  = fixture["league"]["id"]
        home_name  = fixture["teams"]["home"]["name"]
        away_name  = fixture["teams"]["away"]["name"]

        # Load stored pre-match prediction (sent this morning)
        stored = self.db.get_prematch_prediction(fixture_id)
        if not stored:
            log.info("lineup_worker: no stored prediction for fixture_id=%s — skipping", fixture_id)
            return

        # Lineups require a per-fixture call; injuries already fetched by league
        lineups  = self.api.get_fixture_lineups(fixture_id)
        injuries = self._league_injuries.get(league_id, [])

        absent_home, absent_away = _classify_absences(lineups, injuries, home_name, away_name)

        if not absent_home and not absent_away:
            log.info("lineup_worker: no absences for fixture_id=%s", fixture_id)
            return

        # Adjust confidence based on absences — diminishing returns, capped
        home_penalty = _team_penalty(absent_home)
        away_penalty = _team_penalty(absent_away)

        orig_outcome     = stored["recommended_outcome"]
        orig_confidence  = stored["confidence"]

        # Recalculate: absences hurt the team that was favoured
        adj_home = stored["p_home_composite"] - home_penalty
        adj_away = stored["p_away_composite"] - away_penalty
        adj_draw = stored["p_draw_composite"]

        # Re-normalise
        total = max(adj_home, 0.01) + max(adj_draw, 0.01) + max(adj_away, 0.01)
        adj_home = max(adj_home, 0.01) / total
        adj_draw = max(adj_draw, 0.01) / total
        adj_away = max(adj_away, 0.01) / total

        new_outcome, new_confidence = max(
            [("home", adj_home), ("draw", adj_draw), ("away", adj_away)],
            key=lambda x: x[1],
        )
        new_confidence = min(new_confidence, _MAX_CONFIDENCE)

        delta = abs(new_confidence - orig_confidence)
        if delta < _MIN_DELTA_TO_SEND and new_outcome == orig_outcome:
            log.info(
                "lineup_worker: delta=%.2f%% too small for fixture_id=%s — skipping",
                delta * 100, fixture_id,
            )
            return

        msg = _build_correction_message(
            league_id=league_id,
            home=home_name,
            away=away_name,
            kickoff=datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00")),
            absent_home=absent_home,
            absent_away=absent_away,
            orig_outcome=orig_outcome,
            orig_confidence=orig_confidence,
            new_outcome=new_outcome,
            new_confidence=new_confidence,
        )
        self.telegram.send(msg)
        self.db.mark_lineup_check_sent(fixture_id)
        log.info(
            "lineup_worker: sent correction fixture_id=%s %s→%s conf %.0f%%→%.0f%%",
            fixture_id, orig_outcome, new_outcome,
            orig_confidence * 100, new_confidence * 100,
        )


# ------------------------------------------------------------------ helpers

def _classify_absences(
    lineups: list[dict],
    injuries: list[dict],
    home_name: str,
    away_name: str,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (absent_home, absent_away) — each a list of
    {"name": str, "position": str, "reason": str}.
    """
    # Build set of players in starting XI + subs per team
    available: dict[str, set[str]] = {home_name: set(), away_name: set()}
    for team_lineup in lineups:
        tname = team_lineup.get("team", {}).get("name", "")
        if tname not in available:
            continue
        for p in team_lineup.get("startXI", []):
            available[tname].add(p.get("player", {}).get("name", ""))
        for p in team_lineup.get("substitutes", []):
            available[tname].add(p.get("player", {}).get("name", ""))

    absent_home: list[dict] = []
    absent_away: list[dict] = []
    seen: set[tuple[str, str]] = set()  # deduplicate (team, name) — API returns each player twice

    for entry in injuries:
        player  = entry.get("player", {})
        team    = entry.get("team", {}).get("name", "")
        name    = player.get("name", "")
        reason  = player.get("reason", "")
        ptype   = player.get("type", "")  # "Missing Fixture" | "Questionable"

        if ptype not in ("Missing Fixture", "Questionable"):
            continue

        key = (team, name)
        if key in seen:
            continue
        seen.add(key)

        # Only flag players NOT in the available squad
        if team in available and name in available[team]:
            continue

        status = "out" if ptype == "Missing Fixture" else "doubtful"
        record = {
            "name":     name,
            "position": "M",  # position not available in league-level injuries
            "reason":   reason or ptype,
            "status":   status,
        }
        if team == home_name:
            absent_home.append(record)
        elif team == away_name:
            absent_away.append(record)

    return absent_home, absent_away


def _pos_code(pos_str: str) -> str:
    pos = pos_str.lower()
    if "goalkeeper" in pos:  return "G"
    if "defender"   in pos:  return "D"
    if "midfielder" in pos:  return "M"
    if "forward"    in pos or "attacker" in pos: return "F"
    return "M"


_STATUS_EMOJI = {"out": "🚑", "suspended": "🟥", "doubtful": "⚠️"}
_OUTCOME_HE   = {"home": lambda h, a: f"ניצחון {h}", "draw": lambda h, a: "תיקו", "away": lambda h, a: f"ניצחון {a}"}


def _build_correction_message(
    league_id: int,
    home: str,
    away: str,
    kickoff: datetime,
    absent_home: list[dict],
    absent_away: list[dict],
    orig_outcome: str,
    orig_confidence: float,
    new_outcome: str,
    new_confidence: float,
) -> str:
    league_name  = _LEAGUE_NAMES.get(league_id, "ליגה לא ידועה")
    kickoff_str  = kickoff.strftime("%H:%M")
    changed      = new_outcome != orig_outcome

    lines = [
        f"🔄 עדכון טרום משחק | {league_name}",
        f"{home} נגד {away} — {kickoff_str}",
        "",
        "⚠️ שינויים משמעותיים:",
    ]

    for p in absent_home:
        emoji = _STATUS_EMOJI.get(p["status"], "⚠️")
        lines.append(f"  {emoji} {home}: {p['name']} ({p['reason']})")
    for p in absent_away:
        emoji = _STATUS_EMOJI.get(p["status"], "⚠️")
        lines.append(f"  {emoji} {away}: {p['name']} ({p['reason']})")

    lines.append("")
    if changed:
        orig_label = _OUTCOME_HE[orig_outcome](home, away)
        new_label  = _OUTCOME_HE[new_outcome](home, away)
        lines.append(f"📊 תחזית מעודכנת: {new_label} (היה: {orig_label})")
    else:
        label = _OUTCOME_HE[new_outcome](home, away)
        lines.append(f"📊 תחזית: {label} (ללא שינוי בכיוון)")

    lines.append(f"ביטחון: {new_confidence:.0%} (היה: {orig_confidence:.0%})")
    lines.append("")
    lines.append("⚠️ לצורכי מחקר בלבד — לא המלצה פיננסית.")

    return "\n".join(lines)
