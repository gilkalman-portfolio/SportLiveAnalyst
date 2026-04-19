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

-- ============================================================
-- Saturday inspection queries
-- Replace :fixture_id with the actual fixture ID before running.
-- ============================================================

-- last_20_ticks_for_fixture
SELECT id, ts, minute, home_odds, draw_odds, away_odds,
       p_home, p_draw, p_away, source_latency_ms
FROM market_ticks
WHERE fixture_id = :fixture_id
ORDER BY ts DESC
LIMIT 20;

-- all_events_for_fixture
SELECT id, ts, minute, event_type, team_side, player_name, is_key_player
FROM events
WHERE fixture_id = :fixture_id
ORDER BY ts ASC;

-- all_signals_for_fixture
SELECT id, ts_created, minute, tier, cause_type, primary_outcome, direction,
       p_prev, p_now, delta_abs, confidence, blocked, block_reason,
       actionable, telegram_sent, signal_latency_ms, source_latency_ms
FROM signals
WHERE fixture_id = :fixture_id
ORDER BY ts_created ASC;

-- all_signal_outcomes_for_fixture
SELECT s.id              AS signal_id,
       s.ts_created,
       s.minute,
       s.tier,
       s.cause_type,
       s.primary_outcome,
       s.direction,
       s.delta_abs,
       s.confidence,
       so.status,
       so.time_to_move,
       so.max_move_within_120s,
       so.reversed_within_120s
FROM signals s
JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.fixture_id = :fixture_id
ORDER BY s.ts_created ASC;

-- blocked_signals_by_reason  (all fixtures)
SELECT block_reason,
       COUNT(*) AS total
FROM signals
WHERE blocked = TRUE
GROUP BY block_reason
ORDER BY total DESC;

-- avg_signal_latency_by_fixture
SELECT fixture_id,
       COUNT(*)                          AS signal_count,
       ROUND(AVG(signal_latency_ms))     AS avg_signal_latency_ms,
       ROUND(MIN(signal_latency_ms))     AS min_signal_latency_ms,
       ROUND(MAX(signal_latency_ms))     AS max_signal_latency_ms
FROM signals
GROUP BY fixture_id
ORDER BY fixture_id;

-- avg_source_latency_by_fixture
SELECT fixture_id,
       COUNT(*)                          AS tick_count,
       ROUND(AVG(source_latency_ms))     AS avg_source_latency_ms,
       ROUND(MIN(source_latency_ms))     AS min_source_latency_ms,
       ROUND(MAX(source_latency_ms))     AS max_source_latency_ms
FROM market_ticks
GROUP BY fixture_id
ORDER BY fixture_id;

-- outcome_counts_by_fixture
SELECT s.fixture_id,
       COUNT(*)                                                        AS total_signals,
       COUNT(*) FILTER (WHERE so.status = 'confirmed')                AS confirmed,
       COUNT(*) FILTER (WHERE so.status = 'failed')                   AS failed,
       COUNT(*) FILTER (WHERE so.status = 'neutral')                  AS neutral,
       COUNT(*) FILTER (WHERE so.status = 'insufficient_data')        AS insufficient_data,
       COUNT(*) FILTER (WHERE so.signal_id IS NULL)                   AS no_outcome_yet
FROM signals s
LEFT JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
GROUP BY s.fixture_id
ORDER BY s.fixture_id;

-- latency_breakdown_analysis
-- Shows the full event→signal→market latency chain for actionable signals.
-- Positive lead_time_ms = our signal fired BEFORE the market repriced.
-- Negative = market already moved before (or at) our detection.
SELECT
    s.id                                        AS signal_id,
    s.fixture_id,
    s.minute,
    s.tier,
    s.cause_type,
    s.signal_latency_ms,                        -- event → our detection (ms)
    so.event_to_odds_ms,                        -- event → market >=2% move (ms)
    so.event_to_odds_ms - s.signal_latency_ms   AS lead_time_ms,
    so.status,
    so.max_move_within_120s
FROM signals s
JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
  AND so.event_to_odds_ms IS NOT NULL
ORDER BY s.ts_created DESC;

-- latency_breakdown_by_tier
-- Summary per tier: are we ahead or behind the market on average?
SELECT
    s.tier,
    COUNT(*)                                                            AS n,
    ROUND(AVG(s.signal_latency_ms))                                    AS avg_signal_latency_ms,
    ROUND(AVG(so.event_to_odds_ms))                                    AS avg_event_to_odds_ms,
    ROUND(AVG(so.event_to_odds_ms - s.signal_latency_ms))             AS avg_lead_time_ms,
    COUNT(*) FILTER (WHERE so.event_to_odds_ms > s.signal_latency_ms) AS ahead_of_market,
    COUNT(*) FILTER (WHERE so.event_to_odds_ms <= s.signal_latency_ms) AS behind_market
FROM signals s
JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
  AND so.event_to_odds_ms IS NOT NULL
GROUP BY s.tier
ORDER BY s.tier;

-- early_vs_late_signals
-- Compares signal quality between first half (1-45') and second half (46-87').
SELECT CASE
           WHEN s.minute BETWEEN 1  AND 45 THEN '1st half (1-45)'
           WHEN s.minute BETWEEN 46 AND 87 THEN '2nd half (46-87)'
           ELSE 'other'
       END                                                             AS half,
       COUNT(*)                                                        AS total_signals,
       ROUND(AVG(s.delta_abs)::numeric, 4)                            AS avg_delta,
       ROUND(AVG(s.confidence)::numeric, 3)                           AS avg_confidence,
       COUNT(*) FILTER (WHERE so.status = 'confirmed')                AS confirmed,
       COUNT(*) FILTER (WHERE so.status = 'failed')                   AS failed,
       COUNT(*) FILTER (WHERE so.status = 'neutral')                  AS neutral
FROM signals s
LEFT JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
GROUP BY half
ORDER BY half;
