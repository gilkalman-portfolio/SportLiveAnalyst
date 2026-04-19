from __future__ import annotations

import logging
from datetime import datetime, timezone

from liveanalyst.api_football import APIFootballClient
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.domain import PreMatchPrediction, SeasonStake, TeamStanding
from liveanalyst.logic import classify_stake, compute_motivation, compute_prematch_prediction, key_player_from_lineup_player


class PreMatchEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.postgres_dsn)
        self.api = APIFootballClient(settings.api_football_base_url, settings.api_football_key)

    def predict(self, fixture_id: int) -> PreMatchPrediction | None:
        fixture_info = self.db.get_fixture_info(fixture_id)
        if not fixture_info:
            raw = self.api.get_fixture_info(fixture_id)
            if not raw:
                logging.warning("prematch: fixture %s not found", fixture_id)
                return None
            self.db.upsert_fixture_info(**raw)
            fixture_info = raw

        home_id   = fixture_info["home_team_id"]
        away_id   = fixture_info["away_team_id"]
        league_id = fixture_info["league_id"]
        season    = fixture_info["season"]
        round_num = fixture_info["round"]

        # ── Odds ─────────────────────────────────────────────────────────
        odds = self.api.get_odds_1x2(fixture_id)
        if not odds:
            logging.warning("prematch: no odds for fixture %s", fixture_id)
            return None
        home_odds, draw_odds, away_odds, _ = odds

        # ── Motivation ───────────────────────────────────────────────────
        standings = self.db.get_standings_for_round(home_id, away_id, league_id, season, round_num)
        if len(standings) < 2:
            rows = self.api.get_standings_by_round(league_id, season, round_num)
            for entry in rows:
                self.db.upsert_team_standing_for_round(
                    team_id=entry["team"]["id"],
                    league_id=league_id,
                    season=season,
                    round_num=round_num,
                    position=entry["rank"],
                    points=entry["points"],
                    games_played=entry["all"]["played"],
                )
            standings = self.db.get_standings_for_round(home_id, away_id, league_id, season, round_num)

        home_motivation = away_motivation = None
        home_stake = away_stake = None
        if len(standings) >= 2:
            home_row, away_row = standings[0], standings[1]
            home_s = TeamStanding(home_row["team_id"], home_row["position"], home_row["points"], home_row["games_played"], 38)
            away_s = TeamStanding(away_row["team_id"], away_row["position"], away_row["points"], away_row["games_played"], 38)
            home_stake = classify_stake(home_s, league_id)
            away_stake = classify_stake(away_s, league_id)
            home_motivation = compute_motivation(home_stake, home_s.games_remaining)
            away_motivation = compute_motivation(away_stake, away_s.games_remaining)

        # ── Form (home/away split, last 5) ───────────────────────────────
        home_form = self.api.get_team_recent_form(home_id, season, venue="home", last=5)
        away_form = self.api.get_team_recent_form(away_id, season, venue="away", last=5)

        # ── Injuries (key players only) ───────────────────────────────────
        home_absences = self._count_key_absences(fixture_id, home_id)
        away_absences = self._count_key_absences(fixture_id, away_id)

        return compute_prematch_prediction(
            fixture_id=fixture_id,
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            home_motivation=home_motivation,
            away_motivation=away_motivation,
            home_stake=home_stake,
            away_stake=away_stake,
            home_form_results=home_form,
            away_form_results=away_form,
            home_key_absences=home_absences,
            away_key_absences=away_absences,
        )

    def _count_key_absences(self, fixture_id: int, team_id: int) -> int:
        data = self.api._get("/injuries", fixture=fixture_id, team=team_id)
        count = 0
        for entry in data.get("response", []):
            player = entry.get("player", {})
            if key_player_from_lineup_player({"pos": player.get("pos"), "statistics": {}}):
                count += 1
        return count
