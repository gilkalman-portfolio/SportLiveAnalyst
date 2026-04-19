# SportLiveAnalyst

Multi-worker Python system for **Premier League live match analysis** and **pre-match prediction**.

---

## Architecture

```
src/liveanalyst/
├── main_worker.py      ← Main entry point — orchestrates all sub-workers
├── worker.py           ← LiveAnalystWorker: live odds + signal detection (every 5s)
├── prematch_worker.py  ← PreMatchWorker: pre-match predictions (every 5 min)
├── lineup_worker.py    ← LineupWorker: lineup/injury corrections (30–75 min before kickoff)
├── api_football.py     ← API-Football client (odds, events, lineups, standings, form, injuries)
├── domain.py           ← Data models (MarketTick, SignalContext, PreMatchPrediction, SeasonStake, …)
├── logic.py            ← Pure scoring functions (signals, motivation, form)
├── prematch.py         ← PreMatchPrediction engine (Dixon-Coles, EMA form, H2H, injury, motivation)
├── db.py               ← PostgreSQL CRUD
├── telegram.py         ← Telegram notification sender
├── config.py           ← Settings from environment variables
├── replay.py           ← Replay saved ticks/events through the live signal pipeline
└── backtest.py         ← Backtesting utilities
```

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
createdb liveanalyst
cp .env.example .env   # fill in API-Football key, Telegram token, Postgres DSN
set -a && source .env && set +a
PYTHONPATH=src python -m liveanalyst.main_worker
```

On first run all migrations execute automatically, then `backfill_motivation` fills any existing signals.

---

## Configuration

| Env var | Description |
|---------|-------------|
| `POSTGRES_DSN` | PostgreSQL connection string |
| `API_FOOTBALL_KEY` | API-Football (api-sports.io) key |
| `API_FOOTBALL_BASE_URL` | Base URL (default: `https://v3.football.api-sports.io`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `LEAGUE_IDS` | Comma-separated league IDs (default: `39` = Premier League) |
| `SEASON` | Season year (default: `2025`) |

Active hours: **12:00–23:59 Israel time** (workers sleep outside this window).

---

## Processing flow

### On startup (`bootstrap`)
1. Run DB migrations `001` → `010`
2. `backfill_motivation()` — fills `home_motivation / away_motivation / stake` on all historical signals with `NULL`, using round-accurate standings from API-Football
3. `_recover_outcomes()` — backfills `signal_outcomes` for any actionable signals that lost their follow-ups on restart

### Every 5 seconds — LiveAnalystWorker (`run_once`)
1. Fetch live fixtures
2. Refresh league standings (once per calendar day)
3. Store fixture metadata: `fixture_id`, `round`, `home_team_id`, `away_team_id`
4. Poll odds (alert mode: every 2s, quiet mode: every 15s)
5. Process events (GOAL / RED_CARD / LINEUP):
   - Compute probability delta, tier (LOW / MEDIUM / HIGH)
   - Run `detect_signal()` — confidence scoring, blocking rules
   - Apply motivation post-processing (dead-rubber block, ±0.10/±0.20 confidence)
   - Store `signal`, emit Telegram if actionable
   - Schedule follow-ups at +30s / +60s / +120s
6. Process due follow-ups → write `signal_outcomes`

### Every 5 minutes — PreMatchWorker (`run_once`)
- Finds fixtures kicking off within the next 2 hours
- Runs `PreMatchEngine` (Dixon-Coles + EMA form + H2H + injury + motivation)
- Sends Telegram prediction when `confidence ≥ 0.65`

### Every 15 minutes — LineupWorker (`run_once`)
- Active 30–75 min before kickoff
- Detects key-player injuries and emits correction to pre-match prediction

---

## Pre-match prediction logic (`prematch.py`)

**Inputs:**
- Pre-match 1×2 odds (baseline implied probabilities)
- Dixon-Coles model (Poisson, λ from team attack/defense stats)
- EMA form — last 5 overall games with weights `[0.35, 0.25, 0.20, 0.12, 0.08]`
- Home/away split form (from `/teams/statistics`)
- H2H win rate (last 10 meetings)
- Fatigue (games in last 14 days, days since last match)
- Injury penalties by position: G=0.03, D=0.04, M=0.06, F=0.08 (cap 0.20)
- Season motivation (see table below)

**Composite weights:** odds 70% · form 20% · H2H 10%

**Blocking:** `confidence < 0.65`

---

## Season motivation logic (`logic.classify_stake` + `compute_motivation`)

| SeasonStake | Base motivation | Notes |
|-------------|-----------------|-------|
| TITLE / RELEGATION | 1.0 | Maximum pressure |
| CHAMPIONS_LEAGUE | 0.9 | |
| EUROPA_LEAGUE | 0.75 | |
| CONFERENCE | 0.6 | |
| MID_TABLE | 0.35 | |
| SECURED_SAFE | 0.2 | Safe with few games left |
| RELEGATED | 0.1 | Already down |

Urgency multiplier: `×1.3` (≤3 games left), `×1.1` (≤7), `×1.0` (otherwise).

**Live signal adjustments:**
- Both teams `< 0.25` → `dead_rubber_match` block
- Direction `"up"` and favoured team motivation `< 0.25` → confidence `−0.20`
- Direction `"up"` and favoured team motivation `> 0.85` → confidence `+0.10`

---

## Database schema

| Table | Purpose |
|-------|---------|
| `market_ticks` | Odds snapshots per fixture |
| `events` | GOAL / RED_CARD / LINEUP events |
| `signals` | Detected probability-shift signals (incl. motivation fields) |
| `signal_outcomes` | Follow-up evaluation at +30s/+60s/+120s |
| `team_standings` | Standings per team/league/season/**round** (round=0 = live) |
| `fixtures` | Fixture metadata: round, home/away team IDs |

### Migrations (run in order on startup via `main_worker.bootstrap`)

| File | Purpose |
|------|---------|
| `001_init.sql` | Core tables (market_ticks, events, signals, signal_outcomes) |
| `002_add_event_to_odds_ms.sql` | `event_to_odds_ms` column on signals |
| `003_add_league_id.sql` | `league_id` column on signals |
| `004_add_bets_log.sql` | `bets_log` table |
| `005_add_is_early_signal.sql` | `is_early_signal` column on signal_outcomes |
| `006_add_prematch_predictions.sql` | `prematch_predictions` table |
| `007_add_lineup_checks.sql` | `lineup_checks` table |
| `008_add_prematch_columns.sql` | Extra columns on prematch_predictions |
| `009_standings.sql` | `team_standings` table + motivation columns on signals |
| `010_fixtures_round.sql` | `fixtures` table + `round` column on team_standings |

---

## How to run

```bash
# Main entry point (recommended — all workers in one process)
PYTHONPATH=src python -m liveanalyst.main_worker

# Replay saved ticks/events for a fixture (no live API needed)
PYTHONPATH=src python -m liveanalyst.replay --fixture 12345 --speed instant

# Tests (offline — no DB or API required)
PYTHONPATH=src pytest tests/ -v
```

---

## Notes

- Primary league: Premier League (`LEAGUE_ID=39`). Multi-league via `LEAGUE_IDS`.
- Allowed live signal causes: `GOAL`, `RED_CARD`, `LINEUP_KEY_PLAYER_OUT`, `ODDS_MOVE`.
- Pre-match predictions sent to Telegram when confidence ≥ 65%.
- All motivation calculations use round-accurate historical standings for correctness.
- Active hours enforced by `main_worker.py` (12:00–23:59 Israel time).
