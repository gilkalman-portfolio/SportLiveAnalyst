"""
Pre-match prediction backtest using football-data.co.uk historical CSVs.
No API calls — downloads free CSVs with full-season odds history.

Usage:
    python -m liveanalyst.backtest_fd
    python -m liveanalyst.backtest_fd --seasons 2425 2526
    python -m liveanalyst.backtest_fd --seasons 2526 --output results_fd.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from liveanalyst.logic import normalize_probabilities
from liveanalyst.prematch import (
    _DRAW_MIN_PROB,
    _MAX_CONFIDENCE,
    _composite_probs,
    _fatigue_penalty,
    _form_score,
    _h2h_home_rate,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.football-data.co.uk/mmz4281"

# API-Football league_id → football-data league code
LEAGUE_CODES = {
    39:  "E0",   # Premier League
    140: "SP1",  # La Liga
    78:  "D1",   # Bundesliga
    135: "I1",   # Serie A
    61:  "F1",   # Ligue 1
}

# football-data season code → display name
SEASON_NAMES = {
    "2324": "2023/24",
    "2425": "2024/25",
    "2526": "2025/26",
}


# ------------------------------------------------------------------ data loading

def _download_csv(season_code: str, league_code: str) -> list[dict]:
    url = f"{BASE_URL}/{season_code}/{league_code}.csv"
    log.info("fd: downloading %s", url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    # football-data CSVs sometimes have trailing commas — use csv.DictReader
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = [r for r in reader if r.get("HomeTeam") and r.get("FTHG") not in (None, "")]
    log.info("fd: %d completed rows from %s/%s", len(rows), season_code, league_code)
    return rows


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {date_str!r}")


def _actual_result(row: dict) -> str | None:
    ftr = row.get("FTR", "").strip()
    return {"H": "home", "D": "draw", "A": "away"}.get(ftr)


def _get_odds(row: dict) -> tuple[float, float, float] | None:
    """Try Bet365 first, then Avg, then any available bookmaker columns."""
    for h_col, d_col, a_col in [
        ("B365H", "B365D", "B365A"),
        ("AvgH",  "AvgD",  "AvgA"),
        ("MaxH",  "MaxD",  "MaxA"),
        ("BWH",   "BWD",   "BWA"),
        ("PSH",   "PSD",   "PSA"),
    ]:
        try:
            h = float(row[h_col])
            d = float(row[d_col])
            a = float(row[a_col])
            if h > 1.0 and d > 1.0 and a > 1.0:
                return h, d, a
        except (KeyError, ValueError, TypeError):
            continue
    return None


# ------------------------------------------------------------------ form / H2H (CSV-native)

def _fd_form_score(
    all_rows: list[dict],
    team: str,
    before: datetime,
    last: int = 5,
) -> tuple[float, str]:
    relevant = [
        r for r in all_rows
        if (r["HomeTeam"] == team or r["AwayTeam"] == team)
        and _parse_date(r["Date"]) < before
        and r.get("FTR", "").strip() in ("H", "D", "A")
    ]
    relevant.sort(key=lambda r: _parse_date(r["Date"]), reverse=True)
    recent = relevant[:last]

    results = []
    for r in recent:
        ftr = r["FTR"].strip()
        if r["HomeTeam"] == team:
            results.append("W" if ftr == "H" else ("D" if ftr == "D" else "L"))
        else:
            results.append("W" if ftr == "A" else ("D" if ftr == "D" else "L"))

    if not results:
        return 0.5, ""
    wins   = results.count("W")
    draws  = results.count("D")
    points = wins * 3 + draws
    score  = points / (len(results) * 3)
    return score, "".join(results)


def _fd_fatigue(
    all_rows: list[dict],
    team: str,
    before: datetime,
    days: int = 14,
) -> tuple[int, int]:
    """Return (games_in_window, days_since_last) for a team before a given date."""
    cutoff = before.replace(tzinfo=None) - timedelta(days=days)
    relevant = sorted(
        [
            r for r in all_rows
            if (r["HomeTeam"] == team or r["AwayTeam"] == team)
            and r.get("FTR", "").strip() in ("H", "D", "A")
            and cutoff <= _parse_date(r["Date"]) < before.replace(tzinfo=None)
        ],
        key=lambda r: _parse_date(r["Date"]),
        reverse=True,
    )
    if not relevant:
        return 0, days
    last = _parse_date(relevant[0]["Date"])
    days_since = max((before.replace(tzinfo=None) - last).days, 0)
    return len(relevant), days_since


def _fd_h2h_rate(
    all_rows: list[dict],
    home_team: str,
    away_team: str,
    before: datetime,
    last: int = 5,
) -> float:
    relevant = [
        r for r in all_rows
        if {r["HomeTeam"], r["AwayTeam"]} == {home_team, away_team}
        and _parse_date(r["Date"]) < before
        and r.get("FTR", "").strip() in ("H", "D", "A")
    ]
    relevant.sort(key=lambda r: _parse_date(r["Date"]), reverse=True)
    recent = relevant[:last]

    if not recent:
        return 0.5
    wins = sum(
        1 for r in recent
        if (r["HomeTeam"] == home_team and r["FTR"].strip() == "H")
        or (r["AwayTeam"] == home_team and r["FTR"].strip() == "A")
    )
    return wins / len(recent)


# ------------------------------------------------------------------ prediction

def _predict(
    row: dict,
    all_rows: list[dict],
    odds: tuple[float, float, float],
) -> dict:
    home = row["HomeTeam"]
    away = row["AwayTeam"]
    date = _parse_date(row["Date"])

    probs = normalize_probabilities(*odds)

    form_home, form_home_str = _fd_form_score(all_rows, home, date)
    form_away, form_away_str = _fd_form_score(all_rows, away, date)
    h2h_home                 = _fd_h2h_rate(all_rows, home, away, date)

    games_14d_home, days_last_home = _fd_fatigue(all_rows, home, date.replace(tzinfo=timezone.utc))
    games_14d_away, days_last_away = _fd_fatigue(all_rows, away, date.replace(tzinfo=timezone.utc))
    fat_home = _fatigue_penalty(games_14d_home, days_last_home)
    fat_away = _fatigue_penalty(games_14d_away, days_last_away)

    c_home, c_draw, c_away = _composite_probs(
        probs.home, probs.draw, probs.away,
        form_home, form_away, h2h_home,
        fatigue_home=fat_home,
        fatigue_away=fat_away,
    )

    if c_draw >= _DRAW_MIN_PROB and c_draw >= min(c_home, c_away):
        candidates = [("home", c_home), ("draw", c_draw), ("away", c_away)]
    else:
        candidates = [("home", c_home), ("away", c_away)]

    best_outcome, confidence = max(candidates, key=lambda x: x[1])
    confidence = min(confidence, _MAX_CONFIDENCE)

    return {
        "predicted":      best_outcome,
        "confidence":     confidence,
        "p_home_comp":    c_home,
        "p_draw_comp":    c_draw,
        "p_away_comp":    c_away,
        "p_home_implied": probs.home,
        "p_draw_implied": probs.draw,
        "p_away_implied": probs.away,
        "form_home":      form_home_str,
        "form_away":      form_away_str,
        "h2h_home_rate":  h2h_home,
        "games_14d_home": games_14d_home,
        "games_14d_away": games_14d_away,
        "days_last_home": days_last_home,
        "days_last_away": days_last_away,
        "fat_home":       fat_home,
        "fat_away":       fat_away,
    }


# ------------------------------------------------------------------ calibration

def _calibration_table(rows: list[dict]) -> list[dict]:
    bands = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]
    out = []
    for lo, hi in bands:
        bucket  = [r for r in rows if lo <= r["confidence"] < hi]
        total   = len(bucket)
        correct = sum(1 for r in bucket if r["correct"])
        label   = f"{lo:.0%}–{hi:.0%}" if hi < 1.01 else f"{lo:.0%}+"
        out.append({"band": label, "total": total, "correct": correct,
                    "accuracy": correct / total if total else None})
    return out


# ------------------------------------------------------------------ main

def run_backtest(
    seasons: list[str],
    output_path: str | None = None,
    min_confidence: float = 0.0,
) -> None:
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    raw_leagues = os.getenv("LEAGUE_IDS", os.getenv("LEAGUE_ID", "39"))
    league_ids  = [int(x.strip()) for x in raw_leagues.split(",") if x.strip()]

    all_rows_by_league: dict[str, list[dict]] = {}

    for league_id in league_ids:
        code = LEAGUE_CODES.get(league_id)
        if not code:
            log.warning("fd: no code for league_id=%s — skipping", league_id)
            continue
        league_rows: list[dict] = []
        for season in seasons:
            try:
                rows = _download_csv(season, code)
                for r in rows:
                    r["_league_id"] = league_id
                league_rows.extend(rows)
            except Exception as exc:
                log.warning("fd: failed to download %s/%s: %s", season, code, exc)
        all_rows_by_league[code] = league_rows

    result_rows: list[dict] = []
    skipped = 0

    for code, league_rows in all_rows_by_league.items():
        league_id = next(k for k, v in LEAGUE_CODES.items() if v == code)
        for row in league_rows:
            actual = _actual_result(row)
            if not actual:
                skipped += 1
                continue
            odds = _get_odds(row)
            if not odds:
                skipped += 1
                continue

            pred = _predict(row, league_rows, odds)

            if min_confidence and pred["confidence"] < min_confidence:
                skipped += 1
                continue

            correct = pred["predicted"] == actual
            result_rows.append({
                "date":          row["Date"],
                "league":        code,
                "home":          row["HomeTeam"],
                "away":          row["AwayTeam"],
                "home_odds":     odds[0],
                "draw_odds":     odds[1],
                "away_odds":     odds[2],
                "predicted":     pred["predicted"],
                "actual":        actual,
                "correct":       correct,
                "confidence":    round(pred["confidence"], 4),
                "p_home_comp":   round(pred["p_home_comp"], 4),
                "p_draw_comp":   round(pred["p_draw_comp"], 4),
                "p_away_comp":   round(pred["p_away_comp"], 4),
                "form_home":      pred["form_home"],
                "form_away":      pred["form_away"],
                "h2h_home_rate":  round(pred["h2h_home_rate"], 3),
                "games_14d_home": pred["games_14d_home"],
                "games_14d_away": pred["games_14d_away"],
                "days_last_home": pred["days_last_home"],
                "days_last_away": pred["days_last_away"],
                "fatigue_home":   round(pred["fat_home"], 3),
                "fatigue_away":   round(pred["fat_away"], 3),
            })

    if not result_rows:
        log.warning("fd: 0 rows generated")
        return

    total   = len(result_rows)
    correct = sum(1 for r in result_rows if r["correct"])

    print(f"\n{'='*55}")
    seasons_str = " + ".join(SEASON_NAMES.get(s, s) for s in seasons)
    print(f"BACKTEST — {seasons_str}  ({total} fixtures)")
    print(f"{'='*55}")
    print(f"Correct  : {correct}/{total} = {correct/total:.1%}")
    print(f"Skipped  : {skipped}")
    print()

    cal = _calibration_table(result_rows)
    print(f"{'Confidence':<15} {'Total':>7} {'Correct':>9} {'Accuracy':>10}")
    print("-" * 45)
    for b in cal:
        acc = f"{b['accuracy']:.1%}" if b["accuracy"] is not None else "   —"
        print(f"{b['band']:<15} {b['total']:>7} {b['correct']:>9} {acc:>10}")

    home_wins = sum(1 for r in result_rows if r["actual"] == "home")
    print(f"\nBaseline (always home): {home_wins/total:.1%}")

    # Draw analysis
    draw_predicted = sum(1 for r in result_rows if r["predicted"] == "draw")
    draw_actual    = sum(1 for r in result_rows if r["actual"] == "draw")
    print(f"Draws predicted       : {draw_predicted}")
    print(f"Draws actual          : {draw_actual} ({draw_actual/total:.1%})")

    if output_path:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=result_rows[0].keys())
            writer.writeheader()
            writer.writerows(result_rows)
        print(f"\nSaved: {output_path}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="Pre-match backtest via football-data.co.uk")
    parser.add_argument("--seasons", nargs="+", default=["2425", "2526"],
                        help="Season codes e.g. 2425 2526 (default: both)")
    parser.add_argument("--output", default=None, help="CSV output path")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="Only include predictions above this confidence (e.g. 0.65)")
    args = parser.parse_args()
    run_backtest(args.seasons, output_path=args.output, min_confidence=args.min_confidence)


if __name__ == "__main__":
    main()
