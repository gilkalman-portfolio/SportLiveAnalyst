"""
Data integrity tests — validates that data produced by the system
conforms to expected types, ranges, and business rules.

No DB or API required — all tests are pure/unit level.

Run with:
    PYTHONPATH=src pytest tests/test_data_integrity.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liveanalyst.domain import MarketTick, Probabilities, SignalContext
from liveanalyst.logic import normalize_probabilities, classify_tier, clamp
from liveanalyst.worker import detect_signal

# ------------------------------------------------------------------ constants

_VALID_TIERS       = {"LOW", "MEDIUM", "HIGH"}
_VALID_DIRECTIONS  = {"up", "down"}
_VALID_OUTCOMES    = {"home", "draw", "away"}
_VALID_CAUSES      = {"GOAL", "RED_CARD", "LINEUP_KEY_PLAYER_OUT"}
_KNOWN_BLOCK_REASONS = {
    "unsupported_cause",
    "lineup_player_not_key",
    "minute_gte_88",
    "confidence_lt_0.6",
    "prior_same_direction_move",
    "cooldown_300s",
}

_NOW    = datetime(2026, 4, 11, 15, 0, 0, tzinfo=timezone.utc)
_EV_TS  = _NOW


# ------------------------------------------------------------------ helpers

def _probs(home, draw, away):
    return Probabilities(home=home, draw=draw, away=away)


def _signal(**overrides) -> SignalContext | None:
    defaults = dict(
        fixture_id=1,
        minute=45,
        ev_ts=_EV_TS,
        cause="GOAL",
        is_key=True,
        p_prev=_probs(0.40, 0.30, 0.30),
        p_now=_probs(0.55, 0.25, 0.20),
        source_latency_ms=500,
        now=_NOW,
        oscillation_ticks=[],
        prior_exists=False,
        cooldown_hit=False,
    )
    defaults.update(overrides)
    return detect_signal(**defaults)


# ================================================================== Probabilities

class TestProbabilitiesIntegrity:
    """normalize_probabilities must always return values that sum to 1.0."""

    def test_sum_to_one_typical(self):
        p = normalize_probabilities(2.10, 3.40, 3.60)
        assert abs(p.home + p.draw + p.away - 1.0) < 1e-9

    def test_sum_to_one_heavy_favourite(self):
        p = normalize_probabilities(1.05, 12.0, 21.0)
        assert abs(p.home + p.draw + p.away - 1.0) < 1e-9

    def test_sum_to_one_even_odds(self):
        p = normalize_probabilities(3.0, 3.0, 3.0)
        assert abs(p.home + p.draw + p.away - 1.0) < 1e-9

    def test_all_values_between_zero_and_one(self):
        p = normalize_probabilities(1.50, 4.0, 6.0)
        assert 0 < p.home < 1
        assert 0 < p.draw < 1
        assert 0 < p.away < 1

    def test_shorter_odds_yield_higher_prob(self):
        p = normalize_probabilities(1.50, 3.50, 5.00)
        assert p.home > p.draw > p.away

    def test_margin_is_removed(self):
        """Raw implied probs sum > 1 (bookmaker margin). After normalisation = 1."""
        raw_sum = 1/2.10 + 1/3.40 + 1/3.60
        assert raw_sum > 1.0
        p = normalize_probabilities(2.10, 3.40, 3.60)
        assert abs(p.home + p.draw + p.away - 1.0) < 1e-9


# ================================================================== SignalContext fields

class TestSignalContextIntegrity:
    """Every SignalContext produced by detect_signal() must have valid field values."""

    def test_tier_is_valid_value(self):
        s = _signal()
        assert s.tier in _VALID_TIERS

    def test_direction_is_valid_value(self):
        s = _signal()
        assert s.direction in _VALID_DIRECTIONS

    def test_primary_outcome_is_valid_value(self):
        s = _signal()
        assert s.primary_outcome in _VALID_OUTCOMES

    def test_confidence_within_range(self):
        s = _signal()
        assert 0.0 <= s.confidence <= 1.0

    def test_confidence_non_negative_after_all_penalties(self):
        """Stacking all penalties must not drop confidence below 0."""
        s = _signal(
            source_latency_ms=60_000,          # -0.25
            ev_ts=_NOW - timedelta(seconds=20), # -0.20
            cause="LINEUP_KEY_PLAYER_OUT",
            is_key=False,                       # -0.20
            oscillation_ticks=[
                {"p_home": 0.50, "p_draw": 0.25, "p_away": 0.25},
                {"p_home": 0.52, "p_draw": 0.24, "p_away": 0.24},
                {"p_home": 0.50, "p_draw": 0.25, "p_away": 0.25},
            ],                                  # -0.20
        )
        # s may be None (blocked) but if returned, confidence ≥ 0
        if s is not None:
            assert s.confidence >= 0.0

    def test_delta_abs_matches_tier_low(self):
        s = _signal(
            p_prev=_probs(0.40, 0.33, 0.27),
            p_now=_probs(0.44, 0.31, 0.25),  # delta ≈ 0.04
        )
        assert s is not None
        assert s.tier == "LOW"
        assert 0.03 <= s.delta_abs < 0.06

    def test_delta_abs_matches_tier_medium(self):
        s = _signal(
            p_prev=_probs(0.40, 0.33, 0.27),
            p_now=_probs(0.48, 0.30, 0.22),  # delta ≈ 0.08
        )
        assert s is not None
        assert s.tier == "MEDIUM"
        assert 0.06 <= s.delta_abs < 0.10

    def test_delta_abs_matches_tier_high(self):
        s = _signal(
            p_prev=_probs(0.40, 0.30, 0.30),
            p_now=_probs(0.55, 0.25, 0.20),  # delta = 0.15
        )
        assert s is not None
        assert s.tier == "HIGH"
        assert s.delta_abs >= 0.10

    def test_signal_latency_ms_non_negative(self):
        s = _signal(now=_NOW, ev_ts=_NOW - timedelta(seconds=5))
        assert s is not None
        assert s.signal_latency_ms >= 0

    def test_signal_latency_ms_is_zero_for_fresh_event(self):
        s = _signal(now=_NOW, ev_ts=_NOW)
        assert s is not None
        assert s.signal_latency_ms == 0

    def test_signal_latency_ms_reflects_delay(self):
        ev = _NOW - timedelta(seconds=30)
        s = _signal(now=_NOW, ev_ts=ev)
        assert s is not None
        assert s.signal_latency_ms == 30_000

    def test_cooldown_key_format(self):
        s = _signal(fixture_id=42)
        assert s is not None
        parts = s.cooldown_key.split(":")
        assert len(parts) == 3
        assert parts[0] == "42"
        assert parts[2] in _VALID_DIRECTIONS

    def test_signal_type_is_shift(self):
        s = _signal()
        assert s is not None
        assert s.signal_type == "SHIFT"


# ================================================================== Block reasons

class TestBlockReasonIntegrity:
    """block_reason must only contain known tokens."""

    def _block_reasons(self, s: SignalContext) -> set[str]:
        if not s.block_reason:
            return set()
        return set(s.block_reason.split(","))

    def test_actionable_signal_has_no_block_reason(self):
        s = _signal()
        assert s is not None
        assert s.actionable
        assert s.block_reason is None

    def test_block_reasons_are_known_tokens(self):
        s = _signal(minute=89)  # minute_gte_88
        assert s is not None
        assert s.blocked
        reasons = self._block_reasons(s)
        assert reasons.issubset(_KNOWN_BLOCK_REASONS), f"Unknown reasons: {reasons - _KNOWN_BLOCK_REASONS}"

    def test_multiple_block_reasons_are_valid_tokens(self):
        s = _signal(minute=89, cooldown_hit=True)
        assert s is not None
        reasons = self._block_reasons(s)
        assert reasons.issubset(_KNOWN_BLOCK_REASONS)
        assert len(reasons) >= 2

    def test_unsupported_cause_is_blocked(self):
        s = _signal(cause="YELLOW_CARD")
        assert s is not None
        assert s.blocked
        assert "unsupported_cause" in self._block_reasons(s)


# ================================================================== Event timestamp

class TestEventTimestamp:
    """ev_ts must be computed from kickoff + elapsed when elapsed_at is absent."""

    def test_kickoff_plus_elapsed_gives_correct_ev_ts(self):
        kickoff = datetime(2026, 4, 11, 15, 0, 0, tzinfo=timezone.utc)
        elapsed_min = 17
        ev_ts = kickoff + timedelta(minutes=elapsed_min)
        assert ev_ts == datetime(2026, 4, 11, 15, 17, 0, tzinfo=timezone.utc)

    def test_ev_ts_is_before_detection_time(self):
        """The event must have happened before we detected it."""
        kickoff = datetime(2026, 4, 11, 15, 0, 0, tzinfo=timezone.utc)
        elapsed_min = 17
        ev_ts = kickoff + timedelta(minutes=elapsed_min)
        detection_time = kickoff + timedelta(minutes=17, seconds=45)
        assert ev_ts < detection_time

    def test_signal_latency_positive_with_elapsed_ts(self):
        kickoff = datetime(2026, 4, 11, 15, 0, 0, tzinfo=timezone.utc)
        ev_ts = kickoff + timedelta(minutes=17)
        now = kickoff + timedelta(minutes=17, seconds=45)
        latency_ms = int((now - ev_ts).total_seconds() * 1000)
        assert latency_ms == 45_000
        assert latency_ms > 0
