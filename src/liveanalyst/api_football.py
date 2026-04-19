from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


RATE_LIMIT_ALERT_THRESHOLD = 100  # Telegram alert when daily quota drops below this

# Quota watcher thresholds (calls per 60-second window)
_BURST_WINDOW_S    = 60
_BURST_WARN_CALLS  = 40
_BURST_CRIT_CALLS  = 70


class QuotaWatcher:
    """Tracks API call rate and logs warnings when bursts are detected."""

    def __init__(self) -> None:
        self._calls: deque[tuple[float, str]] = deque()

    def record(self, endpoint: str) -> None:
        now = time.monotonic()
        self._calls.append((now, endpoint))
        self._trim(now)
        count = len(self._calls)
        if count >= _BURST_CRIT_CALLS:
            log.warning(
                "quota_watcher: CRITICAL — %d calls in last 60s | %s",
                count, self._breakdown(),
            )
        elif count >= _BURST_WARN_CALLS:
            log.warning(
                "quota_watcher: HIGH — %d calls in last 60s | %s",
                count, self._breakdown(),
            )

    def _trim(self, now: float) -> None:
        cutoff = now - _BURST_WINDOW_S
        while self._calls and self._calls[0][0] < cutoff:
            self._calls.popleft()

    def _breakdown(self) -> str:
        counts: dict[str, int] = {}
        for _, ep in self._calls:
            counts[ep] = counts.get(ep, 0) + 1
        return ", ".join(
            f"{ep}×{n}" for ep, n in sorted(counts.items(), key=lambda x: -x[1])
        )


class APIFootballClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": api_key})
        self.rate_limit_remaining: int | None = None  # updated after every request
        self._watcher = QuotaWatcher()

    def _get(self, path: str, **params) -> dict[str, Any]:
        self._watcher.record(path)
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        limit = resp.headers.get("x-ratelimit-requests-limit")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
            log.info("api_quota remaining=%s/%s endpoint=%s", remaining, limit, path)
        return resp.json()

    def get_live_fixtures(self, league_ids: tuple) -> list[dict[str, Any]]:
        """Fetch all live fixtures and filter by league_ids locally — 1 request regardless of league count."""
        data = self._get("/fixtures", live="all")
        all_fixtures = data.get("response", [])
        return [f for f in all_fixtures if f.get("league", {}).get("id") in league_ids]

    def get_odds_1x2(self, fixture_id: int) -> tuple[float, float, float, int] | None:
        data = self._get("/odds/live", fixture=fixture_id)
        response = data.get("response", [])
        if not response:
            return None

        odds_list = response[0].get("odds", [])
        target = next((o for o in odds_list if o.get("name") in ("Fulltime Result", "Match Winner")), None)
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

<<<<<<< Updated upstream
    def get_standings(self, league_id: int, season: int) -> list[dict[str, Any]]:
        data = self._get("/standings", league=league_id, season=season)
        response = data.get("response", [])
        if not response:
            return []
        league_data = response[0].get("league", {})
        standings = league_data.get("standings", [])
        return standings[0] if standings else []

    def get_standings_by_round(self, league_id: int, season: int, round_num: int) -> list[dict[str, Any]]:
        data = self._get("/standings", league=league_id, season=season, round=round_num)
        response = data.get("response", [])
        if not response:
            return []
        league_data = response[0].get("league", {})
        standings = league_data.get("standings", [])
        return standings[0] if standings else []

    def get_fixture_info(self, fixture_id: int) -> dict[str, Any] | None:
        data = self._get("/fixtures", id=fixture_id)
        response = data.get("response", [])
        if not response:
            return None
        f = response[0]
        round_str = f.get("league", {}).get("round", "")
        round_num = _parse_round(round_str)
        return {
            "fixture_id": fixture_id,
            "league_id": f["league"]["id"],
            "season": f["league"]["season"],
            "round": round_num,
            "home_team_id": f["teams"]["home"]["id"],
            "away_team_id": f["teams"]["away"]["id"],
        }

    def get_team_recent_form(self, team_id: int, season: int, venue: str, last: int = 5) -> list[str]:
        """Return last N results ('W'/'D'/'L') for home or away games, most recent first."""
        params: dict[str, Any] = {"team": team_id, "season": season, "last": last * 2}
        if venue in ("home", "away"):
            params["venue"] = venue
        data = self._get("/fixtures", **params)
        results = []
        for f in reversed(data.get("response", [])):
            goals = f.get("goals", {})
            teams = f.get("teams", {})
            is_home = teams.get("home", {}).get("id") == team_id
            g_for     = goals.get("home") if is_home else goals.get("away")
            g_against = goals.get("away") if is_home else goals.get("home")
            if g_for is None or g_against is None:
                continue
            if g_for > g_against:
                results.append("W")
            elif g_for < g_against:
                results.append("L")
            else:
                results.append("D")
            if len(results) == last:
                break
        return list(reversed(results))  # most recent first


