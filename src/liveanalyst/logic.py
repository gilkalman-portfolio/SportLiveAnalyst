from __future__ import annotations

from dataclasses import dataclass

from liveanalyst.domain import PreMatchPrediction, Probabilities, SeasonStake, TeamStanding


ALLOWED_CAUSES = {"GOAL", "RED_CARD", "LINEUP_KEY_PLAYER_OUT"}


def key_player_from_lineup_player(player: dict) -> bool:
    stats = player.get("statistics", {})
    return (
        player.get("pos") == "G"
        or stats.get("xg_rank", 999) <= 2
        or stats.get("goal_contrib_rank", 999) <= 2
        or (stats.get("minutes_played_pct", 0) > 70 and stats.get("regular_starter", False))
    )


@dataclass
class TickSnapshot:
    ts: int
    p_home: float
    p_draw: float
    p_away: float


def normalize_probabilities(home_odds: float, draw_odds: float, away_odds: float) -> Probabilities:
    p_home_raw = 1.0 / home_odds
    p_draw_raw = 1.0 / draw_odds
    p_away_raw = 1.0 / away_odds
    total = p_home_raw + p_draw_raw + p_away_raw
    return Probabilities(home=p_home_raw / total, draw=p_draw_raw / total, away=p_away_raw / total)


def compute_delta(p_prev, p_now):
    return max(
        abs(p_now.home - p_prev.home),
        abs(p_now.draw - p_prev.draw),
        abs(p_now.away - p_prev.away),
    )


def max_prob_change(t1, t2):
    return max(
        abs(t2.p_home - t1.p_home),
        abs(t2.p_draw - t1.p_draw),
        abs(t2.p_away - t1.p_away),
    )


def is_early_signal(market_ticks, signal_ts):
    prior = [t for t in market_ticks if signal_ts - 30 <= t.ts < signal_ts]
    future = [t for t in market_ticks if signal_ts < t.ts <= signal_ts + 120]

    if len(prior) < 2 or len(future) < 2:
        return False

    baseline_prior = prior[0]
    moved_before = any(
        max_prob_change(baseline_prior, t) > 0.01
        for t in prior[1:]
    )

    baseline_future = future[0]
    max_future_move = max(
        max_prob_change(baseline_future, t)
        for t in future[1:]
    )

    moved_after = max_future_move > 0.02

    return (not moved_before) and moved_after


def evaluate_signal_outcome(signal, future_ticks, move_threshold=0.02, reversal_threshold=0.015):
    if len(future_ticks) < 2:
        return {
            "status": "neutral",
            "max_move_within_120s": 0.0,
            "reversed_within_120s": False,
            "reason": "insufficient_followup_data",
        }

    base = future_ticks[0]
    direction = signal.direction
    outcome = signal.primary_outcome

    def signed_move(t):
        now = getattr(t, f"p_{outcome}")
        prev = getattr(base, f"p_{outcome}")
        return now - prev if direction == "up" else prev - now

    moves = [signed_move(t) for t in future_ticks[1:]]
    max_move = max(moves) if moves else 0.0
    min_move = min(moves) if moves else 0.0

    reversed_flag = min_move < -reversal_threshold

    if max_move >= move_threshold and not reversed_flag:
        status = "confirmed"
    elif reversed_flag:
        status = "failed"
    else:
        status = "neutral"

    return {
        "status": status,
        "max_move_within_120s": max_move,
        "reversed_within_120s": reversed_flag,
    }


def classify_tier(delta_abs: float) -> str | None:
    if 0.03 <= delta_abs <= 0.059:
        return "LOW"
    if 0.06 <= delta_abs <= 0.099:
        return "MEDIUM"
    if delta_abs >= 0.10:
        return "HIGH"
    return None


def cause_confidence(cause: str) -> float:
    return 1.0 if cause in ALLOWED_CAUSES else 0.0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


_LEAGUE_CONFIGS: dict[int, dict] = {
    39: {  # Premier League
        "season_games": 38,
        "total_teams": 20,
        "cl_spots": 4,
        "el_spots": [5, 6],
        "conf_spots": [7],
        "relegation_spots": 3,
    }
}

_STAKE_BASE: dict[SeasonStake, float] = {
    SeasonStake.TITLE: 1.0,
    SeasonStake.RELEGATION: 1.0,
    SeasonStake.CHAMPIONS_LEAGUE: 0.9,
    SeasonStake.EUROPA_LEAGUE: 0.75,
    SeasonStake.CONFERENCE: 0.6,
    SeasonStake.MID_TABLE: 0.35,
    SeasonStake.SECURED_SAFE: 0.2,
    SeasonStake.RELEGATED: 0.1,
}


def classify_stake(standing: TeamStanding, league_id: int) -> SeasonStake:
    cfg = _LEAGUE_CONFIGS.get(league_id, _LEAGUE_CONFIGS[39])
    pos = standing.position
    rem = standing.games_remaining
    rel_zone = cfg["total_teams"] - cfg["relegation_spots"] + 1  # e.g. 18 for PL

    if pos >= rel_zone and rem <= 2:
        return SeasonStake.RELEGATED
    if pos >= rel_zone:
        return SeasonStake.RELEGATION
    if pos == 1:
        return SeasonStake.TITLE
    if pos <= cfg["cl_spots"]:
        return SeasonStake.CHAMPIONS_LEAGUE
    if pos in cfg["el_spots"]:
        return SeasonStake.EUROPA_LEAGUE
    if pos in cfg["conf_spots"]:
        return SeasonStake.CONFERENCE
    safe_buffer = cfg["relegation_spots"] * 3
    if rem <= safe_buffer // 3:
        return SeasonStake.SECURED_SAFE
    return SeasonStake.MID_TABLE


