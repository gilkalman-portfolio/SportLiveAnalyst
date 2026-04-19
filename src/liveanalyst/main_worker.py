"""
Main orchestrator — runs all sub-workers in a single process.

Sub-workers share one DB connection, one API client, one Telegram sender.

    LiveWorker    — live odds + signals        (every 5s)
    PreMatchWorker — pre-match predictions     (every 5min, once per fixture per day)
    LineupWorker  — lineup/injury corrections  (every 15min, 30-75min before kickoff)

Usage:
    python -m liveanalyst.main_worker
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ISRAEL_TZ = _ZoneInfo("Asia/Jerusalem")
except ImportError:
    from datetime import timedelta
    _ISRAEL_TZ = timezone(timedelta(hours=3))  # fallback: UTC+3

from liveanalyst.api_football import APIFootballClient
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.lineup_worker import LineupWorker
from liveanalyst.prematch_worker import PreMatchWorker
from liveanalyst.telegram import TelegramSender
from liveanalyst.worker import LiveAnalystWorker

MAIN_LOOP_SLEEP_S   = 5
ACTIVE_HOUR_START   = 12   # 12:00 Israel time
ACTIVE_HOUR_END     = 23   # 23:59 Israel time (inclusive)

MIGRATIONS = [
    "sql/migrations/001_init.sql",
    "sql/migrations/002_add_event_to_odds_ms.sql",
    "sql/migrations/003_add_league_id.sql",
    "sql/migrations/004_add_bets_log.sql",
    "sql/migrations/005_add_is_early_signal.sql",
    "sql/migrations/006_add_prematch_predictions.sql",
    "sql/migrations/007_add_lineup_checks.sql",
]


class MainWorker:
    def __init__(self, settings: Settings):
        self.settings = settings

        # Shared infrastructure — one connection each
        self.db       = Database(settings.postgres_dsn)
        self.api      = APIFootballClient(settings.api_football_base_url, settings.api_football_key)
        self.telegram = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)

        # Sub-workers — all share the same db/api/telegram
        self.live     = LiveAnalystWorker(settings, db=self.db, api=self.api, telegram=self.telegram)
        self.prematch = PreMatchWorker(settings, db=self.db, api=self.api, telegram=self.telegram)
        self.lineup   = LineupWorker(settings, db=self.db, api=self.api, telegram=self.telegram)

    def bootstrap(self) -> None:
        for migration in MIGRATIONS:
            self.db.run_migration(migration)
        self.live._recover_outcomes()
        logging.info("main_worker: bootstrap complete")

    def run_forever(self) -> None:
        self.bootstrap()
        logging.info("main_worker: starting — live + prematch + lineup workers active")
        _sleeping = False

        while True:
            try:
                now = datetime.now(timezone.utc)
                il_hour = now.astimezone(_ISRAEL_TZ).hour
                if not (ACTIVE_HOUR_START <= il_hour <= ACTIVE_HOUR_END):
                    if not _sleeping:
                        logging.info(
                            "main_worker: outside active hours (%02d:00–%02d:59 IL) — pausing",
                            ACTIVE_HOUR_START, ACTIVE_HOUR_END,
                        )
                        _sleeping = True
                    time.sleep(60)
                    continue
                if _sleeping:
                    logging.info("main_worker: active hours resumed — workers running")
                    _sleeping = False
                self.live.run_once()
                self.live.process_follow_ups()
                self.prematch.run_once(now)
                self.lineup.run_once(now)
            except Exception as exc:
                logging.exception("main_worker: unhandled error: %s", exc)
            time.sleep(MAIN_LOOP_SLEEP_S)


def main() -> None:
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/worker.log", encoding="utf-8"),
        ],
    )
    settings = Settings.from_env()
    MainWorker(settings).run_forever()


if __name__ == "__main__":
    main()
