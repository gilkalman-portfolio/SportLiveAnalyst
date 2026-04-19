from __future__ import annotations

import logging
from datetime import datetime, timezone

from liveanalyst.api_football import APIFootballClient
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.prematch import (
    CONFIDENCE_THRESHOLD,
    fetch_predictions,
    telegram_prematch_message,
)
from liveanalyst.telegram import TelegramSender

log = logging.getLogger(__name__)

POLL_INTERVAL_S      = 300    # every 5 min while new fixtures remain
POLL_IDLE_S          = 900    # every 15 min after all fixtures processed


class PreMatchWorker:
    def __init__(self, settings: Settings, db: Database, api: APIFootballClient, telegram: TelegramSender):
        self.settings = settings
        self.db       = db
        self.api      = api
        self.telegram = telegram
        self._last_run_at: datetime | None = None
        self._all_done_date: str = ""   # date string when all fixtures are processed

    def _interval(self, today: str) -> int:
        return POLL_IDLE_S if self._all_done_date == today else POLL_INTERVAL_S

    def run_once(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        interval = self._interval(today)
        if self._last_run_at and (now - self._last_run_at).total_seconds() < interval:
            return
        self._last_run_at = now

        # Pass db so fetch_predictions skips already-processed fixtures (survives restarts)
        new_preds = fetch_predictions(
            self.api, self.settings.league_ids, self.settings.season, today, db=self.db
        )

        if not new_preds:
            # No new fixtures — mark done so we slow down to hourly
            if self._all_done_date != today:
                self._all_done_date = today
                log.info("prematch_worker: all fixtures processed for %s — switching to hourly poll", today)
            return

        for pred in new_preds:
            pred_id = self.db.upsert_prematch_prediction(pred)

            if pred.confidence < CONFIDENCE_THRESHOLD:
                log.info(
                    "prematch_worker: skip fixture_id=%s confidence=%.0f%% below threshold",
                    pred.fixture_id, pred.confidence * 100,
                )
                continue

            msg = telegram_prematch_message(pred)
            self.telegram.send(msg)
            self.db.mark_prematch_telegram_sent(pred_id)
            log.info(
                "prematch_worker: sent fixture_id=%s %s vs %s → %s %.0f%%",
                pred.fixture_id, pred.home_team, pred.away_team,
                pred.recommended_outcome, pred.confidence * 100,
            )
