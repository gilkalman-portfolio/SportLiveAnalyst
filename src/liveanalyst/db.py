from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from liveanalyst.domain import MarketTick, SignalContext


class Database:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn)
        self.conn.autocommit = True

    def close(self) -> None:
        self.conn.close()

    def run_migration(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        with self.conn.cursor() as cur:
            cur.execute(sql)

    def insert_market_tick(self, tick: MarketTick) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market_ticks (fixture_id, ts, minute, home_odds, draw_odds, away_odds, p_home, p_draw, p_away, source_latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tick.fixture_id,
                    tick.ts,
                    tick.minute,
                    tick.home_odds,
                    tick.draw_odds,
                    tick.away_odds,
                    tick.p_home,
                    tick.p_draw,
                    tick.p_away,
                    tick.source_latency_ms,
                ),
            )
            return cur.fetchone()[0]

    def insert_event(self, event: dict[str, Any]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (fixture_id, ts, minute, event_type, team_side, player_name, is_key_player, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    event["fixture_id"],
                    event["ts"],
                    event.get("minute"),
                    event["event_type"],
                    event.get("team_side"),
                    event.get("player_name"),
                    event.get("is_key_player", False),
                    psycopg.types.json.Jsonb(event["raw_payload"]),
                ),
            )
            row = cur.fetchone()
            return row[0] if row else -1

    def last_tick(self, fixture_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM market_ticks
                WHERE fixture_id = %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (fixture_id,),
            )
            return cur.fetchone()

    def recent_ticks_window(self, fixture_id: int, from_ts: datetime, to_ts: datetime) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT fixture_id, EXTRACT(EPOCH FROM ts)::int AS ts, p_home, p_draw, p_away
                FROM market_ticks
                WHERE fixture_id = %s AND ts BETWEEN %s AND %s
                ORDER BY ts ASC
                """,
                (fixture_id, from_ts, to_ts),
            )
            return list(cur.fetchall())

    def prior_same_direction_exists(self, fixture_id: int, primary_outcome: str, direction: str, ts_created: datetime) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM signals
                WHERE fixture_id = %s
                  AND primary_outcome = %s
                  AND direction = %s
                  AND ts_created < %s
                LIMIT 1
                """,
                (fixture_id, primary_outcome, direction, ts_created),
            )
            return cur.fetchone() is not None

    def cooldown_blocked(self, cooldown_key: str, ts_created: datetime, seconds: int = 300) -> bool:
        cutoff = ts_created - timedelta(seconds=seconds)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM signals
                WHERE cooldown_key = %s
                  AND ts_created >= %s
                LIMIT 1
                """,
                (cooldown_key, cutoff),
            )
            return cur.fetchone() is not None

    def insert_signal(self, signal: SignalContext) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (
                    fixture_id, ts_created, minute, signal_type, tier, primary_outcome, direction,
                    p_prev, p_now, delta_abs, cause_type, cause_confidence, confidence,
                    actionable, blocked, block_reason, telegram_sent, cooldown_key,
                    event_ts, signal_latency_ms, source_latency_ms
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING id
                """,
                (
                    signal.fixture_id,
                    signal.ts_created,
                    signal.minute,
                    signal.signal_type,
                    signal.tier,
                    signal.primary_outcome,
                    signal.direction,
                    signal.p_prev,
                    signal.p_now,
                    signal.delta_abs,
                    signal.cause_type,
                    signal.cause_confidence,
                    signal.confidence,
                    signal.actionable,
                    signal.blocked,
                    signal.block_reason,
                    False,
                    signal.cooldown_key,
                    signal.event_ts,
                    signal.signal_latency_ms,
                    signal.source_latency_ms,
                ),
            )
            return cur.fetchone()[0]

    def mark_telegram_sent(self, signal_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE signals SET telegram_sent = TRUE WHERE id = %s", (signal_id,))

    def get_ticks_after(self, fixture_id: int, ts_created: datetime, seconds: int = 120) -> list[dict[str, Any]]:
        end_ts = ts_created + timedelta(seconds=seconds)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT ts, p_home, p_draw, p_away
                FROM market_ticks
                WHERE fixture_id = %s AND ts > %s AND ts <= %s
                ORDER BY ts ASC
                """,
                (fixture_id, ts_created, end_ts),
            )
            return list(cur.fetchall())

    def upsert_signal_outcome(self, signal_id: int, status: str, time_to_move: int | None, max_move: float, reversed_flag: bool) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_outcomes (signal_id, status, time_to_move, max_move_within_120s, reversed_within_120s)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO UPDATE
                SET status = EXCLUDED.status,
                    time_to_move = EXCLUDED.time_to_move,
                    max_move_within_120s = EXCLUDED.max_move_within_120s,
                    reversed_within_120s = EXCLUDED.reversed_within_120s
                """,
                (signal_id, status, time_to_move, max_move, reversed_flag),
            )

    def get_standings_for_teams(self, home_team_id: int, away_team_id: int, league_id: int, season: int) -> list[dict[str, Any]]:
        """Return [home_row, away_row] standings for the given teams, if available."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT team_id, position, points, games_played
                FROM team_standings
                WHERE team_id = ANY(%s) AND league_id = %s AND season = %s
                """,
                ([home_team_id, away_team_id], league_id, season),
            )
            rows = {r["team_id"]: r for r in cur.fetchall()}
            result = []
            for tid in (home_team_id, away_team_id):
                if tid in rows:
                    result.append(rows[tid])
            return result

    def upsert_team_standing(self, team_id: int, league_id: int, season: int, position: int, points: int, games_played: int) -> None:
        """Upsert live/current standings snapshot (round=0)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO team_standings (team_id, league_id, season, round, position, points, games_played, fetched_at)
                VALUES (%s, %s, %s, 0, %s, %s, %s, NOW())
                ON CONFLICT (team_id, league_id, season, round) DO UPDATE
                SET position = EXCLUDED.position,
                    points = EXCLUDED.points,
                    games_played = EXCLUDED.games_played,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (team_id, league_id, season, position, points, games_played),
            )

    def upsert_team_standing_for_round(self, team_id: int, league_id: int, season: int, round_num: int, position: int, points: int, games_played: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO team_standings (team_id, league_id, season, round, position, points, games_played, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (team_id, league_id, season, round) DO UPDATE
                SET position = EXCLUDED.position,
                    points = EXCLUDED.points,
                    games_played = EXCLUDED.games_played,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (team_id, league_id, season, round_num, position, points, games_played),
            )

    def upsert_fixture_info(self, fixture_id: int, league_id: int, season: int, round_num: int, home_team_id: int, away_team_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fixtures (fixture_id, league_id, season, round, home_team_id, away_team_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id) DO NOTHING
                """,
                (fixture_id, league_id, season, round_num, home_team_id, away_team_id),
            )

    def get_fixture_info(self, fixture_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM fixtures WHERE fixture_id = %s", (fixture_id,))
            return cur.fetchone()

    def get_standings_for_round(self, home_team_id: int, away_team_id: int, league_id: int, season: int, round_num: int) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT team_id, position, points, games_played
                FROM team_standings
                WHERE team_id = ANY(%s) AND league_id = %s AND season = %s AND round = %s
                """,
                ([home_team_id, away_team_id], league_id, season, round_num),
            )
            rows = {r["team_id"]: r for r in cur.fetchall()}
            return [rows[tid] for tid in (home_team_id, away_team_id) if tid in rows]

    def get_signals_without_motivation(self) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT id, fixture_id FROM signals WHERE home_motivation IS NULL ORDER BY ts_created"
            )
            return list(cur.fetchall())

    def update_signal_motivation(self, signal_id: int, home_motivation: float, away_motivation: float, home_stake: str, away_stake: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signals
                SET home_motivation = %s, away_motivation = %s,
                    home_stake = %s, away_stake = %s
                WHERE id = %s
                """,
                (home_motivation, away_motivation, home_stake, away_stake, signal_id),
            )
