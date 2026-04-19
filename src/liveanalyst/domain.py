from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SeasonStake(str, Enum):
    TITLE = "TITLE"
    CHAMPIONS_LEAGUE = "CHAMPIONS_LEAGUE"
    EUROPA_LEAGUE = "EUROPA_LEAGUE"
    CONFERENCE = "CONFERENCE"
    MID_TABLE = "MID_TABLE"
    RELEGATION = "RELEGATION"
    RELEGATED = "RELEGATED"
    SECURED_SAFE = "SECURED_SAFE"


@dataclass
class TeamStanding:
    team_id: int
    position: int
    points: int
    games_played: int
    season_total_games: int

    @property
    def games_remaining(self) -> int:
        return self.season_total_games - self.games_played


@dataclass
class MotivationFactor:
    home_stake: SeasonStake
    away_stake: SeasonStake
    home_motivation: float
    away_motivation: float
    home_games_remaining: int
    away_games_remaining: int


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
    league_id: int | None = None


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
    home_motivation: float | None = None
    away_motivation: float | None = None
    home_stake: SeasonStake | None = None
    away_stake: SeasonStake | None = None
    league_id: int | None = None
