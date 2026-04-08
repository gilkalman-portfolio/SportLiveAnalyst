from dataclasses import dataclass
from datetime import datetime


@dataclass
class Probabilities:
    home: float
    draw: float
    away: float


@dataclass
class MarketTick:
    fixture_id: int
    ts: datetime
    minute: int
    home_odds: float
    draw_odds: float
    away_odds: float
    p_home: float
    p_draw: float
    p_away: float
    source_latency_ms: int


@dataclass
class SignalContext:
    fixture_id: int
    ts_created: datetime
    minute: int
    primary_outcome: str
    direction: str
    p_prev: float
    p_now: float
    delta_abs: float
    cause_type: str
    cause_confidence: float
    confidence: float
    actionable: bool
    blocked: bool
    block_reason: str | None
    cooldown_key: str
    event_ts: datetime | None
    signal_latency_ms: int
    source_latency_ms: int
    tier: str
    signal_type: str = "SHIFT"
