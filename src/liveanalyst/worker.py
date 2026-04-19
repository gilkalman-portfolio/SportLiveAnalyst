from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from liveanalyst.api_football import APIFootballClient
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
    normalize_probabilities,
)
from liveanalyst.telegram import TelegramSender


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class PendingFollowUp:
    signal_id: int
    fixture_id: int
    due_at: datetime
    target_second: int


def key_player_from_lineup_player(player: dict) -> bool:
    stats = player.get("statistics", {})
    return (
        player.get("pos") == "G"
        or stats.get("xg_rank", 999) <= 2
        or stats.get("goal_contrib_rank", 999) <= 2
        or (stats.get("minutes_played_pct", 0) > 70 and stats.get("regular_starter", False))
    )


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
    label = {"home": f"{home} Win", "draw": "Draw", "away": f"{away} Win"}[signal.primary_outcome]
    team_label = f" ({cause_team})" if cause_team else ""
    return (
        f"[{tier}] SHIFT | Premier League | {minute}'\\n"
        f"{home} vs {away}\\n\\n"
        f"Cause: {cause}{team_label}\\n"
        f"Outcome: {label}\\n"
        f"P_prev: {signal.p_prev:.2f}\\n"
        f"P_now: {signal.p_now:.2f}\\n"
        f"Delta: {signal.delta_abs * 100:+.1f}%\\n"
        f"Confidence: {signal.confidence:.2f}\\n"
        f"Actionable: {'YES' if signal.actionable else 'NO'}\\n\\n"
        "Status: Calibration phase\\n"
        "Note: Early signal, not accuracy claim."
    )


class LiveAnalystWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.postgres_dsn)
        self.api = APIFootballClient(settings.api_football_base_url, settings.api_football_key)
        self.telegram = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)
        self.follow_ups: list[PendingFollowUp] = []
        self.seen_event_fingerprints: set[str] = set()
        self.last_lineup_poll_at: dict[int, datetime] = {}
        self._standings_refreshed_date: datetime | None = None

    def bootstrap(self) -> None:
        self.db.run_migration("sql/migrations/001_init.sql")
        self.db.run_migration("sql/migrations/002_standings.sql")
        self.db.run_migration("sql/migrations/003_fixtures_round.sql")

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
        logging.info("Standings refreshed: %d teams", len(rows))

    def backfill_motivation(self) -> int:
        signals = self.db.get_signals_without_motivation()
        updated = 0
        # Cache standings per round to avoid redundant API calls
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
            round_num  = fixture_info["round"]
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
            home_mot   = compute_motivation(home_stake, home_s.games_remaining)
            away_mot   = compute_motivation(away_stake, away_s.games_remaining)

            self.db.update_signal_motivation(sig["id"], home_mot, away_mot, home_stake.value, away_stake.value)
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
        cfg_games = 38
        home_s = TeamStanding(home_row["team_id"], home_row["position"], home_row["points"], home_row["games_played"], cfg_games)
        away_s = TeamStanding(away_row["team_id"], away_row["position"], away_row["points"], away_row["games_played"], cfg_games)
        home_stake = classify_stake(home_s, self.settings.league_id)
        away_stake = classify_stake(away_s, self.settings.league_id)
        return (
            compute_motivation(home_stake, home_s.games_remaining),
            compute_motivation(away_stake, away_s.games_remaining),
            home_stake,
            away_stake,
        )

    def run_once(self) -> None:
        fixture = self.api.get_live_premier_league_fixture(self.settings.league_id, self.settings.season)
        if not fixture:
            logging.info("No live Premier League fixture found")
            return

        fixture_id = fixture["fixture"]["id"]
        minute = fixture["fixture"]["status"].get("elapsed") or 0
        home_team_id = fixture["teams"]["home"]["id"]
        away_team_id = fixture["teams"]["away"]["id"]
        home_name = fixture["teams"]["home"]["name"]
        away_name = fixture["teams"]["away"]["name"]

        self._refresh_standings_if_needed()

        round_str = fixture.get("league", {}).get("round", "")
        from liveanalyst.api_football import _parse_round
        round_num = _parse_round(round_str)
        self.db.upsert_fixture_info(
            fixture_id=fixture_id,
            league_id=self.settings.league_id,
            season=self.settings.season,
            round_num=round_num,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

        odds = self.api.get_odds_1x2(fixture_id)
        if not odds:
            logging.info("Missing live odds for fixture_id=%s", fixture_id)
            return

        home_odds, draw_odds, away_odds, source_latency_ms = odds
        now = datetime.now(timezone.utc)
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
        )
        self.db.insert_market_tick(tick)

        lineups = []
        kickoff_ts = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))
        in_lineup_window = kickoff_ts - timedelta(minutes=60) <= now <= kickoff_ts
        last_poll = self.last_lineup_poll_at.get(fixture_id)
        should_poll_lineups = in_lineup_window and (
            last_poll is None or (now - last_poll).total_seconds() >= 30
        )
        if should_poll_lineups:
            lineups = self.api.get_fixture_lineups(fixture_id)
            self.last_lineup_poll_at[fixture_id] = now
        lineup_lookup: dict[str, bool] = {}
        for team in lineups:
            for p in team.get("startXI", []):
                player = p.get("player", {})
                lineup_lookup[player.get("name", "")] = key_player_from_lineup_player(player)

        events = self.api.get_fixture_events(fixture_id)
        for event in events:
            ev_ts = datetime.fromisoformat(event["time"]["elapsed_at"].replace("Z", "+00:00")) if event.get("time", {}).get("elapsed_at") else now
            fingerprint = f"{fixture_id}:{event.get('type')}:{event.get('detail')}:{event.get('team', {}).get('id')}:{event.get('time', {}).get('elapsed')}:{event.get('player', {}).get('name')}"
            if fingerprint in self.seen_event_fingerprints:
                continue
            self.seen_event_fingerprints.add(fingerprint)

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
            }
            self.db.insert_event(event_row)

            last_tick = self.db.last_tick(fixture_id)
            if not last_tick:
                continue

            prev_time = now - timedelta(seconds=5)
            with self.db.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT * FROM market_ticks
                    WHERE fixture_id = %s AND ts <= %s
                    ORDER BY ts DESC
                    LIMIT 1 OFFSET 1
                    """,
                    (fixture_id, now),
                )
                prev_tick = cur.fetchone()
            if not prev_tick:
                continue

            p_prev = Probabilities(home=prev_tick["p_home"], draw=prev_tick["p_draw"], away=prev_tick["p_away"])
            p_now = Probabilities(home=last_tick["p_home"], draw=last_tick["p_draw"], away=last_tick["p_away"])
            delta_abs = compute_delta(p_prev, p_now)
            tier = classify_tier(delta_abs)
            if tier is None:
                continue

            outcome, direction, p_prev_val, p_now_val, _ = pick_primary_outcome(p_prev, p_now)
            c_conf = cause_confidence(cause)

            confidence = 1.0
            if not odds:
                confidence -= 0.25
            if abs((now - ev_ts).total_seconds()) > 10:
                confidence -= 0.20
            if cause == "LINEUP_KEY_PLAYER_OUT" and not is_key:
                confidence -= 0.20
            ticks_for_osc = self.db.recent_ticks_window(fixture_id, now - timedelta(seconds=30), now)
            if len(ticks_for_osc) >= 3:
                swing = abs(ticks_for_osc[-1]["p_home"] - ticks_for_osc[0]["p_home"])
                if swing > 0.02 and abs(ticks_for_osc[-2]["p_home"] - ticks_for_osc[-1]["p_home"]) > 0.01:
                    confidence -= 0.20
            confidence = clamp(confidence)

            blocked = False
            reasons: list[str] = []
            if cause not in {"GOAL", "RED_CARD", "LINEUP_KEY_PLAYER_OUT"}:
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

            if self.db.prior_same_direction_exists(fixture_id, outcome, direction, now):
                blocked = True
                reasons.append("prior_same_direction_move")

            cooldown_key = f"{fixture_id}:{cause}:{direction}"
            if self.db.cooldown_blocked(cooldown_key, now, 300):
                blocked = True
                reasons.append("cooldown_300s")

            home_motivation, away_motivation, home_stake, away_stake = self._get_motivation(home_team_id, away_team_id)

            if home_motivation is not None and away_motivation is not None:
                if home_motivation < 0.25 and away_motivation < 0.25:
                    blocked = True
                    reasons.append("dead_rubber_match")
                if direction == "up":
                    if outcome == "home" and home_motivation < 0.25:
                        confidence -= 0.20
                    elif outcome == "away" and away_motivation < 0.25:
                        confidence -= 0.20
                    if outcome == "home" and home_motivation > 0.85:
                        confidence = clamp(confidence + 0.10)
                    elif outcome == "away" and away_motivation > 0.85:
                        confidence = clamp(confidence + 0.10)
                confidence = clamp(confidence)

            signal = SignalContext(
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
                signal_latency_ms=int((datetime.now(timezone.utc) - ev_ts).total_seconds() * 1000),
                source_latency_ms=source_latency_ms,
                tier=tier,
                home_motivation=home_motivation,
                away_motivation=away_motivation,
                home_stake=home_stake,
                away_stake=away_stake,
            )
            signal_id = self.db.insert_signal(signal)

            if not blocked:
                msg = telegram_message(
                    tier=tier,
                    minute=minute,
                    home=home_name,
                    away=away_name,
                    cause=cause,
                    cause_team=event.get("team", {}).get("name"),
                    signal=signal,
                )
                self.telegram.send(msg)
                self.db.mark_telegram_sent(signal_id)

            for sec in (30, 60, 120):
                self.follow_ups.append(
                    PendingFollowUp(
                        signal_id=signal_id,
                        fixture_id=fixture_id,
                        due_at=now + timedelta(seconds=sec),
                        target_second=sec,
                    )
                )

            early_window = self.db.recent_ticks_window(fixture_id, now - timedelta(seconds=30), now + timedelta(seconds=120))
            early_flag = is_early_signal([TickSnapshot(**t) for t in early_window], int(now.timestamp()))
            logging.info(
                "signal_created id=%s fixture=%s tier=%s delta=%.4f blocked=%s early=%s",
                signal_id,
                fixture_id,
                tier,
                delta_abs,
                blocked,
                early_flag,
            )

    def process_follow_ups(self) -> None:
        now = datetime.now(timezone.utc)
        due = [f for f in self.follow_ups if f.due_at <= now]
        remaining = [f for f in self.follow_ups if f.due_at > now]
        self.follow_ups = remaining

        for item in due:
            with self.db.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("SELECT * FROM signals WHERE id = %s", (item.signal_id,))
                signal = cur.fetchone()
            if not signal:
                continue

            ticks = self.db.get_ticks_after(item.fixture_id, signal["ts_created"], 120)
            ticks_as_obj = [type("Tick", (), t) for t in ticks]
            signal_obj = type("Signal", (), {"direction": signal["direction"], "primary_outcome": signal["primary_outcome"]})
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

            self.db.upsert_signal_outcome(
                signal_id=item.signal_id,
                status=outcome["status"],
                time_to_move=time_to_move,
                max_move=outcome["max_move_within_120s"],
                reversed_flag=outcome["reversed_within_120s"],
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
            time.sleep(5)


def main() -> None:
    settings = Settings.from_env()
    worker = LiveAnalystWorker(settings)
    worker.run_forever()


if __name__ == "__main__":
    main()
