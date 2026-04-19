-- 003_add_league_id.sql
-- Adds league_id to market_ticks, events, and signals for multi-league support.
ALTER TABLE market_ticks ADD COLUMN IF NOT EXISTS league_id INTEGER;
ALTER TABLE events       ADD COLUMN IF NOT EXISTS league_id INTEGER;
ALTER TABLE signals      ADD COLUMN IF NOT EXISTS league_id INTEGER;

-- Index for filtering by league
CREATE INDEX IF NOT EXISTS idx_market_ticks_league ON market_ticks (league_id);
CREATE INDEX IF NOT EXISTS idx_events_league       ON events (league_id);
CREATE INDEX IF NOT EXISTS idx_signals_league      ON signals (league_id);
