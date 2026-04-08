-- last_tick_per_fixture
SELECT DISTINCT ON (fixture_id)
    id,
    fixture_id,
    ts,
    minute,
    home_odds,
    draw_odds,
    away_odds,
    p_home,
    p_draw,
    p_away,
    source_latency_ms
FROM market_ticks
ORDER BY fixture_id, ts DESC;

-- last_signal_per_cooldown_key
SELECT DISTINCT ON (cooldown_key)
    id,
    fixture_id,
    ts_created,
    minute,
    signal_type,
    tier,
    primary_outcome,
    direction,
    p_prev,
    p_now,
    delta_abs,
    cause_type,
    cause_confidence,
    confidence,
    actionable,
    blocked,
    block_reason,
    telegram_sent,
    cooldown_key,
    event_ts,
    signal_latency_ms,
    source_latency_ms
FROM signals
ORDER BY cooldown_key, ts_created DESC;
