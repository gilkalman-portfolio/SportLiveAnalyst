"""
Unit tests for detect_signal() in liveanalyst/worker.py.

detect_signal() is a pure function — no DB, no API, no mocking required.
These tests verify that the extraction from _process_fixture() preserved
every block rule and confidence penalty exactly.

Run with:
    PYTHONPATH=src pytest tests/test_detect_signal.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from liveanalyst.domain import Probabilities, SignalContext
from liveanalyst.worker import detect_signal


# ------------------------------------------------------------------ helpers

_NOW = datetime(2025, 4, 12, 15, 30, 0, tzinfo=timezone.utc)

# Event timestamp matching _NOW exactly → no latency penalty (abs diff = 0s)
_EV_TS_FRESH = _NOW

# Event timestamp 20 seconds before _NOW → triggers >10s penalty (-0.20)
_EV_TS_STALE = datetime(2025, 4, 12, 15, 29, 40, tzinfo=timezone.utc)


def _probs(home: float, draw: float, away: float) -> Probabilities:
    return Probabilities(home=home, draw=draw, away=away)


def _call(
    *,
    cause: str = "GOAL",
    is_key: bool = True,
    p_prev: Probabilities | None = None,
    p_now: Probabilities | None = None,
    source_latency_ms: int = 500,
    now: datetime = _NOW,
    ev_ts: datetime = _EV_TS_FRESH,
    oscillation_ticks: list | None = None,
    prior_exists: bool = False,
    cooldown_hit: bool = False,
    minute: int = 32,
    fixture_id: int = 99001,
) -> SignalContext | None:
    """Convenience wrapper with sensible defaults for a clear GOAL signal."""
    if p_prev is None:
        p_prev = _probs(0.50, 0.30, 0.20)
    if p_now is None:
        # Default: home moved up 0.15 → clearly HIGH tier (avoids IEEE-754 boundary).
        # 0.60-0.50 = 0.0999...998 in float, which is MEDIUM not HIGH.
        p_now = _probs(0.65, 0.23, 0.12)
    return detect_signal(
        fixture_id=fixture_id,
        minute=minute,
        ev_ts=ev_ts,
        cause=cause,
        is_key=is_key,
        p_prev=p_prev,
        p_now=p_now,
        source_latency_ms=source_latency_ms,
        now=now,
        oscillation_ticks=oscillation_ticks or [],
        prior_exists=prior_exists,
        cooldown_hit=cooldown_hit,
    )


# ------------------------------------------------------------------ returns None below tier

class TestReturnNoneWhenBelowTier:
    def test_tiny_delta_returns_none(self):
        p_prev = _probs(0.50, 0.30, 0.20)
        p_now  = _probs(0.51, 0.295, 0.195)  # delta ≈ 0.01 — below 0.03 threshold
        result = _call(p_prev=p_prev, p_now=p_now)
        assert result is None

    def test_exactly_at_threshold_returns_signal(self):
        p_prev = _probs(0.50, 0.30, 0.20)
        p_now  = _probs(0.53, 0.285, 0.185)  # home diff = 0.03 — exactly LOW tier
        result = _call(p_prev=p_prev, p_now=p_now)
        assert result is not None
        assert result.tier == "LOW"


# ------------------------------------------------------------------ returns correct SignalContext

class TestSignalContextFields:
    def test_returns_signal_context_instance(self):
        assert isinstance(_call(), SignalContext)

    def test_fixture_id_propagated(self):
        sig = _call(fixture_id=42000)
        assert sig.fixture_id == 42000

    def test_minute_propagated(self):
        sig = _call(minute=67)
        assert sig.minute == 67

    def test_tier_high_for_large_delta(self):
        # Default p_prev→p_now gives delta = 0.15 → HIGH
        sig = _call()
        assert sig.tier == "HIGH"

    def test_tier_medium(self):
        p_prev = _probs(0.50, 0.30, 0.20)
        p_now  = _probs(0.57, 0.27, 0.16)   # delta = 0.07 → MEDIUM
        sig = _call(p_prev=p_prev, p_now=p_now)
        assert sig.tier == "MEDIUM"

    def test_primary_outcome_is_home(self):
        sig = _call()  # home moved the most
        assert sig.primary_outcome == "home"

    def test_direction_up_when_home_increased(self):
        sig = _call()
        assert sig.direction == "up"

    def test_direction_down_when_home_decreased(self):
        p_prev = _probs(0.65, 0.22, 0.13)
        p_now  = _probs(0.52, 0.28, 0.20)   # home fell 0.13 → down
        sig = _call(p_prev=p_prev, p_now=p_now)
        assert sig.direction == "down"

    def test_signal_type_is_shift(self):
        assert _call().signal_type == "SHIFT"

    def test_event_ts_propagated(self):
        sig = _call(ev_ts=_EV_TS_FRESH)
        assert sig.event_ts == _EV_TS_FRESH

    def test_source_latency_ms_propagated(self):
        sig = _call(source_latency_ms=1200)
        assert sig.source_latency_ms == 1200

    def test_signal_latency_ms_is_now_minus_ev_ts(self):
        # ev_ts is 20s before now → latency = 20 000 ms
        sig = _call(now=_NOW, ev_ts=_EV_TS_STALE)
        assert sig.signal_latency_ms == 20_000

    def test_cooldown_key_format(self):
        sig = _call(fixture_id=99001, cause="GOAL")
        # direction will be "up" with default probs
        assert sig.cooldown_key == "99001:GOAL:up"

    def test_block_reason_none_when_actionable(self):
        sig = _call()
        assert sig.blocked is False
        assert sig.actionable is True
        assert sig.block_reason is None

    def test_confidence_starts_at_1(self):
        # No penalties: fresh ev_ts, low source latency, GOAL, no oscillation
        sig = _call(source_latency_ms=500, ev_ts=_EV_TS_FRESH)
        assert sig.confidence == 1.0


# ------------------------------------------------------------------ block rules

class TestBlockRules:
    def test_unsupported_cause_blocked(self):
        sig = _call(cause="SUBSTITUTION")
        assert sig.blocked is True
        assert "unsupported_cause" in sig.block_reason

    def test_lineup_non_key_player_blocked(self):
        sig = _call(cause="LINEUP_KEY_PLAYER_OUT", is_key=False)
        assert sig.blocked is True
        assert "lineup_player_not_key" in sig.block_reason

    def test_minute_88_blocked(self):
        sig = _call(minute=88)
        assert sig.blocked is True
        assert "minute_gte_88" in sig.block_reason

    def test_minute_87_not_blocked_by_minute_rule(self):
        sig = _call(minute=87)
        # minute rule should not fire; signal is actionable with default params
        assert sig.blocked is False

    def test_prior_same_direction_blocks(self):
        sig = _call(prior_exists=True)
        assert sig.blocked is True
        assert "prior_same_direction_move" in sig.block_reason

    def test_cooldown_blocks(self):
        sig = _call(cooldown_hit=True)
        assert sig.blocked is True
        assert "cooldown_300s" in sig.block_reason

    def test_multiple_block_reasons_joined(self):
        sig = _call(minute=89, prior_exists=True, cooldown_hit=True)
        assert sig.blocked is True
        reasons = sig.block_reason.split(",")
        assert "minute_gte_88" in reasons
        assert "prior_same_direction_move" in reasons
        assert "cooldown_300s" in reasons

    def test_allowed_causes_not_blocked_by_cause_rule(self):
        for cause in ("GOAL", "RED_CARD"):
            sig = _call(cause=cause)
            assert sig.blocked is False, f"{cause} should not be blocked"

    def test_lineup_key_player_not_blocked(self):
        sig = _call(cause="LINEUP_KEY_PLAYER_OUT", is_key=True)
        assert sig.blocked is False


# ------------------------------------------------------------------ confidence penalties

class TestConfidencePenalties:
    def test_high_source_latency_penalises(self):
        # source_latency_ms > 30 000 → -0.25
        sig = _call(source_latency_ms=31_000, ev_ts=_EV_TS_FRESH)
        assert abs(sig.confidence - 0.75) < 1e-9

    def test_stale_ev_ts_penalises(self):
        # abs(now - ev_ts) > 10s → -0.20
        sig = _call(ev_ts=_EV_TS_STALE, source_latency_ms=500)
        assert abs(sig.confidence - 0.80) < 1e-9

    def test_lineup_non_key_penalises(self):
        # is_key=False with LINEUP_KEY_PLAYER_OUT → -0.20 confidence penalty
        # (signal will also be blocked, but confidence is still computed)
        sig = _call(cause="LINEUP_KEY_PLAYER_OUT", is_key=False, ev_ts=_EV_TS_FRESH)
        assert sig.confidence <= 0.80

    def test_oscillating_market_penalises(self):
        # 3 ticks where swing >0.02 AND last step >0.01 → -0.20
        osc = [
            {"p_home": 0.50, "p_draw": 0.30, "p_away": 0.20},
            {"p_home": 0.515, "p_draw": 0.295, "p_away": 0.19},  # swings up
            {"p_home": 0.47, "p_draw": 0.31, "p_away": 0.22},    # swings down >0.01
        ]
        sig = _call(oscillation_ticks=osc, ev_ts=_EV_TS_FRESH, source_latency_ms=500)
        assert sig.confidence <= 0.80

    def test_no_oscillation_no_penalty(self):
        # 3 stable ticks — swing ≤0.02 → no penalty
        osc = [
            {"p_home": 0.50, "p_draw": 0.30, "p_away": 0.20},
            {"p_home": 0.501, "p_draw": 0.30, "p_away": 0.199},
            {"p_home": 0.502, "p_draw": 0.299, "p_away": 0.199},
        ]
        sig = _call(oscillation_ticks=osc, ev_ts=_EV_TS_FRESH, source_latency_ms=500)
        assert sig.confidence == 1.0

    def test_combined_penalties_can_drop_below_threshold_and_block(self):
        # source latency >30k (-0.25) + stale ev_ts (-0.20) → confidence 0.55 < 0.60
        sig = _call(source_latency_ms=35_000, ev_ts=_EV_TS_STALE)
        assert sig.confidence < 0.60
        assert sig.blocked is True
        assert "confidence_lt_0.6" in sig.block_reason

    def test_confidence_clamped_to_zero_minimum(self):
        # Pile on every penalty; result must not go negative
        osc = [
            {"p_home": 0.50, "p_draw": 0.30, "p_away": 0.20},
            {"p_home": 0.52, "p_draw": 0.29, "p_away": 0.19},
            {"p_home": 0.49, "p_draw": 0.31, "p_away": 0.20},
        ]
        sig = _call(
            cause="LINEUP_KEY_PLAYER_OUT",
            is_key=False,
            source_latency_ms=40_000,
            ev_ts=_EV_TS_STALE,
            oscillation_ticks=osc,
        )
        assert sig.confidence >= 0.0


# ------------------------------------------------------------------ cause_confidence propagation

class TestCauseConfidencePropagation:
    def test_goal_cause_confidence_is_1(self):
        sig = _call(cause="GOAL")
        assert sig.cause_confidence == 1.0

    def test_unknown_cause_confidence_is_0(self):
        # cause_confidence for unsupported cause = 0.0
        # Signal will be blocked, but field is still set
        sig = _call(cause="VAR")
        assert sig.cause_confidence == 0.0