def _parse_round(round_str: str) -> int:
    """Extract integer round from strings like 'Regular Season - 24'."""
    import re
    m = re.search(r"(\d+)$", round_str)
    return int(m.group(1)) if m else 0
=======
    def get_fixtures_by_date(self, date: str, league_ids: tuple, season: int) -> list[dict[str, Any]]:
        """Fetch scheduled fixtures for a specific date across all configured leagues."""
        results = []
        for league_id in league_ids:
            data = self._get("/fixtures", date=date, league=league_id, season=season)
            results.extend(data.get("response", []))
        return results

    def get_prematch_odds(self, fixture_id: int, bookmaker_id: int = 8) -> tuple[float, float, float] | None:
        """Fetch pre-match 1X2 odds. Tries bookmaker_id first, then falls back to any available bookmaker."""
        # Try without bookmaker filter first to maximise coverage
        for params in (
            {"fixture": fixture_id, "bookmaker": bookmaker_id},
            {"fixture": fixture_id},
        ):
            data = self._get("/odds", **params)
            response = data.get("response", [])
            if not response:
                continue
            for bm in response[0].get("bookmakers", []):
                target = next(
                    (b for b in bm.get("bets", []) if b.get("name") in ("Match Winner", "Fulltime Result")),
                    None,
                )
                if not target:
                    continue
                vals = {v["value"]: float(v["odd"]) for v in target.get("values", []) if "odd" in v}
                home = vals.get("Home")
                draw = vals.get("Draw")
                away = vals.get("Away")
                if all([home, draw, away]):
                    return home, draw, away
        return None

    def get_team_form(self, team_id: int, league_id: int, season: int, last: int = 5) -> list[dict[str, Any]]:
        """Last N completed fixtures for a team in the given league."""
        data = self._get("/fixtures", team=team_id, league=league_id, season=season, last=last, status="FT")
        return data.get("response", [])

    def get_h2h(self, home_team_id: int, away_team_id: int, last: int = 5) -> list[dict[str, Any]]:
        """Head-to-head results between two teams."""
        data = self._get("/fixtures/headtohead", h2h=f"{home_team_id}-{away_team_id}", last=last, status="FT")
        return data.get("response", [])

    def get_scheduled_fixtures(self, date: str, league_ids: tuple, season: int) -> list[dict[str, Any]]:
        """Fixtures scheduled (not yet started) for a given date."""
        results = []
        for league_id in league_ids:
            data = self._get("/fixtures", date=date, league=league_id, season=season, status="NS")
            results.extend(data.get("response", []))
        return results

    def get_fixture_injuries(self, fixture_id: int) -> list[dict[str, Any]]:
        """Injured and suspended players for a fixture."""
        data = self._get("/injuries", fixture=fixture_id)
        return data.get("response", [])

    def get_league_injuries(self, league_id: int, season: int, date: str) -> list[dict[str, Any]]:
        """All injuries/suspensions for a league on a given date — 1 call per league."""
        data = self._get("/injuries", league=league_id, season=season, date=date)
        return data.get("response", [])

    def get_api_predictions(self, fixture_id: int) -> dict[str, Any] | None:
        """API prediction data — includes form (last_5) and H2H in one call."""
        data = self._get("/predictions", fixture=fixture_id)
        response = data.get("response", [])
        return response[0] if response else None

    def get_team_statistics(self, team_id: int, league_id: int, season: int) -> dict[str, Any] | None:
        """Team statistics for the season: goals scored/conceded by venue, form, fixtures played."""
        data = self._get("/teams/statistics", team=team_id, league=league_id, season=season)
        response = data.get("response")
        return response if isinstance(response, dict) else None

    def get_standings(self, league_id: int, season: int) -> list[dict[str, Any]]:
        """League standings — returns the flat list of team standing entries."""
        data = self._get("/standings", league=league_id, season=season)
        try:
            return data["response"][0]["league"]["standings"][0]
        except (IndexError, KeyError, TypeError):
            return []

    def get_fixture_result(self, fixture_id: int) -> dict[str, Any] | None:
        """Fetch a finished fixture to get the final score — used by backtest pipeline."""
        data = self._get("/fixtures", id=fixture_id)
        response = data.get("response", [])
        return response[0] if response else None

    def get_player_statistics(self, player_id: int, league_id: int, season: int) -> dict[str, Any] | None:
        """Player season statistics — used for injury importance weighting."""
        data = self._get("/players", id=player_id, league=league_id, season=season)
        response = data.get("response", [])
        return response[0] if response else None

    def extract_events_from_fixture(self, fixture: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract events already embedded in the live fixture response — no extra API call."""
        return fixture.get("events", [])
>>>>>>> Stashed changes
