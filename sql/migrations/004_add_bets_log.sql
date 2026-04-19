-- Migration 004: bets_log table for CLV tracking
-- Run: psql $POSTGRES_DSN -f sql/migrations/004_add_bets_log.sql

CREATE TABLE IF NOT EXISTS bets_log (
    id               SERIAL PRIMARY KEY,
    fixture_id       INTEGER        NOT NULL,
    league_id        INTEGER,
    ts_prediction    TIMESTAMPTZ    NOT NULL,
    primary_outcome  VARCHAR(10)    NOT NULL,  -- 'home' | 'draw' | 'away'
    prob_model       FLOAT          NOT NULL,  -- p מהמודל שלנו
    odds_taken       FLOAT,                    -- אודס שלקחנו (NULL אם לא הומר)
    odds_open        FLOAT,                    -- אודס בפתיחת שוק
    odds_closing     FLOAT,                    -- אודס סגירה (CLV base)
    clv              FLOAT                     -- odds_closing / odds_taken - 1 (חיובי = טוב)
                     GENERATED ALWAYS AS (
                         CASE WHEN odds_taken IS NOT NULL AND odds_closing IS NOT NULL
                              THEN odds_closing / odds_taken - 1
                              ELSE NULL
                         END
                     ) STORED,
    result           SMALLINT,                 -- 1=ניצחנו, 0=הפסדנו, NULL=טרם הוכרע
    signal_id        INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS bets_log_fixture_idx  ON bets_log (fixture_id);
CREATE INDEX IF NOT EXISTS bets_log_league_idx   ON bets_log (league_id);
CREATE INDEX IF NOT EXISTS bets_log_ts_idx       ON bets_log (ts_prediction);
