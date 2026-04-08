CREATE TABLE IF NOT EXISTS market_ticks (
    id BIGSERIAL PRIMARY KEY,
    fixture_id BIGINT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    minute INTEGER,
    home_odds DOUBLE PRECISION NOT NULL,
    draw_odds DOUBLE PRECISION NOT NULL,
    away_odds DOUBLE PRECISION NOT NULL,
    p_home DOUBLE PRECISION NOT NULL,
    p_draw DOUBLE PRECISION NOT NULL,
    p_away DOUBLE PRECISION NOT NULL,
    source_latency_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_ticks_fixture_ts ON market_ticks (fixture_id, ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    fixture_id BIGINT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    minute INTEGER,
    event_type TEXT NOT NULL,
    team_side TEXT,
    player_name TEXT,
    is_key_player BOOLEAN NOT NULL DEFAULT FALSE,
    raw_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_fixture_ts ON events (fixture_id, ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    fixture_id BIGINT NOT NULL,
    ts_created TIMESTAMPTZ NOT NULL,
    minute INTEGER,
    signal_type TEXT NOT NULL,
    tier TEXT NOT NULL,
    primary_outcome TEXT NOT NULL,
    direction TEXT NOT NULL,
    p_prev DOUBLE PRECISION NOT NULL,
    p_now DOUBLE PRECISION NOT NULL,
    delta_abs DOUBLE PRECISION NOT NULL,
    cause_type TEXT NOT NULL,
    cause_confidence DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    actionable BOOLEAN NOT NULL,
    blocked BOOLEAN NOT NULL,
    block_reason TEXT,
    telegram_sent BOOLEAN NOT NULL DEFAULT FALSE,
    cooldown_key TEXT NOT NULL,
    event_ts TIMESTAMPTZ,
    signal_latency_ms INTEGER NOT NULL,
    source_latency_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_cooldown_ts ON signals (cooldown_key, ts_created DESC);
CREATE INDEX IF NOT EXISTS idx_signals_fixture_ts ON signals (fixture_id, ts_created DESC);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    time_to_move INTEGER,
    max_move_within_120s DOUBLE PRECISION NOT NULL,
    reversed_within_120s BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_status ON signal_outcomes (status);
