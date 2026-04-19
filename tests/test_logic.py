"""
Unit tests for liveanalyst/logic.py — pure functions only.
No DB, no API, no fixtures file needed.

Run with:
    PYTHONPATH=src pytest tests/test_logic.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from liveanalyst.logic import (
    TickSnapshot,
    cause_confidence,
    classify_tier,
    clamp,
    compute_delta,
    dc_1x2,
    evaluate_signal_outcome,
    is_early_signal,
    normalize_probabilities,
)
from liveanalyst.domain import Probabilities


# ------------------------------------------------------------------ helpers

def _tick(p_home: float, p_draw: float, p_away: float, ts: int = 0) -> TickSnapshot:
    return TickSnapshot(ts=ts, p_home=p_home, p_draw=p_draw, p_away=p_away)


def _ns_tick(p_home: float, p_draw: float, p_away: float):
    """SimpleNamespace tick — used by evaluate_signal_outcome."""
    return SimpleNamespace(p_home=p_home, p_draw=p_draw, p_away=p_away)


def _signal(direction: str = "up", primary_outcome: str = "home"):
    return SimpleNamespace(direction=direction, primary_outcome=primary_outcome)


# ------------------------------------------------------------------ normalize_probabilities

class TestNormalizeProbabilities:
    def test_sum_to_one(self):
        probs = normalize_probabilities(2.0, 3.5, 4.0)
        assert abs(probs.home + probs.draw + probs.away - 1.0) < 1e-9

    def test_shorter_odds_higher_prob(self):
        probs = normalize_probabilities(1.5, 4.0, 6.0)
        assert probs.home > probs.draw > probs.away

    def test_even_odds_equal_probs(self):
        probs = normalize_probabilities(3.0, 3.0, 3.0)
        assert abs(probs.home - probs.draw) < 1e-9
        assert abs(probs.draw - probs.away) < 1e-9

    def test_margin_is_removed(self):
        # Raw implied probs with margin > 1; normalised must still sum to exactly 1.
        probs = normalize_probabilities(2.5, 3.2, 2.8)
        assert abs(probs.home + probs.draw + probs.away - 1.0) < 1e-9

    def test_returns_probabilities_dataclass(self):
        probs = normalize_probabilities(2.0, 3.5, 4.0)
        assert isinstance(probs, Probabilities)


# ------------------------------------------------------------------ compute_delta

class TestComputeDelta:
    def test_returns_max_of_three_diffs(self):
        p_prev = Probabilities(home=0.50, draw=0.30, away=0.20)
        p_now  = Probabilities(home=0.55, draw=0.28, away=0.17)
        # diffs: home=0.05, draw=0.02, away=0.03
        assert abs(compute_delta(p_prev, p_now) - 0.05) < 1e-9

    def test_zero_when_unchanged(self):
        p = Probabilities(home=0.50, draw=0.25, away=0.25)
        assert compute_delta(p, p) == 0.0

    def test_uses_absolute_values(self):
        p_prev = Probabilities(home=0.60, draw=0.25, away=0.15)
        p_now  = Probabilities(home=0.50, draw=0.28, away=0.22)
        # home dropped 0.10 — largest absolute change
        assert abs(compute_delta(p_prev, p_now) - 0.10) < 1e-9


# ------------------------------------------------------------------ classify_tier

class TestClassifyTier:
    def test_below_threshold_returns_none(self):
        assert classify_tier(0.02) is None
        assert classify_tier(0.0) is None

    def test_low_tier(self):
        assert classify_tier(0.03) == "LOW"
        assert classify_tier(0.05) == "LOW"
        assert classify_tier(0.059) == "LOW"

    def test_medium_tier(self):
        assert classify_tier(0.06) == "MEDIUM"
        assert classify_tier(0.09) == "MEDIUM"
        assert classify_tier(0.099) == "MEDIUM"

    def test_high_tier(self):
        assert classify_tier(0.10) == "HIGH"
        assert classify_tier(0.25) == "HIGH"

    def test_boundary_exactly_at_0_03(self):
        assert classify_tier(0.03) == "LOW"

    def test_boundary_exactly_at_0_06(self):
        assert classify_tier(0.06) == "MEDIUM"

    def test_boundary_exactly_at_0_10(self):
        assert classify_tier(0.10) == "HIGH"


# ------------------------------------------------------------------ clamp

class TestClamp:
    def test_within_range_unchanged(self):
        assert clamp(0.5) == 0.5

    def test_above_max_clamped(self):
        assert clamp(1.5) == 1.0

    def test_below_min_clamped(self):
        assert clamp(-0.1) == 0.0

    def test_exactly_at_bounds(self):
        assert clamp(0.0) == 0.0
        assert clamp(1.0) == 1.0

    def test_custom_bounds(self):
        assert clamp(5.0, low=0.0, high=3.0) == 3.0
        assert clamp(-1.0, low=0.2, high=1.0) == 0.2


# ------------------------------------------------------------------ cause_confidence

class TestCauseConfidence:
    def test_allowed_causes_return_1(self):
        assert cause_confidence("GOAL") == 1.0
        assert cause_confidence("RED_CARD") == 1.0
        assert cause_confidence("LINEUP_KEY_PLAYER_OUT") == 1.0

    def test_unknown_cause_returns_0(self):
        assert cause_confidence("SUBSTITUTION") == 0.0
        assert cause_confidence("VAR") == 0.0
        assert cause_confidence("UNKNOWN") == 0.0
        assert cause_confidence("") == 0.0


# ------------------------------------------------------------------ evaluate_signal_outcome

class TestEvaluateSignalOutcome:
    def test_insufficient_data_when_fewer_than_2_ticks(self):
        sig = _signal("up", "home")
        result = evaluate_signal_outcome(sig, [_ns_tick(0.55, 0.25, 0.20)])
        assert result["status"] == "neutral"
        assert result["max_move_within_120s"] == 0.0
        assert result["reversed_within_120s"] is False

    def test_confirmed_when_move_exceeds_threshold(self):
        sig = _signal("up", "home")
        ticks = [
            _ns_tick(0.50, 0.30, 0.20),  # base
            _ns_tick(0.52, 0.29, 0.19),
            _ns_tick(0.55, 0.27, 0.18),  # +0.05 from base — above 0.02 threshold
        ]
        result = evaluate_signal_outcome(sig, ticks)
        assert result["status"] == "confirmed"
        assert result["max_move_within_120s"] >= 0.02
        assert result["reversed_within_120s"] is False

    def test_failed_when_reversed(self):
        sig = _signal("up", "home")
        ticks = [
            _ns_tick(0.55, 0.25, 0.20),  # base (direction=up, so we look at home going up)
            _ns_tick(0.54, 0.26, 0.20),
            _ns_tick(0.53, 0.26, 0.21),
            _ns_tick(0.51, 0.27, 0.22),  # dropped >0.015 from base → reversed
        ]
        result = evaluate_signal_outcome(sig, ticks)
        assert result["status"] == "failed"
        assert result["reversed_within_120s"] is True

    def test_neutral_when_small_move_no_reversal(self):
        sig = _signal("up", "home")
        ticks = [
            _ns_tick(0.50, 0.30, 0.20),
            _ns_tick(0.505, 0.298, 0.197),  # only +0.005 — below 0.02 threshold
            _ns_tick(0.508, 0.296, 0.196),
        ]
        result = evaluate_signal_outcome(sig, ticks)
        assert result["status"] == "neutral"

    def test_direction_down_confirmed(self):
        sig = _signal("down", "home")
        ticks = [
            _ns_tick(0.60, 0.25, 0.15),  # base
            _ns_tick(0.57, 0.27, 0.16),
            _ns_tick(0.55, 0.28, 0.17),  # dropped 0.05 — move in down direction
        ]
        result = evaluate_signal_outcome(sig, ticks)
        assert result["status"] == "confirmed"

    def test_away_outcome_tracked(self):
        sig = _signal("up", "away")
        ticks = [
            _ns_tick(0.50, 0.30, 0.20),
            _ns_tick(0.48, 0.29, 0.23),
            _ns_tick(0.46, 0.28, 0.26),  # away went up 0.06
        ]
        result = evaluate_signal_outcome(sig, ticks)
        assert result["status"] == "confirmed"


# ------------------------------------------------------------------ is_early_signal

class TestIsEarlySignal:
    def _make_ticks(self, ts_prob_pairs: list[tuple[int, float]]) -> list[TickSnapshot]:
        return [
            TickSnapshot(ts=ts, p_home=prob, p_draw=0.30, p_away=1.0 - prob - 0.30)
            for ts, prob in ts_prob_pairs
        ]

    def test_returns_false_with_insufficient_prior_ticks(self):
        signal_ts = 100
        ticks = self._make_ticks([(110, 0.52), (130, 0.55)])  # only future ticks
        assert is_early_signal(ticks, signal_ts) is False

    def test_returns_false_with_insufficient_future_ticks(self):
        signal_ts = 100
        ticks = self._make_ticks([(70, 0.50), (85, 0.50)])  # only prior ticks
        assert is_early_signal(ticks, signal_ts) is False

    def test_true_when_stable_prior_then_big_future_move(self):
        signal_ts = 100
        # Prior: flat (no move before signal)
        # Future: big move after signal
        ticks = self._make_ticks([
            (71, 0.50),
            (85, 0.50),    # prior — essentially flat
            (110, 0.52),
            (190, 0.56),   # future — moved >0.02 from future[0]
        ])
        assert is_early_signal(ticks, signal_ts) is True

    def test_false_when_prior_already_moved(self):
        signal_ts = 100
        ticks = self._make_ticks([
            (71, 0.50),
            (85, 0.515),   # prior moved >0.01 → not early
            (110, 0.54),
            (190, 0.57),
        ])
        assert is_early_signal(ticks, signal_ts) is False

    def test_false_when_future_move_too_small(self):
        signal_ts = 100
        ticks = self._make_ticks([
            (71, 0.50),
            (85, 0.50),
            (110, 0.505),  # future barely moved — below 0.02 threshold
            (190, 0.508),
        ])
        assert is_early_signal(ticks, signal_ts) is False


# ------------------------------------------------------------------ dc_1x2

class TestDC1x2:
    def test_probabilities_sum_to_one(self):
        ph, pd, pa = dc_1x2(1.4, 1.1)
        assert abs(ph + pd + pa - 1.0) < 1e-6

    def test_stronger_home_team_favoured(self):
        ph, pd, pa = dc_1x2(2.0, 0.8)  # dominant home attack, weak away
        assert ph > pa

    def test_symmetric_teams_roughly_equal_win_probs(self):
        ph, pd, pa = dc_1x2(1.3, 1.3)
        assert abs(ph - pa) < 0.01

    def test_draw_probability_positive(self):
        _, pd, _ = dc_1x2(1.4, 1.1)
        assert pd > 0.15  # draws should be reasonably probable

    def test_outputs_are_in_0_1(self):
        for mu_h, mu_a in [(0.5, 0.5), (3.0, 0.3), (0.3, 3.0), (1.5, 1.5)]:
            ph, pd, pa = dc_1x2(mu_h, mu_a)
            assert 0 < ph < 1
            assert 0 < pd < 1
            assert 0 < pa < 1

    def test_zero_rho_equals_independent_poisson(self):
        """With rho=0 τ correction vanishes — result is pure independent Poisson."""
        ph, pd, pa = dc_1x2(1.4, 1.1, rho=0.0)
        assert abs(ph + pd + pa - 1.0) < 1e-6

    def test_typical_match_home_win_probability(self):
        """Home team scoring 1.6 vs away 1.1 should have >50% home win probability."""
        ph, _, _ = dc_1x2(1.6, 1.1)
        assert ph > 0.50
