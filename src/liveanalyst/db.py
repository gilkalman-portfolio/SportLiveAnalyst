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
                INSERT INTO market_ticks (fixture_id, ts, minute, home_odds, draw_odds, away_odds, p_home, p_draw, p_away, source_latency_ms, league_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    tick.league_id,
                ),
            )
            return cur.fetchone()[0]

    def insert_event(self, event: dict[str, Any]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (fixture_id, ts, minute, event_type, team_side, player_name, is_key_player, raw_payload, league_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id, minute, event_type, COALESCE(team_side, ''), COALESCE(player_name, '')) DO NOTHING
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
                    event.get("league_id"),
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

    def prev_tick(self, fixture_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM market_ticks
                WHERE fixture_id = %s
                ORDER BY ts DESC
                LIMIT 1 OFFSET 1
                """,
                (fixture_id,),
            )
            return cur.fetchone()

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM signals WHERE id = %s", (signal_id,))
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
                    event_ts, signal_latency_ms, source_latency_ms, league_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                    signal.league_id,
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

    def upsert_signal_outcome(
        self,
        signal_id: int,
        status: str,
        time_to_move: int | None,
        max_move: float,
        reversed_flag: bool,
        event_to_odds_ms: int | None = None,
        is_early_signal: bool | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_outcomes
                    (signal_id, status, time_to_move, max_move_within_120s, reversed_within_120s, event_to_odds_ms, is_early_signal)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO UPDATE
                SET status               = EXCLUDED.status,
                    time_to_move         = EXCLUDED.time_to_move,
                    max_move_within_120s = EXCLUDED.max_move_within_120s,
                    reversed_within_120s = EXCLUDED.reversed_within_120s,
                    event_to_odds_ms     = EXCLUDED.event_to_odds_ms,
                    is_early_signal      = COALESCE(EXCLUDED.is_early_signal, signal_outcomes.is_early_signal)
                """,
                (signal_id, status, time_to_move, max_move, reversed_flag, event_to_odds_ms, is_early_signal),
            )

<<<<<<< Updated upstream
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
=======
    def get_tick_minutes_ago(self, fixture_id: int, cutoff: datetime) -> dict[str, Any] | None:
        """Tick closest to (but not after) cutoff — used as baseline for odds-driven signals."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT ts, p_home, p_draw, p_away
                FROM market_ticks
                WHERE fixture_id = %s AND ts <= %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (fixture_id, cutoff),
            )
            return cur.fetchone()

    def get_tick_before(self, fixture_id: int, ts: datetime) -> dict[str, Any] | None:
        """Last tick strictly before ts — used as baseline for event_to_odds_ms."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT ts, p_home, p_draw, p_away
                FROM market_ticks
                WHERE fixture_id = %s AND ts < %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (fixture_id, ts),
            )
            return cur.fetchone()

    def get_unresolved_signals(self, min_age_seconds: int = 120) -> list[dict[str, Any]]:
        """Actionable signals older than min_age_seconds with no signal_outcome — for recovery on startup."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT s.id, s.fixture_id, s.ts_created, s.primary_outcome,
                       s.direction, s.event_ts
                FROM signals s
                LEFT JOIN signal_outcomes so ON so.signal_id = s.id
                WHERE s.actionable = TRUE
                  AND s.ts_created < %s
                  AND so.signal_id IS NULL
                ORDER BY s.ts_created ASC
                """,
                (cutoff,),
            )
            return list(cur.fetchall())

    def get_score_from_events(self, fixture_id: int) -> dict[str, Any]:
        """Reconstruct score timeline from stored goal events.

        Own goals in api-football are attributed to the BENEFITING team,
        so team_side already points to the correct scoring side for all goal types.
        """
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT minute, team_side, player_name,
                       raw_payload->>'detail' AS detail
                FROM events
                WHERE fixture_id = %s AND event_type = 'GOAL'
                ORDER BY minute, id
                """,
                (fixture_id,),
            )
            goals = cur.fetchall()

        score = {"home": 0, "away": 0, "timeline": []}
        for g in goals:
            score[g["team_side"]] += 1
            score["timeline"].append({
                "minute":     g["minute"],
                "player":     g["player_name"],
                "side":       g["team_side"],
                "detail":     g["detail"],
                "score_home": score["home"],
                "score_away": score["away"],
            })
        return score

    # --------------------------------------------------------- prematch

    def upsert_prematch_prediction(self, pred) -> int:
        """Insert or update a pre-match prediction. Returns the row id."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prematch_predictions (
                    fixture_id, league_id, kickoff,
                    home_team, away_team, home_team_id, away_team_id,
                    home_odds, draw_odds, away_odds,
                    p_home_implied, p_draw_implied, p_away_implied,
                    p_home_composite, p_draw_composite, p_away_composite,
                    recommended_outcome, confidence,
                    form_home, form_away, h2h_home_rate,
                    form_home_str, form_away_str,
                    under_over, form_home_home, form_home_away,
                    form_away_home, form_away_away,
                    mu_home, mu_away, p_home_dc, p_draw_dc, p_away_dc,
                    standings_gap, injury_penalty_home, injury_penalty_away
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (fixture_id) DO UPDATE SET
                    league_id              = EXCLUDED.league_id,
                    kickoff                = EXCLUDED.kickoff,
                    home_odds              = EXCLUDED.home_odds,
                    draw_odds              = EXCLUDED.draw_odds,
                    away_odds              = EXCLUDED.away_odds,
                    p_home_implied         = EXCLUDED.p_home_implied,
                    p_draw_implied         = EXCLUDED.p_draw_implied,
                    p_away_implied         = EXCLUDED.p_away_implied,
                    p_home_composite       = EXCLUDED.p_home_composite,
                    p_draw_composite       = EXCLUDED.p_draw_composite,
                    p_away_composite       = EXCLUDED.p_away_composite,
                    recommended_outcome    = EXCLUDED.recommended_outcome,
                    confidence             = EXCLUDED.confidence,
                    form_home              = EXCLUDED.form_home,
                    form_away              = EXCLUDED.form_away,
                    h2h_home_rate          = EXCLUDED.h2h_home_rate,
                    form_home_str          = EXCLUDED.form_home_str,
                    form_away_str          = EXCLUDED.form_away_str,
                    under_over             = EXCLUDED.under_over,
                    form_home_home         = EXCLUDED.form_home_home,
                    form_home_away         = EXCLUDED.form_home_away,
                    form_away_home         = EXCLUDED.form_away_home,
                    form_away_away         = EXCLUDED.form_away_away,
                    mu_home                = EXCLUDED.mu_home,
                    mu_away                = EXCLUDED.mu_away,
                    p_home_dc              = EXCLUDED.p_home_dc,
                    p_draw_dc              = EXCLUDED.p_draw_dc,
                    p_away_dc              = EXCLUDED.p_away_dc,
                    standings_gap          = EXCLUDED.standings_gap,
                    injury_penalty_home    = EXCLUDED.injury_penalty_home,
                    injury_penalty_away    = EXCLUDED.injury_penalty_away
                RETURNING id
                """,
                (
                    pred.fixture_id, pred.league_id, pred.kickoff,
                    pred.home_team, pred.away_team, pred.home_team_id, pred.away_team_id,
                    pred.home_odds, pred.draw_odds, pred.away_odds,
                    pred.p_home_implied, pred.p_draw_implied, pred.p_away_implied,
                    pred.p_home_composite, pred.p_draw_composite, pred.p_away_composite,
                    pred.recommended_outcome, pred.confidence,
                    pred.form_home, pred.form_away, pred.h2h_home_rate,
                    pred.form_home_str, pred.form_away_str,
                    pred.under_over, pred.form_home_home, pred.form_home_away,
                    pred.form_away_home, pred.form_away_away,
                    pred.mu_home, pred.mu_away, pred.p_home_dc, pred.p_draw_dc, pred.p_away_dc,
                    pred.standings_gap, pred.injury_penalty_home, pred.injury_penalty_away,
                ),
            )
            return cur.fetchone()[0]

    def mark_prematch_telegram_sent(self, prediction_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE prematch_predictions SET telegram_sent = TRUE WHERE id = %s", (prediction_id,))

    def get_prematch_prediction(self, fixture_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM prematch_predictions WHERE fixture_id = %s", (fixture_id,))
            return cur.fetchone()

    def mark_lineup_check_sent(self, fixture_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE prematch_predictions SET lineup_check_sent = TRUE WHERE fixture_id = %s",
                (fixture_id,),
            )

    # ------------------------------------------------------------------ backtest

    def record_prediction_outcome(
        self,
        fixture_id: int,
        prediction_id: int | None,
        home_goals: int,
        away_goals: int,
        predicted_outcome: str,
        predicted_confidence: float,
    ) -> None:
        """Store the final match result against the pre-match prediction. Computes Brier score."""
        if home_goals > away_goals:
            actual = "home"
        elif home_goals == away_goals:
            actual = "draw"
        else:
            actual = "away"

        # Brier score = (1 - p_correct)^2 where p_correct is the predicted confidence
        # for the actual outcome (not necessarily the recommended one).
        # We store based on whether recommended matched actual.
        p_correct = predicted_confidence if predicted_outcome == actual else (1 - predicted_confidence)
        brier = (1 - p_correct) ** 2

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prediction_outcomes (
                    fixture_id, prediction_id,
                    actual_home_goals, actual_away_goals, actual_outcome,
                    predicted_outcome, predicted_confidence, brier_score
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id) DO UPDATE SET
                    actual_home_goals    = EXCLUDED.actual_home_goals,
                    actual_away_goals    = EXCLUDED.actual_away_goals,
                    actual_outcome       = EXCLUDED.actual_outcome,
                    brier_score          = EXCLUDED.brier_score,
                    settled_at           = NOW()
                """,
                (
                    fixture_id, prediction_id,
                    home_goals, away_goals, actual,
                    predicted_outcome, predicted_confidence, brier,
                ),
            )

    def get_weekly_brier_score(self) -> dict[str, Any]:
        """Compute accuracy stats for the last 7 days of settled predictions."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                                            AS n,
                    ROUND(AVG(brier_score)::numeric, 4)                AS avg_brier,
                    COUNT(*) FILTER (WHERE predicted_outcome = actual_outcome) AS correct,
                    ROUND(
                        100.0 * COUNT(*) FILTER (WHERE predicted_outcome = actual_outcome) / NULLIF(COUNT(*), 0),
                        1
                    )                                                   AS accuracy_pct
                FROM prediction_outcomes
                WHERE settled_at >= %s
                """,
                (cutoff,),
            )
            return dict(cur.fetchone() or {})

    def get_unsettled_predictions(self, hours_after_kickoff: int = 3) -> list[dict[str, Any]]:
        """Pre-match predictions whose kickoff has passed but no outcome recorded yet."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_after_kickoff)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT pp.id AS prediction_id, pp.fixture_id,
                       pp.recommended_outcome, pp.confidence
                FROM prematch_predictions pp
                LEFT JOIN prediction_outcomes po ON po.fixture_id = pp.fixture_id
                WHERE pp.kickoff <= %s
                  AND po.fixture_id IS NULL
                ORDER BY pp.kickoff ASC
                """,
                (cutoff,),
            )
            return list(cur.fetchall())

    # ------------------------------------------------------------------ replay

    def get_all_ticks_for_fixture(self, fixture_id: int) -> list[dict[str, Any]]:
        """All market ticks for a fixture ordered by ts ASC. Used by replay."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT id, fixture_id, ts, minute, home_odds, draw_odds, away_odds,
                       p_home, p_draw, p_away, source_latency_ms
                FROM market_ticks
                WHERE fixture_id = %s
                ORDER BY ts ASC
                """,
                (fixture_id,),
            )
            return list(cur.fetchall())

    def get_all_events_for_fixture(self, fixture_id: int) -> list[dict[str, Any]]:
        """All events for a fixture ordered by ts ASC. Used by replay."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT id, fixture_id, ts, minute, event_type, team_side,
                       player_name, is_key_player
                FROM events
                WHERE fixture_id = %s
                ORDER BY ts ASC
                """,
                (fixture_id,),
            )
            return list(cur.fetchall())

    def signal_exists_for_key(
        self, fixture_id: int, minute: int, cause_type: str, primary_outcome: str
    ) -> bool:
        """Idempotency check for replay: true if a signal with this key already exists."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM signals
                WHERE fixture_id = %s
                  AND minute = %s
                  AND cause_type = %s
                  AND primary_outcome = %s
                LIMIT 1
                """,
                (fixture_id, minute, cause_type, primary_outcome),
            )
            return cur.fetchone() is not None
>>>>>>> Stashed changes
