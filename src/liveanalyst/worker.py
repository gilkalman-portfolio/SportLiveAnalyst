from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from liveanalyst.api_football import APIFootballClient, _parse_round, RATE_LIMIT_ALERT_THRESHOLD
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.domain import MarketTick, Probabilities, SeasonStake, SignalContext, TeamStanding
from liveanalyst.logic import (
    TickSnapshot,
    cause_confidence,
    classify_stake,
    classify_tier,
    clamp,
    compute_delta,
    compute_motivation,
    evaluate_signal_outcome,
    is_early_signal,
    key_player_from_lineup_player,
    normalize_probabilities,
)
from liveanalyst.telegram import TelegramSender


# Polling intervals (seconds)
FIXTURES_POLL_INTERVAL_S  = 60   # refresh fixtures list once per minute (events embedded in response)
FIXTURES_IDLE_BASE_S      = 300  # first idle back-off: 5 min
FIXTURES_IDLE_MAX_S       = 3600 # max idle back-off: 1 hour (exponential, doubles each empty poll)
QUIET_ODDS_INTERVAL_S     = 60   # odds in quiet mode
ALERT_ODDS_INTERVAL_S     = 15   # odds right after an event
ALERT_DURATION_S          = 120  # how long alert mode lasts after an event
LINEUP_POLL_INTERVAL_S    = 900  # lineups update every 15 min per API docs
MAIN_LOOP_SLEEP_S         = 5    # main loop heartbeat
EVENT_RETRY_TIMEOUT_S     = 120  # max time to retry an event whose odds hadn't updated yet
STALE_EVENT_THRESHOLD_S   = 300  # skip signals for events >5 min old at first sight (worker restart artifact)
ODDS_SIGNAL_BASELINE_MIN  = 3    # compare current odds to tick from 3 minutes ago
ODDS_SIGNAL_COOLDOWN_S    = 600  # 10-min cooldown between odds-driven signals per direction



HEALTH_CHECK_PORT = 8765  # simple /health HTTP endpoint


class _HealthState:
    """Shared mutable state for the health-check HTTP handler."""
    last_poll_at: datetime | None = None


