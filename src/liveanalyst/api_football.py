from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


class APIFootballClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": api_key})

    def _get(self, path: str, **params) -> dict[str, Any]:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_live_premier_league_fixture(self, league_id: int, season: int) -> dict[str, Any] | None:
        data = self._get("/fixtures", live="all", league=league_id, season=season)
        response = data.get("response", [])
        return response[0] if response else None

    def get_odds_1x2(self, fixture_id: int) -> tuple[float, float, float, int] | None:
        data = self._get("/odds/live", fixture=fixture_id)
        response = data.get("response", [])
        if not response:
            return None
        bookmakers = response[0].get("bookmakers", [])
        if not bookmakers:
            return None
        bets = bookmakers[0].get("bets", [])
        target = next((b for b in bets if b.get("name") == "Match Winner"), None)
        if not target:
            return None

        vals = {v["value"]: float(v["odd"]) for v in target.get("values", []) if "odd" in v}
        home = vals.get("Home")
        draw = vals.get("Draw")
        away = vals.get("Away")
        if not all([home, draw, away]):
            return None

        now = datetime.now(timezone.utc)
        source_ts = datetime.fromisoformat(response[0]["update"].replace("Z", "+00:00"))
        latency_ms = int((now - source_ts).total_seconds() * 1000)
        return home, draw, away, max(latency_ms, 0)

    def get_fixture_events(self, fixture_id: int) -> list[dict[str, Any]]:
        data = self._get("/fixtures/events", fixture=fixture_id)
        return data.get("response", [])

    def get_fixture_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        data = self._get("/fixtures/lineups", fixture=fixture_id)
        return data.get("response", [])

    def get_standings(self, league_id: int, season: int) -> list[dict[str, Any]]:
        data = self._get("/standings", league=league_id, season=season)
        response = data.get("response", [])
        if not response:
            return []
        league_data = response[0].get("league", {})
        standings = league_data.get("standings", [])
        return standings[0] if standings else []
