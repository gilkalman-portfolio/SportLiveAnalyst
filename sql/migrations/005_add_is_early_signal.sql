-- Migration 005: add is_early_signal column to signal_outcomes
-- Run: psql $POSTGRES_DSN -f sql/migrations/005_add_is_early_signal.sql

ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS is_early_signal BOOLEAN;
