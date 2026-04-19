"""
Replay mode for LiveAnalyst v0.

Loads saved market_ticks and events for a single fixture from the DB and
replays them through the same signal pipeline as if they were live.

Usage:
    python -m liveanalyst.replay --fixture <fixture_id> [--speed instant|1x|5x] [--telegram]

Guarantees:
- Uses the same detect_signal() logic as the live worker (no divergence).
- Idempotent: skips signals that already exist for (fixture_id, minute, cause_type, primary_outcome).
- Blocked signals are not written to DB (replay only stores actionable signals).
- is_early_signal is evaluated only in follow-up, never during signal creation.
- signal_latency_ms is set to 0 for replay signals (now=ev_ts, not wall clock).
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import timedelta
from types import SimpleNamespace

from dotenv import load_dotenv

from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.domain import Probabilities
from liveanalyst.logic import (
    TickSnapshot,
    evaluate_signal_outcome,
    is_early_signal,
)
from liveanalyst.telegram import TelegramSender
from liveanalyst.worker import (
    detect_signal,
    pick_primary_outcome,
    telegram_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Maps speed arg to divisor applied to real-time delta between events.
# None = no sleep (instant).
_SPEED_DIVISORS: dict[str, float | None] = {
    "instant": None,
    "1x": 1.0,
    "5x": 5.0,
}


class ReplayWorker:
    def __init__(
        self,
        settings: Settings,
        fixture_id: int,
        speed: str = "instant",
        emit_telegram: bool = False,
    ) -> None:
        self.db = Database(settings.postgres_dsn)
        self.telegram = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)
        self.fixture_id = fixture_id
        self.speed = speed
        self.emit_telegram = emit_telegram

    def run(self) -> None:
        ticks = self.db.get_all_ticks_for_fixture(self.fixture_id)
        events = self.db.get_all_events_for_fixture(self.fixture_id)

        if not ticks:
            logging.info("replay_no_ticks fixture_id=%s — nothing to replay", self.fixture_id)
            return
        if not events:
            logging.info("replay_no_events fixture_id=%s — nothing to replay", self.fixture_id)
            return

        logging.info(
            "replay_start fixture_id=%s ticks=%d events=%d speed=%s telegram=%s",
            self.fixture_id, len(ticks), len(events), self.speed, self.emit_telegram,
        )

        speed_div = _SPEED_DIVISORS[self.speed]
        tick_ptr = 0          # advances forward only — O(n) total
        prev_event_ts = None
        replay_signal_ids: list[int] = []

        for event in events:
            ev_ts = event["ts"]

            # --- Speed simulation ---
            if speed_div is not None and prev_event_ts is not None:
                delta_s = (ev_ts - prev_event_ts).total_seconds()
                if delta_s > 0:
                    time.sleep(delta_s / speed_div)
            prev_event_ts = ev_ts

            # --- Advance tick pointer to include all ticks up to ev_ts ---
            while tick_ptr < len(ticks) and ticks[tick_ptr]["ts"] <= ev_ts:
                tick_ptr += 1
            visible_count = tick_ptr  # ticks[0:visible_count] are "visible" at ev_ts

            if visible_count < 2:
                continue  # need at least two ticks for a delta

            last_tick = ticks[visible_count - 1]
            prev_tick = ticks[visible_count - 2]

            cause = event["event_type"]
            is_key = bool(event.get("is_key_player", False))
            minute = event.get("minute") or 0

            p_prev = Probabilities(
                home=prev_tick["p_home"],
                draw=prev_tick["p_draw"],
                away=prev_tick["p_away"],
            )
            p_now = Probabilities(
                home=last_tick["p_home"],
                draw=last_tick["p_draw"],
                away=last_tick["p_away"],
            )

            # Pre-compute outcome/direction needed for idempotency key and
            # DB-dependent block checks (detect_signal() also computes these
            # internally — it's a pure function, so the duplication is cheap).
            outcome_pre, direction_pre, _, _, _ = pick_primary_outcome(p_prev, p_now)

            # --- Idempotency guard ---
            if self.db.signal_exists_for_key(self.fixture_id, minute, cause, outcome_pre):
                logging.info(
                    "replay_skip_duplicate fixture_id=%s minute=%s cause=%s outcome=%s",
                    self.fixture_id, minute, cause, outcome_pre,
                )
                continue

            cooldown_key_pre = f"{self.fixture_id}:{cause}:{direction_pre}"

            # Build oscillation window from the in-memory tick list (no DB call).
            # detect_signal() only reads p_home/p_draw/p_away from these dicts.
            osc_from = ev_ts - timedelta(seconds=30)
            oscillation_ticks = [
                t for t in ticks[:visible_count]
                if osc_from <= t["ts"] <= ev_ts
            ]

            # In replay "now" == ev_ts: we process the event exactly at event time.
            # This makes signal_latency_ms = 0 and keeps confidence penalty for
            # source latency realistic (using stored source_latency_ms from the tick).
            prior_exists = self.db.prior_same_direction_exists(
                self.fixture_id, outcome_pre, direction_pre, ev_ts
            )
            cooldown_hit = self.db.cooldown_blocked(cooldown_key_pre, ev_ts, 300)

            signal = detect_signal(
                fixture_id=self.fixture_id,
                minute=minute,
                ev_ts=ev_ts,
                cause=cause,
                is_key=is_key,
                p_prev=p_prev,
                p_now=p_now,
                source_latency_ms=int(last_tick.get("source_latency_ms") or 0),
                now=ev_ts,
                oscillation_ticks=oscillation_ticks,
                prior_exists=prior_exists,
                cooldown_hit=cooldown_hit,
            )

            if signal is None:
                # Delta below lowest tier threshold — nothing to record.
                continue

            # Blocked signals are not persisted in replay.
            if signal.blocked:
                logging.info(
                    "replay_signal_blocked fixture_id=%s minute=%s cause=%s reason=%s",
                    self.fixture_id, minute, cause, signal.block_reason,
                )
                continue

            signal_id = self.db.insert_signal(signal)
            replay_signal_ids.append(signal_id)
            logging.info(
                "replay_signal_created signal_id=%s fixture_id=%s minute=%s "
                "cause=%s tier=%s delta=%.4f confidence=%.2f",
                signal_id, self.fixture_id, minute, cause,
                signal.tier, signal.delta_abs, signal.confidence,
            )

            if self.emit_telegram:
                msg = telegram_message(
                    tier=signal.tier,
                    minute=minute,
                    home=f"Home[{self.fixture_id}]",
                    away=f"Away[{self.fixture_id}]",
                    cause=cause,
                    cause_team=None,
                    signal=signal,
                )
                self.telegram.send(msg)
                self.db.mark_telegram_sent(signal_id)

        logging.info(
            "replay_complete fixture_id=%s actionable_signals=%d",
            self.fixture_id, len(replay_signal_ids),
        )

        self._evaluate_followups(replay_signal_ids)

    def _evaluate_followups(self, signal_ids: list[int]) -> None:
        """Evaluate signal outcomes using the stored tick history.

        Mirrors process_follow_ups() in the live worker exactly, with two
        differences:
        - All three checkpoints (30s / 60s / 120s) are evaluated in one pass
          using the full 120s tick window already in the DB.
        - is_early_signal is evaluated here (at the 120s window), same as live.
        """
        for signal_id in signal_ids:
            signal = self.db.get_signal(signal_id)
            if not signal:
                continue

            ticks = self.db.get_ticks_after(self.fixture_id, signal["ts_created"], 120)

            # Guard: insufficient follow-up data
            if len(ticks) < 3:
                self.db.upsert_signal_outcome(
                    signal_id=signal_id,
                    status="insufficient_data",
                    time_to_move=None,
                    max_move=0.0,
                    reversed_flag=False,
                )
                logging.info(
                    "replay_followup signal_id=%s status=insufficient_data ticks_in_window=%d",
                    signal_id, len(ticks),
                )
                continue

            ticks_as_obj = [SimpleNamespace(**t) for t in ticks]
            signal_obj = SimpleNamespace(
                direction=signal["direction"],
                primary_outcome=signal["primary_outcome"],
            )
            outcome = evaluate_signal_outcome(signal_obj, ticks_as_obj)

            time_to_move = None
            base = ticks[0]
            base_v = base[f"p_{signal['primary_outcome']}"]
            for t in ticks[1:]:
                now_v = t[f"p_{signal['primary_outcome']}"]
                signed = now_v - base_v if signal["direction"] == "up" else base_v - now_v
                if signed >= 0.02:
                    time_to_move = int((t["ts"] - base["ts"]).total_seconds())
                    break

            self.db.upsert_signal_outcome(
                signal_id=signal_id,
                status=outcome["status"],
                time_to_move=time_to_move,
                max_move=outcome["max_move_within_120s"],
                reversed_flag=outcome["reversed_within_120s"],
            )

            # is_early_signal evaluated at 120s window — same as live worker
            ts_created = signal["ts_created"]
            early_window = self.db.recent_ticks_window(
                self.fixture_id,
                ts_created - timedelta(seconds=30),
                ts_created + timedelta(seconds=120),
            )
            early_flag = is_early_signal(
                [TickSnapshot(**t) for t in early_window],
                int(ts_created.timestamp()),
            )

            logging.info(
                "replay_followup signal_id=%s status=%s max_move=%.4f "
                "reversed=%s early=%s time_to_move=%s",
                signal_id,
                outcome["status"],
                outcome["max_move_within_120s"],
                outcome["reversed_within_120s"],
                early_flag,
                time_to_move,
            )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Replay LiveAnalyst signals for a single fixture from saved DB data."
    )
    parser.add_argument(
        "--fixture", type=int, required=True,
        help="Fixture ID to replay (must have ticks + events in DB).",
    )
    parser.add_argument(
        "--speed", choices=["instant", "1x", "5x"], default="instant",
        help="Replay speed: instant (no sleep) / 1x (real time) / 5x (5× faster). Default: instant.",
    )
    parser.add_argument(
        "--telegram", action="store_true", default=False,
        help="Emit Telegram messages during replay. Default: off.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    worker = ReplayWorker(
        settings=settings,
        fixture_id=args.fixture,
        speed=args.speed,
        emit_telegram=args.telegram,
    )
    worker.run()


if __name__ == "__main__":
    main()