def _make_health_handler(state: _HealthState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({
                    "status": "ok",
                    "last_poll": state.last_poll_at.isoformat() if state.last_poll_at else None,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_):
            pass  # suppress default HTTP access logs

    return Handler


def _start_health_server(state: _HealthState, port: int = HEALTH_CHECK_PORT) -> None:
    try:
        server = HTTPServer(("", port), _make_health_handler(state))
        logging.info("health_check listening on :%d", port)
        server.serve_forever()
    except OSError:
        logging.warning("health_check: port %d in use — skipping", port)


@dataclass
class PendingFollowUp:
    signal_id: int
    fixture_id: int
    due_at: datetime
    target_second: int


def event_to_cause(event: dict, lineup_lookup: dict[str, bool]) -> tuple[str, bool]:
    typ = (event.get("type") or "").upper()
    detail = (event.get("detail") or "").upper()
    player_name = event.get("player", {}).get("name")

    if typ == "GOAL":
        return "GOAL", True
    if typ == "CARD" and "RED" in detail:
        return "RED_CARD", True
    if typ == "LINEUP":
        is_key = lineup_lookup.get(player_name or "", False)
        return "LINEUP_KEY_PLAYER_OUT", is_key
    return typ or "UNKNOWN", False


def pick_primary_outcome(p_prev: Probabilities, p_now: Probabilities) -> tuple[str, str, float, float, float]:
    diffs = {
        "home": p_now.home - p_prev.home,
        "draw": p_now.draw - p_prev.draw,
        "away": p_now.away - p_prev.away,
    }
    outcome, signed = max(diffs.items(), key=lambda item: abs(item[1]))
    direction = "up" if signed >= 0 else "down"
    prev_val = getattr(p_prev, outcome)
    now_val = getattr(p_now, outcome)
    return outcome, direction, prev_val, now_val, abs(signed)


def telegram_message(tier: str, minute: int, home: str, away: str, cause: str, cause_team: str | None, signal: SignalContext) -> str:
    _LEAGUE_NAMES = {
        39: "פרמייר ליג 🏴󠁧󠁢󠁥󠁮󠁧󠁿",
        140: "לה ליגה 🇪🇸",
        78: "בונדסליגה 🇩🇪",
        135: "סרייה א 🇮🇹",
        61: "ליג 1 🇫🇷",
    }
    _CAUSE_NAMES = {
        "GOAL": "⚽ גול",
        "RED_CARD": "🟥 כרטיס אדום",
        "LINEUP_KEY_PLAYER_OUT": "🚑 שחקן מפתח יוצא",
        "ODDS_MOVE": "📊 תנועת שוק",
    }
    _TIER_EMOJI = {"LOW": "🟡", "MEDIUM": "🟠", "HIGH": "🔴"}

    league_name = _LEAGUE_NAMES.get(signal.league_id, "ליגה לא ידועה")
    label = {"home": f"ניצחון {home}", "draw": "תיקו", "away": f"ניצחון {away}"}[signal.primary_outcome]
    direction_arrow = "⬆️" if signal.direction == "up" else "⬇️"
    cause_text = _CAUSE_NAMES.get(cause, cause)
    team_label = f" ({cause_team})" if cause_team else ""
    tier_emoji = _TIER_EMOJI.get(tier, "⚪")

    return (
        f"{tier_emoji} סיגנל {tier} | {league_name} | דקה {minute}'\n"
        f"{home} נגד {away}\n\n"
        f"סיבה: {cause_text}{team_label}\n"
        f"תוצאה צפויה: {label} {direction_arrow}\n"
        f"הסתברות קודמת: {signal.p_prev:.0%}\n"
        f"הסתברות כעת: {signal.p_now:.0%}\n"
        f"שינוי: {signal.delta_abs * 100:+.1f}%\n"
        f"ביטחון: {signal.confidence:.0%}\n\n"
        f"⚠️ שלב כיול — סיגנל מוקדם, לא המלצה סופית."
    )


def detect_signal(
    fixture_id: int,
    minute: int,
    ev_ts: datetime,
    cause: str,
    is_key: bool,
    p_prev: Probabilities,
    p_now: Probabilities,
    source_latency_ms: int,
    now: datetime,
    oscillation_ticks: list,
    prior_exists: bool,
    cooldown_hit: bool,
) -> SignalContext | None:
    """Pure signal detection — no DB or API calls.

    Caller is responsible for:
      - fetching oscillation_ticks (recent 30s tick window)
      - computing prior_exists via db.prior_same_direction_exists()
      - computing cooldown_hit via db.cooldown_blocked()

    Returns None if the probability delta is below the lowest tier threshold.
    """
    delta_abs = compute_delta(p_prev, p_now)
    tier = classify_tier(delta_abs)
    if tier is None:
        return None

    outcome, direction, p_prev_val, p_now_val, _ = pick_primary_outcome(p_prev, p_now)
    c_conf = cause_confidence(cause)
    cooldown_key = f"{fixture_id}:{cause}:{direction}"

    confidence = 1.0
    if source_latency_ms > 30_000:
        confidence -= 0.25
    if cause == "ODDS_MOVE":
        confidence -= 0.10  # unknown cause — could be goal, card, or drift
    elif abs((now - ev_ts).total_seconds()) > 10:
        confidence -= 0.20
    if cause == "LINEUP_KEY_PLAYER_OUT" and not is_key:
        confidence -= 0.20
    if len(oscillation_ticks) >= 3:
        swing = abs(oscillation_ticks[-1]["p_home"] - oscillation_ticks[0]["p_home"])
        if swing > 0.02 and abs(oscillation_ticks[-2]["p_home"] - oscillation_ticks[-1]["p_home"]) > 0.01:
            confidence -= 0.20
    confidence = clamp(confidence)

    blocked = False
    reasons: list[str] = []
    if cause not in {"GOAL", "RED_CARD", "LINEUP_KEY_PLAYER_OUT", "ODDS_MOVE"}:
        blocked = True
        reasons.append("unsupported_cause")
    if cause == "LINEUP_KEY_PLAYER_OUT" and not is_key:
        blocked = True
        reasons.append("lineup_player_not_key")
    if minute >= 88:
        blocked = True
        reasons.append("minute_gte_88")
    if confidence < 0.6:
        blocked = True
        reasons.append("confidence_lt_0.6")
    if prior_exists:
        blocked = True
        reasons.append("prior_same_direction_move")
    if cooldown_hit:
        blocked = True
        reasons.append("cooldown_300s")

    return SignalContext(
        fixture_id=fixture_id,
        ts_created=now,
        minute=minute,
        primary_outcome=outcome,
        direction=direction,
        p_prev=p_prev_val,
        p_now=p_now_val,
        delta_abs=delta_abs,
        cause_type=cause,
        cause_confidence=c_conf,
        confidence=confidence,
        actionable=not blocked,
        blocked=blocked,
        block_reason=",".join(reasons) if reasons else None,
        cooldown_key=cooldown_key,
        event_ts=ev_ts,
        signal_latency_ms=int((now - ev_ts).total_seconds() * 1000),
        source_latency_ms=source_latency_ms,
        tier=tier,
    )


class LiveAnalystWorker:
    def __init__(
        self,
        settings: Settings,
        db: "Database | None" = None,
        api: "APIFootballClient | None" = None,
        telegram: "TelegramSender | None" = None,
    ):
        self.settings = settings
        self.db       = db       or Database(settings.postgres_dsn)
        self.api      = api      or APIFootballClient(settings.api_football_base_url, settings.api_football_key)
        self.telegram = telegram or TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)
        self.follow_ups: list[PendingFollowUp] = []
        # fingerprint → first-seen time. Only moved to seen_event_fingerprints once a signal fires.
        # If no signal after EVENT_RETRY_TIMEOUT_S we give up and mark as done anyway.
        self.seen_event_fingerprints: set[str] = set()
        self.pending_event_fingerprints: dict[str, datetime] = {}
        self.last_lineup_poll_at: dict[int, datetime] = {}
        # Adaptive polling state
        self.last_fixtures_poll_at: datetime | None = None
        self.cached_fixtures: list = []
        self.last_odds_poll_at: dict[int, datetime] = {}
        self.alert_mode_until: dict[int, datetime] = {}
        self._consecutive_idle_polls: int = 0  # counts back-to-back empty fixtures polls
        self._health_state = _HealthState()
        self._rate_limit_alerted: bool = False  # send Telegram alert at most once per day
        self._standings_refreshed_date: datetime | None = None

    def bootstrap(self) -> None:
        for migration in [
            "sql/migrations/001_init.sql",
            "sql/migrations/002_add_event_to_odds_ms.sql",
            "sql/migrations/003_add_league_id.sql",
            "sql/migrations/004_add_bets_log.sql",
            "sql/migrations/005_add_is_early_signal.sql",
            "sql/migrations/009_standings.sql",
            "sql/migrations/010_fixtures_round.sql",
        ]:
            self.db.run_migration(migration)
        self.backfill_motivation()
        self._recover_outcomes()
        t = threading.Thread(
            target=_start_health_server,
            args=(self._health_state,),
            daemon=True,
        )
        t.start()

    def _recover_outcomes(self) -> None:
        """On startup: fill signal_outcomes for actionable signals that lost their follow_ups (worker restart)."""
        unresolved = self.db.get_unresolved_signals(min_age_seconds=120)
        if not unresolved:
            return
        logging.info("recovery: found %d unresolved signals — backfilling outcomes", len(unresolved))
        for signal in unresolved:
            signal_id   = signal["id"]
            fixture_id  = signal["fixture_id"]
            ts_created  = signal["ts_created"]
            event_ts    = signal.get("event_ts")

            ticks = self.db.get_ticks_after(fixture_id, ts_created, 120)
            ticks_as_obj = [SimpleNamespace(**t) for t in ticks]
            signal_obj   = SimpleNamespace(
                direction=signal["direction"],
                primary_outcome=signal["primary_outcome"],
            )
            outcome = evaluate_signal_outcome(signal_obj, ticks_as_obj)

            time_to_move = None
            if len(ticks) > 1:
                base_v = ticks[0][f"p_{signal['primary_outcome']}"]
                for t in ticks[1:]:
                    now_v = t[f"p_{signal['primary_outcome']}"]
                    signed = now_v - base_v if signal["direction"] == "up" else base_v - now_v
                    if signed >= 0.02:
                        time_to_move = int((t["ts"] - ticks[0]["ts"]).total_seconds())
                        break

            event_to_odds_ms = None
            if event_ts is not None:
                pre_tick = self.db.get_tick_before(fixture_id, event_ts)
                event_ticks = self.db.get_ticks_after(fixture_id, event_ts, 120)
                if pre_tick and event_ticks:
                    base_ev_v = pre_tick[f"p_{signal['primary_outcome']}"]
                    for t in event_ticks:
                        now_v = t[f"p_{signal['primary_outcome']}"]
                        signed = now_v - base_ev_v if signal["direction"] == "up" else base_ev_v - now_v
                        if signed >= 0.02:
                            event_to_odds_ms = int((t["ts"] - event_ts).total_seconds() * 1000)
                            break

            early_window = self.db.recent_ticks_window(
                fixture_id,
                ts_created - timedelta(seconds=30),
                ts_created + timedelta(seconds=120),
            )
            early_flag = is_early_signal(
                [TickSnapshot(ts=t["ts"], p_home=t["p_home"], p_draw=t["p_draw"], p_away=t["p_away"]) for t in early_window],
                int(ts_created.timestamp()),
            )

            self.db.upsert_signal_outcome(
                signal_id=signal_id,
                status=outcome["status"],
                time_to_move=time_to_move,
                max_move=outcome["max_move_within_120s"],
                reversed_flag=outcome["reversed_within_120s"],
                event_to_odds_ms=event_to_odds_ms,
                is_early_signal=early_flag,
            )
            logging.info(
                "recovery: signal_id=%s status=%s max_move=%.4f event_to_odds_ms=%s early=%s",
                signal_id, outcome["status"], outcome["max_move_within_120s"], event_to_odds_ms, early_flag,
            )

    def _refresh_standings_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._standings_refreshed_date == today:
            return
        rows = self.api.get_standings(self.settings.league_id, self.settings.season)
        for entry in rows:
            self.db.upsert_team_standing(
                team_id=entry["team"]["id"],
                league_id=self.settings.league_id,
                season=self.settings.season,
                position=entry["rank"],
                points=entry["points"],
                games_played=entry["all"]["played"],
            )
        self._standings_refreshed_date = today
        logging.info("standings_refreshed teams=%d", len(rows))

    def backfill_motivation(self) -> int:
        signals = self.db.get_signals_without_motivation()
        if not signals:
            return 0
        updated = 0
        standings_cache: dict[tuple, bool] = {}
        for sig in signals:
            fixture_info = self.db.get_fixture_info(sig["fixture_id"])
            if not fixture_info:
                raw = self.api.get_fixture_info(sig["fixture_id"])
                if not raw:
                    continue
                self.db.upsert_fixture_info(**raw)
                fixture_info = self.db.get_fixture_info(sig["fixture_id"])
            if not fixture_info:
                continue
            league_id  = fixture_info["league_id"]
            season     = fixture_info["season"]
            round_num  = fixture_info["round_num"]
            home_id    = fixture_info["home_team_id"]
            away_id    = fixture_info["away_team_id"]
            cache_key = (league_id, season, round_num)
            if cache_key not in standings_cache:
                rows = self.api.get_standings_by_round(league_id, season, round_num)
                for entry in rows:
                    self.db.upsert_team_standing_for_round(
                        team_id=entry["team"]["id"],
                        league_id=league_id,
                        season=season,
                        round_num=round_num,
                        position=entry["rank"],
                        points=entry["points"],
                        games_played=entry["all"]["played"],
                    )
                standings_cache[cache_key] = True
            standings = self.db.get_standings_for_round(home_id, away_id, league_id, season, round_num)
            if len(standings) < 2:
                continue
            home_row, away_row = standings[0], standings[1]
            home_s = TeamStanding(home_row["team_id"], home_row["position"], home_row["points"], home_row["games_played"], 38)
            away_s = TeamStanding(away_row["team_id"], away_row["position"], away_row["points"], away_row["games_played"], 38)
            home_stake = classify_stake(home_s, league_id)
            away_stake = classify_stake(away_s, league_id)
            self.db.update_signal_motivation(
                sig["id"],
                compute_motivation(home_stake, home_s.games_remaining),
                compute_motivation(away_stake, away_s.games_remaining),
                home_stake.value,
                away_stake.value,
            )
            updated += 1
        logging.info("backfill_motivation: updated %d signals", updated)
        return updated

    def _get_motivation(
        self, home_team_id: int, away_team_id: int
    ) -> tuple[float | None, float | None, SeasonStake | None, SeasonStake | None]:
        rows = self.db.get_standings_for_teams(home_team_id, away_team_id, self.settings.league_id, self.settings.season)
        if len(rows) < 2:
            return None, None, None, None
        home_row, away_row = rows[0], rows[1]
        home_s = TeamStanding(home_row["team_id"], home_row["position"], home_row["points"], home_row["games_played"], 38)
        away_s = TeamStanding(away_row["team_id"], away_row["position"], away_row["points"], away_row["games_played"], 38)
        home_stake = classify_stake(home_s, self.settings.league_id)
        away_stake = classify_stake(away_s, self.settings.league_id)
        return (
            compute_motivation(home_stake, home_s.games_remaining),
            compute_motivation(away_stake, away_s.games_remaining),
            home_stake,
            away_stake,
        )

    def _check_odds_driven_signal(
        self,
        fixture: dict,
        now: datetime,
        p_now: Probabilities,
        source_latency_ms: int,
    ) -> None:
        """Fire a signal based purely on odds movement vs 3-min-ago baseline — no event required."""
        fixture_id = fixture["fixture"]["id"]
        league_id  = fixture["league"]["id"]
        minute     = fixture["fixture"]["status"].get("elapsed") or 0
        home_name  = fixture["teams"]["home"]["name"]
        away_name  = fixture["teams"]["away"]["name"]

        cutoff = now - timedelta(minutes=ODDS_SIGNAL_BASELINE_MIN)
        baseline = self.db.get_tick_minutes_ago(fixture_id, cutoff)
        if not baseline:
            return

        p_baseline = Probabilities(
            home=baseline["p_home"], draw=baseline["p_draw"], away=baseline["p_away"]
        )

        outcome_pre, direction_pre, _, _, _ = pick_primary_outcome(p_baseline, p_now)
        cooldown_key_pre = f"{fixture_id}:ODDS_MOVE:{direction_pre}"
        cooldown_hit = self.db.cooldown_blocked(cooldown_key_pre, now, ODDS_SIGNAL_COOLDOWN_S)

        oscillation_ticks = self.db.recent_ticks_window(fixture_id, now - timedelta(seconds=30), now)

        signal = detect_signal(
            fixture_id=fixture_id,
            minute=minute,
            ev_ts=now,
            cause="ODDS_MOVE",
            is_key=False,
            p_prev=p_baseline,
            p_now=p_now,
            source_latency_ms=source_latency_ms,
            now=now,
            oscillation_ticks=oscillation_ticks,
            prior_exists=False,
            cooldown_hit=cooldown_hit,
        )
        if signal is None:
            return

        signal.league_id = league_id
        signal_id = self.db.insert_signal(signal)

        if not signal.blocked:
            msg = telegram_message(
                tier=signal.tier,
                minute=minute,
                home=home_name,
                away=away_name,
                cause="ODDS_MOVE",
                cause_team=None,
                signal=signal,
            )
            self.telegram.send(msg)
            self.db.mark_telegram_sent(signal_id)
            logging.info("telegram_sent signal_id=%s fixture_id=%s (odds_driven)", signal_id, fixture_id)

            for sec in (30, 60, 120):
                self.follow_ups.append(PendingFollowUp(
                    signal_id=signal_id,
                    fixture_id=fixture_id,
                    due_at=now + timedelta(seconds=sec),
                    target_second=sec,
                ))

        logging.info(
            "odds_signal id=%s fixture=%s tier=%s delta=%.4f blocked=%s reason=%s",
            signal_id, fixture_id, signal.tier, signal.delta_abs,
            signal.blocked, signal.block_reason,
        )

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        # Refresh fixtures list with exponential backoff when idle:
        #   0 live matches → 5 min, 10 min, 20 min, ... up to 1 hour per poll.
        #   Live matches found → back to normal 60-second interval.
        idle = not self.cached_fixtures
        if idle:
            idle_interval = min(
                FIXTURES_IDLE_BASE_S * (2 ** self._consecutive_idle_polls),
                FIXTURES_IDLE_MAX_S,
            )
            interval = idle_interval
        else:
            interval = FIXTURES_POLL_INTERVAL_S
        if self.last_fixtures_poll_at is None or (now - self.last_fixtures_poll_at).total_seconds() >= interval:
            self.cached_fixtures = self.api.get_live_fixtures(self.settings.league_ids)
            self.last_fixtures_poll_at = now
            if self.cached_fixtures:
                self._consecutive_idle_polls = 0
                logging.info("fixtures_refreshed count=%d leagues=%s", len(self.cached_fixtures), self.settings.league_ids)
            else:
                self._consecutive_idle_polls += 1
                next_check = min(FIXTURES_IDLE_BASE_S * (2 ** self._consecutive_idle_polls), FIXTURES_IDLE_MAX_S)
                logging.info("fixtures_refreshed count=0 — idle poll #%d, next check in %ds", self._consecutive_idle_polls, next_check)

        self._health_state.last_poll_at = now

        # Telegram alert when daily API quota runs low
        remaining = self.api.rate_limit_remaining
        if remaining is not None and remaining < RATE_LIMIT_ALERT_THRESHOLD and not self._rate_limit_alerted:
            self.telegram.send(
                f"⚠️ LiveAnalyst: מכסת API נמוכה — נותרו {remaining} בקשות להיום.\n"
                "Worker ממשיך, אך ייתכן שחלק מה-polling יכשל."
            )
            logging.warning("rate_limit_low remaining=%d", remaining)
            self._rate_limit_alerted = True
        # Reset alert flag when quota refills (new day)
        if remaining is not None and remaining >= RATE_LIMIT_ALERT_THRESHOLD:
            self._rate_limit_alerted = False

        if not self.cached_fixtures:
            return
        for fixture in self.cached_fixtures:
            self._process_fixture(fixture)

    def _process_fixture(self, fixture: dict) -> None:
        fixture_id = fixture["fixture"]["id"]
        league_id  = fixture["league"]["id"]
        status_short = fixture["fixture"]["status"].get("short", "")
        minute = fixture["fixture"]["status"].get("elapsed") or 0
        home_team_id = fixture["teams"]["home"]["id"]
        away_team_id = fixture["teams"]["away"]["id"]
        home_name = fixture["teams"]["home"]["name"]
        away_name = fixture["teams"]["away"]["name"]

        self._refresh_standings_if_needed()
        round_str = fixture.get("league", {}).get("round", "")
        round_num = _parse_round(round_str)
        self.db.upsert_fixture_info(
            fixture_id=fixture_id,
            league_id=self.settings.league_id,
            season=self.settings.season,
            round_num=round_num,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

        if status_short in ("HT", "BT"):
            logging.info("skipping_break fixture_id=%s status=%s", fixture_id, status_short)
            return

        now = datetime.now(timezone.utc)
        in_alert = self.alert_mode_until.get(fixture_id, datetime.min.replace(tzinfo=timezone.utc)) > now
        odds_interval = ALERT_ODDS_INTERVAL_S if in_alert else QUIET_ODDS_INTERVAL_S
        mode = "ALERT" if in_alert else "QUIET"

        # Odds — only poll if interval elapsed
        should_poll_odds = (
            fixture_id not in self.last_odds_poll_at
            or (now - self.last_odds_poll_at[fixture_id]).total_seconds() >= odds_interval
        )
        if not should_poll_odds:
            return

        logging.info("processing_fixture fixture_id=%s minute=%s mode=%s", fixture_id, minute, mode)

        odds = self.api.get_odds_1x2(fixture_id)
        self.last_odds_poll_at[fixture_id] = now  # always update — prevents retry spam on missing odds
        if not odds:
            logging.info("Missing live odds for fixture_id=%s (%s vs %s)", fixture_id, home_name, away_name)
            return
        home_odds, draw_odds, away_odds, source_latency_ms = odds
        probs = normalize_probabilities(home_odds, draw_odds, away_odds)
        tick = MarketTick(
            fixture_id=fixture_id,
            ts=now,
            minute=minute,
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            p_home=probs.home,
            p_draw=probs.draw,
            p_away=probs.away,
            source_latency_ms=source_latency_ms,
            league_id=league_id,
        )
        self.db.insert_market_tick(tick)
        self._check_odds_driven_signal(fixture, now, probs, source_latency_ms)

        lineups = []
        kickoff_ts = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))
        in_lineup_window = kickoff_ts - timedelta(minutes=60) <= now <= kickoff_ts
        last_poll = self.last_lineup_poll_at.get(fixture_id)
        should_poll_lineups = in_lineup_window and (
            last_poll is None or (now - last_poll).total_seconds() >= LINEUP_POLL_INTERVAL_S
        )
        if should_poll_lineups:
            lineups = self.api.get_fixture_lineups(fixture_id)
            self.last_lineup_poll_at[fixture_id] = now
            if fixture_id not in self._prematch_predicted:
                pred = self._prematch_engine.predict(fixture_id)
                if pred:
                    self._prematch_predicted.add(fixture_id)
                    logging.info(
                        "prematch fixture=%s outcome=%s p_home=%.3f p_draw=%.3f p_away=%.3f "
                        "confidence=%.2f actionable=%s home_stake=%s away_stake=%s",
                        fixture_id, pred.predicted_outcome,
                        pred.p_home, pred.p_draw, pred.p_away,
                        pred.confidence, pred.actionable,
                        pred.home_stake, pred.away_stake,
                    )
        lineup_lookup: dict[str, bool] = {}
        for team in lineups:
            for p in team.get("startXI", []):
                player = p.get("player", {})
                lineup_lookup[player.get("name", "")] = key_player_from_lineup_player(player)

        # Events are embedded in the live fixtures response — no extra API call needed
        events = self.api.extract_events_from_fixture(fixture)
        for event in events:
            elapsed_min = (event.get("time") or {}).get("elapsed")
            if (event.get("time") or {}).get("elapsed_at"):
                ev_ts = datetime.fromisoformat(event["time"]["elapsed_at"].replace("Z", "+00:00"))
            elif elapsed_min is not None:
                ev_ts = kickoff_ts + timedelta(minutes=elapsed_min)  # ±30s approximation
            else:
                ev_ts = now
            fingerprint = f"{fixture_id}:{event.get('type')}:{event.get('detail')}:{event.get('team', {}).get('id')}:{event.get('time', {}).get('elapsed')}:{event.get('player', {}).get('name')}"
            if fingerprint in self.seen_event_fingerprints:
                continue

            cause, is_key = event_to_cause(event, lineup_lookup)
            event_row = {
                "fixture_id": fixture_id,
                "ts": ev_ts,
                "minute": event.get("time", {}).get("elapsed") or minute,
                "event_type": cause,
                "team_side": "home" if event.get("team", {}).get("name") == home_name else "away",
                "player_name": event.get("player", {}).get("name"),
                "is_key_player": is_key,
                "raw_payload": event,
                "league_id": league_id,
            }
            self.db.insert_event(event_row)

            # Stale event guard: if the event is very old when we first see it (e.g. worker
            # restarted mid-game), record it to DB but skip signal to avoid latency artifacts.
            first_seen = self.pending_event_fingerprints.get(fingerprint)
            event_age_s = (now - ev_ts).total_seconds()
            if first_seen is None and event_age_s > STALE_EVENT_THRESHOLD_S:
                self.seen_event_fingerprints.add(fingerprint)
                logging.info("stale_event_skipped fingerprint=%s age_s=%.0f", fingerprint, event_age_s)
                continue

            # If we saw this event before but odds hadn't moved yet — retry until timeout
            if first_seen is None:
                self.pending_event_fingerprints[fingerprint] = now
                # New event → switch to alert mode for 2 minutes
                self.alert_mode_until[fixture_id] = now + timedelta(seconds=ALERT_DURATION_S)
                logging.info("alert_mode_activated fixture_id=%s until=%s", fixture_id, self.alert_mode_until[fixture_id])

            last_tick = self.db.last_tick(fixture_id)
            if not last_tick:
                continue

            prev_tick = self.db.prev_tick(fixture_id)
            if not prev_tick:
                continue

            p_prev = Probabilities(home=prev_tick["p_home"], draw=prev_tick["p_draw"], away=prev_tick["p_away"])
            p_now_probs = Probabilities(home=last_tick["p_home"], draw=last_tick["p_draw"], away=last_tick["p_away"])

            # Pre-compute outcome/direction so DB-dependent block checks can run
            # before detect_signal() (which also computes them internally — pure).
            outcome_pre, direction_pre, _, _, _ = pick_primary_outcome(p_prev, p_now_probs)
            cooldown_key_pre = f"{fixture_id}:{cause}:{direction_pre}"

            oscillation_ticks = self.db.recent_ticks_window(fixture_id, now - timedelta(seconds=30), now)
            prior_exists = self.db.prior_same_direction_exists(fixture_id, outcome_pre, direction_pre, now)
            cooldown_hit = self.db.cooldown_blocked(cooldown_key_pre, now, 300)

            signal = detect_signal(
                fixture_id=fixture_id,
                minute=minute,
                ev_ts=ev_ts,
                cause=cause,
                is_key=is_key,
                p_prev=p_prev,
                p_now=p_now_probs,
                source_latency_ms=source_latency_ms,
                now=now,
                oscillation_ticks=oscillation_ticks,
                prior_exists=prior_exists,
                cooldown_hit=cooldown_hit,
            )
            if signal is None:
                # Odds haven't moved yet — keep in pending so we retry next poll
                elapsed_since_seen = (now - self.pending_event_fingerprints.get(fingerprint, now)).total_seconds()
                if elapsed_since_seen >= EVENT_RETRY_TIMEOUT_S:
                    logging.info("event_retry_timeout fingerprint=%s — giving up after %ds", fingerprint, int(elapsed_since_seen))
                    self.seen_event_fingerprints.add(fingerprint)
                    self.pending_event_fingerprints.pop(fingerprint, None)
                else:
                    logging.debug("event_pending fingerprint=%s elapsed=%.0fs — will retry", fingerprint, elapsed_since_seen)
                continue
            # Signal generated → mark fingerprint as fully processed
            self.seen_event_fingerprints.add(fingerprint)
            self.pending_event_fingerprints.pop(fingerprint, None)
            signal.league_id = league_id

            # Motivation post-processing
            home_mot, away_mot, h_stake, a_stake = self._get_motivation(home_team_id, away_team_id)
            signal.home_motivation = home_mot
            signal.away_motivation = away_mot
            signal.home_stake = h_stake
            signal.away_stake = a_stake
            if home_mot is not None and away_mot is not None:
                if home_mot < 0.25 and away_mot < 0.25:
                    signal.blocked = True
                    signal.block_reason = (signal.block_reason + ",dead_rubber_match") if signal.block_reason else "dead_rubber_match"
                    signal.actionable = False
                elif signal.direction == "up":
                    outcome_m = signal.primary_outcome
                    if outcome_m == "home" and home_mot < 0.25:
                        signal.confidence = clamp(signal.confidence - 0.20)
                    elif outcome_m == "away" and away_mot < 0.25:
                        signal.confidence = clamp(signal.confidence - 0.20)
                    if outcome_m == "home" and home_mot > 0.85:
                        signal.confidence = clamp(signal.confidence + 0.10)
                    elif outcome_m == "away" and away_mot > 0.85:
                        signal.confidence = clamp(signal.confidence + 0.10)
                    if signal.confidence < 0.6 and not signal.blocked:
                        signal.blocked = True
                        signal.block_reason = (signal.block_reason + ",confidence_lt_0.6") if signal.block_reason else "confidence_lt_0.6"
                        signal.actionable = False

            signal_id = self.db.insert_signal(signal)

            if not signal.blocked:
                msg = telegram_message(
                    tier=signal.tier,
                    minute=minute,
                    home=home_name,
                    away=away_name,
                    cause=cause,
                    cause_team=event.get("team", {}).get("name"),
                    signal=signal,
                )
                self.telegram.send(msg)
                self.db.mark_telegram_sent(signal_id)
                logging.info("telegram_sent signal_id=%s fixture_id=%s", signal_id, fixture_id)

                for sec in (30, 60, 120):
                    self.follow_ups.append(
                        PendingFollowUp(
                            signal_id=signal_id,
                            fixture_id=fixture_id,
                            due_at=now + timedelta(seconds=sec),
                            target_second=sec,
                        )
                    )
            else:
                logging.info(
                    "signal_blocked signal_id=%s fixture_id=%s reason=%s",
                    signal_id, fixture_id, signal.block_reason,
                )

            logging.info(
                "signal_created id=%s fixture=%s tier=%s delta=%.4f blocked=%s",
                signal_id,
                fixture_id,
                signal.tier,
                signal.delta_abs,
                signal.blocked,
            )

    def process_follow_ups(self) -> None:
        now = datetime.now(timezone.utc)
        due = [f for f in self.follow_ups if f.due_at <= now]
        remaining = [f for f in self.follow_ups if f.due_at > now]
        self.follow_ups = remaining

        for item in due:
            signal = self.db.get_signal(item.signal_id)
            if not signal:
                continue

            ticks = self.db.get_ticks_after(item.fixture_id, signal["ts_created"], 120)
            ticks_as_obj = [SimpleNamespace(**t) for t in ticks]
            signal_obj = SimpleNamespace(direction=signal["direction"], primary_outcome=signal["primary_outcome"])
            outcome = evaluate_signal_outcome(signal_obj, ticks_as_obj)

            time_to_move = None
            if ticks and len(ticks) > 1:
                base = ticks[0]
                base_v = base[f"p_{signal['primary_outcome']}"]
                for t in ticks[1:]:
                    now_v = t[f"p_{signal['primary_outcome']}"]
                    signed = now_v - base_v if signal["direction"] == "up" else base_v - now_v
                    if signed >= 0.02:
                        time_to_move = int((t["ts"] - base["ts"]).total_seconds())
                        break

            event_to_odds_ms = None
            event_ts = signal.get("event_ts")
            if event_ts is not None:
                pre_tick = self.db.get_tick_before(item.fixture_id, event_ts)
                event_ticks = self.db.get_ticks_after(item.fixture_id, event_ts, 120)
                if pre_tick and event_ticks:
                    base_ev_v = pre_tick[f"p_{signal['primary_outcome']}"]
                    for t in event_ticks:
                        now_v = t[f"p_{signal['primary_outcome']}"]
                        signed = now_v - base_ev_v if signal["direction"] == "up" else base_ev_v - now_v
                        if signed >= 0.02:
                            event_to_odds_ms = int((t["ts"] - event_ts).total_seconds() * 1000)
                            break

            early_flag = None
            if item.target_second == 120:
                ts_created = signal["ts_created"]
                early_window = self.db.recent_ticks_window(
                    item.fixture_id,
                    ts_created - timedelta(seconds=30),
                    ts_created + timedelta(seconds=120),
                )
                early_flag = is_early_signal(
                    [TickSnapshot(ts=t["ts"], p_home=t["p_home"], p_draw=t["p_draw"], p_away=t["p_away"]) for t in early_window],
                    int(ts_created.timestamp()),
                )
                logging.info("early_signal_eval signal_id=%s early=%s", item.signal_id, early_flag)

            self.db.upsert_signal_outcome(
                signal_id=item.signal_id,
                status=outcome["status"],
                time_to_move=time_to_move,
                max_move=outcome["max_move_within_120s"],
                reversed_flag=outcome["reversed_within_120s"],
                event_to_odds_ms=event_to_odds_ms,
                is_early_signal=early_flag,
            )

            logging.info(
                "followup signal_id=%s checkpoint=%ss status=%s max_move=%.4f",
                item.signal_id,
                item.target_second,
                outcome["status"],
                outcome["max_move_within_120s"],
            )

    def run_forever(self) -> None:
        self.bootstrap()
        while True:
            try:
                self.run_once()
                self.process_follow_ups()
            except Exception as exc:  # deterministic worker should keep running
                logging.exception("worker_error: %s", exc)
            time.sleep(MAIN_LOOP_SLEEP_S)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/worker.log", encoding="utf-8"),
        ],
    )
    settings = Settings.from_env()
    worker = LiveAnalystWorker(settings)
    worker.run_forever()


if __name__ == "__main__":
    main()
