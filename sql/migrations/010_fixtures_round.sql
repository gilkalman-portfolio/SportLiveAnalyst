-- Fixture metadata table (round, teams) for motivation backfill
CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id   BIGINT PRIMARY KEY,
    league_id    INT NOT NULL,
    season       INT NOT NULL,
    round        INT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL
);

-- Add round column to team_standings (0 = live/current snapshot)
ALTER TABLE team_standings ADD COLUMN IF NOT EXISTS round INT NOT NULL DEFAULT 0;

-- Migrate PK to include round
ALTER TABLE team_standings DROP CONSTRAINT IF EXISTS team_standings_pkey;
ALTER TABLE team_standings ADD PRIMARY KEY (team_id, league_id, season, round);
