CREATE TABLE IF NOT EXISTS team_standings (
    team_id     INT NOT NULL,
    league_id   INT NOT NULL,
    season      INT NOT NULL,
    position    INT NOT NULL,
    points      INT NOT NULL,
    games_played INT NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, league_id, season)
);

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS home_motivation DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS away_motivation DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS home_stake TEXT,
    ADD COLUMN IF NOT EXISTS away_stake TEXT;
