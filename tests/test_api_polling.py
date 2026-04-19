"""
API polling interval tests — verifies that the worker respects all polling
intervals and does NOT leak extra API calls.

Key rules verified:
  - /fixtures?live=all  : called once per 60s, not more
  - /odds/live          : called once per 60s (quiet) / 15s (alert), not more
  - /fixtures/lineups   : called once per 900s inside kickoff window only
  - /fixtures/events    : NEVER called (events are embedded in fixtures response)
  - No calls during HT / BT status

All time is controlled via mock — no real sleep.

Run with:
    PYTHONPATH=src pytest tests/test_api_polling.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from liveanalyst.worker import LiveAnalystWorker, FIXTURES_POLL_INTERVAL_S, QUIET_ODDS_INTERVAL_S, ALERT_ODDS_INTERVAL_S, ALERT_DURATION_S, LINEUP_POLL_INTERVAL_S


# ------------------------------------------------------------------ fixtures / helpers

_KICKOFF = datetime(2026, 4, 11, 15, 0, 0, tzinfo=timezone.utc)
_FIXTURE_ID = 111


def _make_fixture(
    fixture_id: int = _FIXTURE_ID,
    league_id: int = 39,
    minute: int = 45,
    status: str = "1H",
    kickoff: datetime = _KICKOFF,
    events: list | None = None,
) -> dict:
    return {
        "fixture": {
            "id": fixture_id,
            "status": {"short": status, "elapsed": minute},
            "date": kickoff.isoformat().replace("+00:00", "Z"),
        },
        "league": {"id": league_id},
        "teams": {
            "home": {"name": "Arsenal"},
            "away": {"name": "Chelsea"},
        },
        "events": events or [],
    }


def _make_worker(now: datetime, fixtures: list | None = None) -> tuple[LiveAnalystWorker, MagicMock]:
    """Return a worker with mocked API + DB. API call counts are on the mock."""
    settings = MagicMock()
    settings.league_ids = (39,)

    api = MagicMock()
    api.get_live_fixtures.return_value = fixtures or [_make_fixture()]
    api.get_odds_1x2.return_value = (2.10, 3.40, 3.60, 500)
    api.get_fixture_lineups.return_value = []
    api.extract_events_from_fixture.return_value = []
    api.rate_limit_remaining = None  # not set during polling tests

    db = MagicMock()
    db.insert_market_tick.return_value = 1
    db.last_tick.return_value = None
    db.prev_tick.return_value = None
    db.get_tick_minutes_ago.return_value = None  # suppresses odds-driven signal path

    from liveanalyst.worker import _HealthState
    worker = LiveAnalystWorker.__new__(LiveAnalystWorker)
    worker.settings = settings
    worker.api = api
    worker.db = db
    worker.telegram = MagicMock()
    worker.follow_ups = []
    worker.seen_event_fingerprints = set()
    worker.pending_event_fingerprints = {}
    worker.last_lineup_poll_at = {}
    worker.last_fixtures_poll_at = None
    worker.cached_fixtures = []
    worker.last_odds_poll_at = {}
    worker.alert_mode_until = {}
    worker._consecutive_idle_polls = 0
    worker._health_state = _HealthState()
    worker._rate_limit_alerted = False

    return worker, api


# ================================================================== Fixtures polling

class TestFixturesPolling:

    def test_fixtures_called_on_first_run(self):
        now = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(now)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_live_fixtures.assert_called_once()

    def test_fixtures_not_called_again_before_60s(self):
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 30s later — must NOT re-poll
            mock_dt.now.return_value = t0 + timedelta(seconds=30)
            worker.run_once()
        assert api.get_live_fixtures.call_count == 1

    def test_fixtures_called_again_after_60s(self):
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 61s later — MUST re-poll
            mock_dt.now.return_value = t0 + timedelta(seconds=61)
            worker.run_once()
        assert api.get_live_fixtures.call_count == 2


# ================================================================== Odds polling

class TestOddsPolling:

    def test_odds_called_on_first_fixture_process(self):
        now = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(now)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_odds_1x2.assert_called_once_with(_FIXTURE_ID)

    def test_odds_not_called_again_before_quiet_interval(self):
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 30s later — quiet mode, must NOT re-poll odds
            mock_dt.now.return_value = t0 + timedelta(seconds=30)
            worker.run_once()
        assert api.get_odds_1x2.call_count == 1

    def test_odds_called_again_after_quiet_interval(self):
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 61s later — must re-poll odds
            mock_dt.now.return_value = t0 + timedelta(seconds=61)
            worker.run_once()
        assert api.get_odds_1x2.call_count == 2

    def test_odds_polled_faster_in_alert_mode(self):
        """In alert mode (120s after event) odds interval drops to 15s."""
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        # Manually set alert mode active
        worker.alert_mode_until[_FIXTURE_ID] = t0 + timedelta(seconds=ALERT_DURATION_S)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 16s later — alert mode, should re-poll (interval=15s)
            mock_dt.now.return_value = t0 + timedelta(seconds=16)
            worker.run_once()
        assert api.get_odds_1x2.call_count == 2

    def test_odds_not_polled_faster_than_alert_interval(self):
        """In alert mode, still must NOT re-poll before 15s."""
        t0 = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(t0)
        worker.alert_mode_until[_FIXTURE_ID] = t0 + timedelta(seconds=ALERT_DURATION_S)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 10s later — alert mode, but still below 15s threshold
            mock_dt.now.return_value = t0 + timedelta(seconds=10)
            worker.run_once()
        assert api.get_odds_1x2.call_count == 1


# ================================================================== No events endpoint

class TestNoEventsEndpointLeakage:
    """get_fixture_events must NEVER be called — events are embedded in fixtures."""

    def test_get_fixture_events_never_called_on_normal_run(self):
        now = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(now)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_fixture_events.assert_not_called()

    def test_get_fixture_events_never_called_in_alert_mode(self):
        now = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(now)
        worker.alert_mode_until[_FIXTURE_ID] = now + timedelta(seconds=ALERT_DURATION_S)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            for _ in range(10):
                worker.run_once()
                mock_dt.now.return_value += timedelta(seconds=16)
        api.get_fixture_events.assert_not_called()

    def test_events_extracted_from_fixture_not_api(self):
        """extract_events_from_fixture (0 API calls) must be used instead."""
        now = _KICKOFF + timedelta(minutes=30)
        worker, api = _make_worker(now)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.extract_events_from_fixture.assert_called_once()


# ================================================================== Lineups polling

class TestLineupsPolling:

    def test_lineups_not_called_during_live_match(self):
        """Lineups only fetched in 60-min pre-kickoff window, not during the match."""
        now = _KICKOFF + timedelta(minutes=30)  # match in progress
        worker, api = _make_worker(now)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_fixture_lineups.assert_not_called()

    def test_lineups_called_in_pre_kickoff_window(self):
        """Lineups fetched when inside 60-min pre-kickoff window."""
        now = _KICKOFF - timedelta(minutes=30)  # 30 min before kickoff
        fixture = _make_fixture(minute=0, status="NS", kickoff=_KICKOFF)
        worker, api = _make_worker(now, fixtures=[fixture])
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_fixture_lineups.assert_called_once_with(_FIXTURE_ID)

    def test_lineups_not_re_polled_before_900s(self):
        """Lineups must not be re-fetched before 15 minutes (900s)."""
        now = _KICKOFF - timedelta(minutes=45)
        fixture = _make_fixture(minute=0, status="NS", kickoff=_KICKOFF)
        worker, api = _make_worker(now, fixtures=[fixture])
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 5 minutes later — must NOT re-poll lineups
            mock_dt.now.return_value = now + timedelta(minutes=5)
            worker.run_once()
        assert api.get_fixture_lineups.call_count == 1

    def test_lineups_re_polled_after_900s(self):
        """Lineups must be re-fetched after 15 minutes."""
        now = _KICKOFF - timedelta(minutes=55)
        fixture = _make_fixture(minute=0, status="NS", kickoff=_KICKOFF)
        worker, api = _make_worker(now, fixtures=[fixture])
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
            # 16 minutes later — MUST re-poll lineups
            mock_dt.now.return_value = now + timedelta(minutes=16)
            worker.run_once()
        assert api.get_fixture_lineups.call_count == 2


# ================================================================== Break / HT skip

class TestBreakSkipping:

    def test_no_odds_call_during_halftime(self):
        """Worker must skip fixture processing entirely during HT / BT."""
        now = _KICKOFF + timedelta(minutes=47)
        fixture = _make_fixture(status="HT", minute=45)
        worker, api = _make_worker(now, fixtures=[fixture])
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_odds_1x2.assert_not_called()

    def test_no_odds_call_during_bt(self):
        now = _KICKOFF + timedelta(minutes=92)
        fixture = _make_fixture(status="BT", minute=90)
        worker, api = _make_worker(now, fixtures=[fixture])
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            worker.run_once()
        api.get_odds_1x2.assert_not_called()


# ================================================================== Total call budget

class TestTotalCallBudget:
    """Over a simulated 90-minute match, total API calls must not exceed budget."""

    def test_call_budget_90_min_quiet_mode(self):
        """
        90 minutes quiet mode:
          fixtures: 90 calls (1/min)
          odds:     90 calls (1/min)
          lineups:  0        (no pre-kickoff window)
          events:   0        (embedded)
        Total: ≤ 182 calls (small overhead for loop timing)
        """
        t0 = _KICKOFF + timedelta(minutes=1)
        worker, api = _make_worker(t0)
        with patch("liveanalyst.worker.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            for minute in range(90):
                mock_dt.now.return_value = t0 + timedelta(minutes=minute)
                worker.run_once()

        fixtures_calls = api.get_live_fixtures.call_count
        odds_calls = api.get_odds_1x2.call_count
        events_calls = getattr(api.get_fixture_events, 'call_count', 0)
        total = fixtures_calls + odds_calls + events_calls

        assert events_calls == 0, "get_fixture_events must never be called"
        assert fixtures_calls <= 91
        assert odds_calls <= 91
        assert total <= 182
