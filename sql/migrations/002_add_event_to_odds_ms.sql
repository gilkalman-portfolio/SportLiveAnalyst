-- 002_add_event_to_odds_ms.sql
-- Adds event_to_odds_ms to signal_outcomes.
-- Measures milliseconds from event_ts to the first market_tick where odds
-- moved >=2% in the signal direction. NULL if no such tick exists within
-- the 120-second observation window.
ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS event_to_odds_ms INTEGER;
