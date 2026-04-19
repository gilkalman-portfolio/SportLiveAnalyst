"""
Run pre-match predictions for today's fixtures.

Usage:
    python -m liveanalyst.prematch_runner
    python -m liveanalyst.prematch_runner --date 2025-05-10
    python -m liveanalyst.prematch_runner --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys

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


def _settle_finished_predictions(api, db, telegram, dry_run: bool) -> None:
    """Fetch final scores for past predictions and record outcomes + Brier Score."""
    pending = db.get_unsettled_predictions(hours_after_kickoff=3)
    if not pending:
        return
    log.info("backtest: settling %d prediction(s)", len(pending))
    for row in pending:
        try:
            fixture = api.get_fixture_result(row["fixture_id"])
            if not fixture:
                continue
            status = fixture["fixture"]["status"]["short"]
            if status not in ("FT", "AET", "PEN"):
                continue  # match not finished yet
            home_goals = fixture["goals"]["home"]
            away_goals = fixture["goals"]["away"]
            if home_goals is None or away_goals is None:
                continue
            if not dry_run:
                db.record_prediction_outcome(
                    fixture_id=row["fixture_id"],
                    prediction_id=row["prediction_id"],
                    home_goals=int(home_goals),
                    away_goals=int(away_goals),
                    predicted_outcome=row["recommended_outcome"],
                    predicted_confidence=row["confidence"],
                )
            log.info(
                "backtest: settled fixture_id=%s result=%d-%d predicted=%s",
                row["fixture_id"], home_goals, away_goals, row["recommended_outcome"],
            )
        except Exception as e:
            log.warning("backtest: failed to settle fixture_id=%s: %s", row["fixture_id"], e)

    # Weekly Brier Score report to Telegram (every time we settle, show running stats)
    if not dry_run:
        stats = db.get_weekly_brier_score()
        if stats.get("n", 0) > 0:
            msg = (
                f"📊 דוח שבועי — Pre-Match:\n"
                f"משחקים שהוכרעו (7 ימים): {stats['n']}\n"
                f"דיוק: {stats.get('accuracy_pct', 0):.1f}% ({stats.get('correct', 0)}/{stats['n']})\n"
                f"Brier Score: {stats.get('avg_brier', 'N/A')}"
            )
            telegram.send(msg)
            log.info("backtest: weekly brier report sent — %s", stats)


def run(date: str | None = None, dry_run: bool = False) -> None:
    settings = Settings.from_env()
    api      = APIFootballClient(settings.api_football_base_url, settings.api_football_key)
    db       = Database(settings.postgres_dsn)
    telegram = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)

    db.run_migration("sql/migrations/006_add_prematch_predictions.sql")
    db.run_migration("sql/migrations/007_add_lineup_checks.sql")
    db.run_migration("sql/migrations/008_add_prematch_columns.sql")

    _settle_finished_predictions(api, db, telegram, dry_run)

    predictions = fetch_predictions(api, settings.league_ids, settings.season, date)

    if not predictions:
        log.info("prematch_runner: no predictions generated")
        return

    sent = 0
    for pred in predictions:
        pred_id = db.upsert_prematch_prediction(pred)

        if pred.confidence < CONFIDENCE_THRESHOLD:
            log.info(
                "prematch_runner: skipping fixture_id=%s confidence=%.0f%% (below threshold)",
                pred.fixture_id, pred.confidence * 100,
            )
            continue

        msg = telegram_prematch_message(pred)
        if dry_run:
            print(msg)
            print("---")
        else:
            telegram.send(msg)
            db.mark_prematch_telegram_sent(pred_id)
            log.info(
                "prematch_runner: sent prediction fixture_id=%s %s vs %s → %s %.0f%%",
                pred.fixture_id, pred.home_team, pred.away_team,
                pred.recommended_outcome, pred.confidence * 100,
            )
        sent += 1

    log.info("prematch_runner: done — %d/%d predictions sent", sent, len(predictions))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="Pre-match prediction runner")
    parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Print messages without sending to Telegram")
    args = parser.parse_args()
    run(date=args.date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
