from __future__ import annotations

from dataclasses import dataclass
from math import exp, factorial

from liveanalyst.domain import Probabilities, SeasonStake, TeamStanding


ALLOWED_CAUSES = {"GOAL", "RED_CARD", "LINEUP_KEY_PLAYER_OUT", "ODDS_MOVE"}


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
    if 0.03 <= delta_abs < 0.06:
        return "LOW"
    if 0.06 <= delta_abs < 0.10:
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


def dc_1x2(
    mu_home: float,
    mu_away: float,
    rho: float = 0.13,
    max_goals: int = 7,
) -> tuple[float, float, float]:
    """Dixon-Coles 1X2 probabilities with τ correction for low-score scorelines.

    τ correction adjusts the Poisson-independent assumption for:
      (0,0), (1,0), (0,1), (1,1) — historically over/under-estimated by pure Poisson.
    rho ≈ 0.13 is a typical league calibration constant.
    """
    def _pmf(k: int, mu: float) -> float:
        return exp(-mu) * mu ** k / factorial(k)

    def _tau(h: int, a: int) -> float:
        if h == 0 and a == 0:
            return 1 - mu_home * mu_away * rho
        if h == 1 and a == 0:
            return 1 + mu_away * rho
        if h == 0 and a == 1:
            return 1 + mu_home * rho
        if h == 1 and a == 1:
            return 1 - rho
        return 1.0

    p_home = p_draw = p_away = 0.0
    for h in range(max_goals + 1):
        ph = _pmf(h, mu_home)
        for a in range(max_goals + 1):
            prob = ph * _pmf(a, mu_away) * _tau(h, a)
            if h > a:
                p_home += prob
            elif h == a:
                p_draw += prob
            else:
                p_away += prob

    total = p_home + p_draw + p_away
    if total == 0:
        return 1 / 3, 1 / 3, 1 / 3
    return p_home / total, p_draw / total, p_away / total