def compute_motivation(stake: SeasonStake, games_remaining: int) -> float:
    base = _STAKE_BASE[stake]
    if games_remaining <= 3:
        multiplier = 1.3
    elif games_remaining <= 7:
        multiplier = 1.1
    else:
        multiplier = 1.0
    return clamp(base * multiplier)


# Recency weights for form: most recent game first
_FORM_WEIGHTS = [0.30, 0.25, 0.20, 0.15, 0.10]

# Pre-match prediction weights
_W_MOT_REL = 0.04   # relative shift (home vs away)
_W_MOT_ABS = 0.03   # absolute draw suppression (both motivated)
_W_FORM    = 0.03   # home/away-split form
_W_ABSENCE = 0.03   # per missing key player (max 2)


def compute_form_score(results: list[str]) -> float:
    """Score 0..1 from last 5 home (or away) results, most recent first.
    Uses recency weights so yesterday's win counts more than 5 weeks ago.
    results items: 'W' | 'D' | 'L'
    """
    pts = {"W": 3, "D": 1, "L": 0}
    score = sum(pts.get(r, 0) * w for r, w in zip(results, _FORM_WEIGHTS))
    return score / 3.0  # max = 3 * sum(weights) = 3.0


def compute_prematch_prediction(
    fixture_id: int,
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    home_motivation: float | None,
    away_motivation: float | None,
    home_stake: SeasonStake | None,
    away_stake: SeasonStake | None,
    home_form_results: list[str],   # last 5 HOME games, most recent first
    away_form_results: list[str],   # last 5 AWAY games, most recent first
    home_key_absences: int = 0,
    away_key_absences: int = 0,
) -> PreMatchPrediction:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    base = normalize_probabilities(home_odds, draw_odds, away_odds)
    p_home_adj = base.home
    p_draw_adj = base.draw
    p_away_adj = base.away

    # ── Motivation modifier ──────────────────────────────────────────────
    mot_known = home_motivation is not None and away_motivation is not None
    if mot_known:
        # Relative: one team more motivated → shifts home/away
        rel_mot = (home_motivation - away_motivation) * _W_MOT_REL
        p_home_adj += rel_mot
        p_away_adj -= rel_mot
        # Absolute: both teams highly motivated → draw less likely (and vice versa)
        total_mot = (home_motivation + away_motivation) / 2.0
        draw_mot_adj = -(total_mot - 0.5) * _W_MOT_ABS  # range ≈ -0.015..+0.015
        p_draw_adj += draw_mot_adj

    # ── Form modifier (home/away split) ──────────────────────────────────
    home_fs = compute_form_score(home_form_results) if home_form_results else None
    away_fs = compute_form_score(away_form_results) if away_form_results else None
    if home_fs is not None and away_fs is not None:
        form_delta = (home_fs - away_fs) * _W_FORM
        p_home_adj += form_delta
        p_away_adj -= form_delta

    # ── Key player absences ───────────────────────────────────────────────
    p_home_adj -= min(home_key_absences, 2) * _W_ABSENCE
    p_away_adj -= min(away_key_absences, 2) * _W_ABSENCE

    # ── Normalize so probabilities sum to 1 ──────────────────────────────
    total = p_home_adj + p_draw_adj + p_away_adj
    p_home_f = clamp(p_home_adj / total)
    p_draw_f  = clamp(p_draw_adj  / total)
    p_away_f  = clamp(p_away_adj  / total)

    predicted_outcome = max(
        {"home": p_home_f, "draw": p_draw_f, "away": p_away_f},
        key=lambda k: {"home": p_home_f, "draw": p_draw_f, "away": p_away_f}[k],
    )

    # ── Confidence ────────────────────────────────────────────────────────
    confidence = 1.0
    if not home_form_results:  confidence -= 0.20
    if not away_form_results:  confidence -= 0.20
    if not mot_known:          confidence -= 0.15
    if max(p_home_f, p_draw_f, p_away_f) < 0.40:
        confidence -= 0.15
    confidence = clamp(confidence)

    # ── Blocking ──────────────────────────────────────────────────────────
    reasons: list[str] = []
    if mot_known and home_motivation < 0.20 and away_motivation < 0.20:
        reasons.append("dead_rubber")
    if max(p_home_f, p_draw_f, p_away_f) < 0.35:
        reasons.append("market_too_even")
    if confidence < 0.50:
        reasons.append("low_confidence")

    blocked = bool(reasons)
    return PreMatchPrediction(
        fixture_id=fixture_id,
        ts_created=now,
        p_home=p_home_f,
        p_draw=p_draw_f,
        p_away=p_away_f,
        predicted_outcome=predicted_outcome,
        confidence=confidence,
        actionable=not blocked,
        block_reason=",".join(reasons) if reasons else None,
        home_stake=home_stake,
        away_stake=away_stake,
        home_motivation=home_motivation,
        away_motivation=away_motivation,
        home_form_score=home_fs,
        away_form_score=away_fs,
    )
