"""
Pre-match prediction backtest over the last N months.

Usage:
    python -m liveanalyst.backtest
    python -m liveanalyst.backtest --months 3
    python -m liveanalyst.backtest --months 2 --output results.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# Pro plan: 10 req/s — stay safely under
_REQUEST_DELAY = 0.15  # seconds between API calls

from liveanalyst.api_football import APIFootballClient
from liveanalyst.prematch import build_prediction

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers

def _actual_result(fixture: dict) -> str | None:
    """Return 'home' | 'draw' | 'away' from a completed fixture, or None if unavailable."""
    home_g = fixture.get("goals", {}).get("home")
    away_g = fixture.get("goals", {}).get("away")
    if home_g is None or away_g is None:
        return None
    if home_g > away_g:
        return "home"
    if home_g == away_g:
        return "draw"
    return "away"


def _form_before_date(
    all_fixtures: list[dict],
    team_id: int,
    before: datetime,
    last: int = 5,
) -> list[dict]:
    """Return up to `last` completed fixtures for team_id that finished before `before`."""
    relevant = [
        f for f in all_fixtures
        if (
            f["fixture"]["status"]["short"] == "FT"
            and (f["teams"]["home"]["id"] == team_id or f["teams"]["away"]["id"] == team_id)
            and datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")) < before
        )
    ]
    relevant.sort(key=lambda f: f["fixture"]["date"], reverse=True)
    return relevant[:last]


def _h2h_before_date(
    h2h_cache: dict[str, list[dict]],
    home_id: int,
    away_id: int,
    before: datetime,
    last: int = 5,
) -> list[dict]:
    key = f"{min(home_id, away_id)}-{max(home_id, away_id)}"
    all_h2h = h2h_cache.get(key, [])
    relevant = [
        f for f in all_h2h
        if datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")) < before
    ]
    relevant.sort(key=lambda f: f["fixture"]["date"], reverse=True)
    return relevant[:last]


# ------------------------------------------------------------------ calibration

def calibration_table(rows: list[dict]) -> list[dict]:
    """
    Bucket predictions by confidence band and compute actual accuracy per band.
    Returns list of dicts with keys: band, total, correct, accuracy.
    """
    bands = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]
    results = []
    for lo, hi in bands:
        bucket = [r for r in rows if lo <= r["confidence"] < hi]
        total = len(bucket)
        correct = sum(1 for r in bucket if r["correct"])
        label = f"{lo:.0%}–{hi:.0%}" if hi < 1.01 else f"{lo:.0%}+"
        results.append({
            "band": label,
            "total": total,
            "correct": correct,
            "accuracy": correct / total if total > 0 else None,
        })
    return results


# ------------------------------------------------------------------ main

def run_backtest(months: int = 3, output_path: str | None = None) -> None:
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key      = os.environ["API_FOOTBALL_KEY"]
    base_url     = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
    season       = int(os.getenv("SEASON", "2025"))
    raw_leagues  = os.getenv("LEAGUE_IDS", os.getenv("LEAGUE_ID", "39"))
    league_ids   = tuple(int(x.strip()) for x in raw_leagues.split(",") if x.strip())

    api = APIFootballClient(base_url, api_key)

    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)

    # Fetch all completed fixtures for the season (1 request per league)
    log.info("backtest: fetching all FT fixtures for season %s ...", season)
    all_fixtures: list[dict] = []
    for league_id in league_ids:
        data = api._get("/fixtures", league=league_id, season=season, status="FT")
        all_fixtures.extend(data.get("response", []))
    log.info("backtest: fetched %d completed fixtures total", len(all_fixtures))

    # Filter to our backtest window
    target_fixtures = [
        f for f in all_fixtures
        if datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")) >= cutoff
    ]
    log.info("backtest: %d fixtures in last %d months", len(target_fixtures), months)

    if not target_fixtures:
        log.warning("backtest: no fixtures found in window — check season/league settings")
        return

    # Pre-fetch H2H for every unique pair (cache to avoid duplicate requests)
    h2h_cache: dict[str, list[dict]] = {}
    seen_pairs: set[str] = set()
    for f in target_fixtures:
        hid = f["teams"]["home"]["id"]
        aid = f["teams"]["away"]["id"]
        key = f"{min(hid, aid)}-{max(hid, aid)}"
        if key not in seen_pairs:
            seen_pairs.add(key)
            time.sleep(_REQUEST_DELAY)
            h2h_data = api._get("/fixtures/headtohead", h2h=f"{hid}-{aid}", status="FT")
            h2h_cache[key] = h2h_data.get("response", [])
    log.info("backtest: fetched H2H for %d unique pairs", len(h2h_cache))

    rows: list[dict] = []
    skipped = 0

    for f in target_fixtures:
        fixture_id = f["fixture"]["id"]
        kickoff    = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
        home_id    = f["teams"]["home"]["id"]
        away_id    = f["teams"]["away"]["id"]
        home_name  = f["teams"]["home"]["name"]
        away_name  = f["teams"]["away"]["name"]

        actual = _actual_result(f)
        if actual is None:
            skipped += 1
            continue

        # Try pre-match odds
        time.sleep(_REQUEST_DELAY)
        odds = api.get_prematch_odds(fixture_id)
        if not odds:
            log.debug("backtest: no odds for fixture_id=%s — skipping", fixture_id)
            skipped += 1
            continue

        form_home = _form_before_date(all_fixtures, home_id, kickoff)
        form_away = _form_before_date(all_fixtures, away_id, kickoff)
        h2h       = _h2h_before_date(h2h_cache, home_id, away_id, kickoff)

        try:
            pred = build_prediction(f, odds, form_home, form_away, h2h)
        except Exception as exc:
            log.warning("backtest: build_prediction failed fixture_id=%s: %s", fixture_id, exc)
            skipped += 1
            continue

        correct = pred.recommended_outcome == actual
        rows.append({
            "fixture_id":    fixture_id,
            "date":          kickoff.strftime("%Y-%m-%d"),
            "home":          home_name,
            "away":          away_name,
            "predicted":     pred.recommended_outcome,
            "actual":        actual,
            "correct":       correct,
            "confidence":    pred.confidence,
            "home_odds":     pred.home_odds,
            "draw_odds":     pred.draw_odds,
            "away_odds":     pred.away_odds,
            "p_home_comp":   pred.p_home_composite,
            "p_draw_comp":   pred.p_draw_composite,
            "p_away_comp":   pred.p_away_composite,
        })

    if not rows:
        log.warning("backtest: 0 rows generated (all skipped — likely no historical odds on this plan)")
        return

    # ---- Summary
    total   = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    accuracy = correct / total

    print(f"\n{'='*50}")
    print(f"BACKTEST RESULTS — last {months} months")
    print(f"{'='*50}")
    print(f"Fixtures evaluated : {total}")
    print(f"Skipped (no odds)  : {skipped}")
    print(f"Correct predictions: {correct}")
    print(f"Accuracy           : {accuracy:.1%}")
    print()

    # ---- Calibration
    cal = calibration_table(rows)
    print(f"{'Confidence band':<15} {'Total':>7} {'Correct':>9} {'Accuracy':>10}")
    print("-" * 45)
    for band in cal:
        acc_str = f"{band['accuracy']:.1%}" if band["accuracy"] is not None else "  —"
        print(f"{band['band']:<15} {band['total']:>7} {band['correct']:>9} {acc_str:>10}")
    print()

    # Baseline: always predict home win (naive)
    home_wins = sum(1 for r in rows if r["actual"] == "home")
    print(f"Baseline (always home): {home_wins/total:.1%}")

    # ---- CSV output
    if output_path:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nDetailed results saved to: {output_path}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="Pre-match prediction backtest")
    parser.add_argument("--months",  type=int, default=3, help="How many months back to test (default: 3)")
    parser.add_argument("--output",  default=None, help="Optional CSV output path")
    args = parser.parse_args()
    run_backtest(months=args.months, output_path=args.output)


if __name__ == "__main__":
    main()
