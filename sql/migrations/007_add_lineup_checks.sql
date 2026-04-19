ALTER TABLE prematch_predictions
    ADD COLUMN IF NOT EXISTS lineup_check_sent BOOLEAN NOT NULL DEFAULT FALSE;
