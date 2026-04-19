-- Enrichment columns for prematch_predictions (xG, home/away form split, DC probs, under_over)
ALTER TABLE prematch_predictions
    ADD COLUMN IF NOT EXISTS under_over         TEXT,
    ADD COLUMN IF NOT EXISTS form_home_home     FLOAT,
    ADD COLUMN IF NOT EXISTS form_home_away     FLOAT,
    ADD COLUMN IF NOT EXISTS form_away_home     FLOAT,
    ADD COLUMN IF NOT EXISTS form_away_away     FLOAT,
    ADD COLUMN IF NOT EXISTS mu_home            FLOAT,
    ADD COLUMN IF NOT EXISTS mu_away            FLOAT,
    ADD COLUMN IF NOT EXISTS p_home_dc          FLOAT,
    ADD COLUMN IF NOT EXISTS p_draw_dc          FLOAT,
    ADD COLUMN IF NOT EXISTS p_away_dc          FLOAT,
    ADD COLUMN IF NOT EXISTS standings_gap      INT,
    ADD COLUMN IF NOT EXISTS injury_penalty_home FLOAT,
    ADD COLUMN IF NOT EXISTS injury_penalty_away FLOAT;

-- Backtest / ground-truth outcomes table
CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id                  SERIAL PRIMARY KEY,
    fixture_id          INT NOT NULL UNIQUE,
    prediction_id       INT REFERENCES prematch_predictions(id) ON DELETE SET NULL,
    actual_home_goals   INT,
    actual_away_goals   INT,
    actual_outcome      TEXT,           -- 'home' | 'draw' | 'away'
    predicted_outcome   TEXT,
    predicted_confidence FLOAT,
    brier_score         FLOAT,          -- (1 - p_correct)^2
    settled_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_settled_at ON prediction_outcomes (settled_at DESC);
