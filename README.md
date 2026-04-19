# SportLiveAnalyst

Deterministic Python worker for **Premier League live match analysis** and **pre-match prediction**.

---

## Architecture

```
api_football.py   ← API-Football client (odds, events, lineups, standings, form, injuries)
domain.py         ← Data models (MarketTick, SignalContext, PreMatchPrediction, SeasonStake, …)
logic.py          ← Pure scoring functions (signals, motivation, form, pre-match prediction)
db.py             ← PostgreSQL CRUD (market_ticks, signals, fixtures, standings, …)
worker.py         ← Main loop: live signal detection + backfill + pre-match trigger
prematch.py       ← Pre-match prediction engine (PreMatchEngine)
telegram.py       ← Telegram notification sender
config.py         ← Settings from environment variables
main.py           ← Entry point
```

---

## Processing flow

### On startup (`bootstrap`)
1. Run DB migrations `001` → `002` → `003`
2. `backfill_motivation()` — fills `home_motivation / away_motivation / stake` on all historical
   signals that have `NULL`, using the correct round's standings from API-Football

### Every 5 seconds (`run_once`)
1. Fetch live Premier League fixture
2. Refresh league standings (once per calendar day)
3. Store fixture metadata: `fixture_id`, `round`, `home_team_id`, `away_team_id`
4. Ingest odds → normalize probabilities → store `market_tick`
5. **Pre-match window** (60 min before kickoff, first poll only):
   - Fetch lineups
   - Run `PreMatchEngine.predict(fixture_id)` → log prediction
6. Process events (GOAL / RED_CARD / LINEUP):
   - Compute probability delta, tier (LOW / MEDIUM / HIGH)
   - Apply confidence scoring with motivation adjustments (±0.10 / ±0.20)
   - Apply blocking rules (dead rubber, minute ≥ 88, cooldown, confidence < 0.6)
   - Store `signal`, emit Telegram if actionable
   - Schedule follow-ups at +30s / +60s / +120s
7. Process due follow-ups → write `signal_outcomes`

---

## Pre-match prediction logic (`logic.compute_prematch_prediction`)

**Inputs:**
- Pre-match 1×2 odds (baseline)
- Season stake + motivation (from round-accurate standings)
- Home form: last 5 **home** games with recency weights `[0.30, 0.25, 0.20, 0.15, 0.10]`
- Away form: last 5 **away** games with same weights
- Key player absences (injuries API)

**Adjustments applied to baseline:**

| Factor | Effect | Weight |
|--------|--------|--------|
| Motivation relative | Shifts home↔away probability | `W=0.04` |
| Motivation absolute | Suppresses draw when both teams motivated; raises it in dead rubbers | `W=0.03` |
| Form delta (home/away split) | Shifts home↔away | `W=0.03` |
| Key player absence | Reduces affected team's probability | `0.03/player, max 2` |

**Blocking:** `dead_rubber`, `market_too_even` (max_p < 0.35), `low_confidence` (< 0.50)

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

### Migrations (run in order on startup)
- `001_init.sql` — core tables
- `002_standings.sql` — `team_standings` table + motivation columns on `signals`
- `003_fixtures_round.sql` — `fixtures` table + `round` column on `team_standings`

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb liveanalyst
cp .env.example .env   # fill in API-Football key, Telegram token, Postgres DSN
set -a && source .env && set +a
PYTHONPATH=src python -m liveanalyst.main
```

On first run, all migrations execute automatically, then `backfill_motivation` runs for any
existing signals.

---

## Configuration

| Env var | Description |
|---------|-------------|
| `POSTGRES_DSN` | PostgreSQL connection string |
| `API_FOOTBALL_KEY` | API-Football (api-sports.io) key |
| `API_FOOTBALL_BASE_URL` | Base URL (default: `https://v3.football.api-sports.io`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `LEAGUE_ID` | League ID (default: `39` = Premier League) |
| `SEASON` | Season year (default: `2025`) |

---

## Notes

- League is hard-locked to Premier League by default (`LEAGUE_ID=39`).
- Allowed live signal causes: `GOAL`, `RED_CARD`, `LINEUP_KEY_PLAYER_OUT`.
- Pre-match prediction runs once per fixture, logged only (not yet sent to Telegram).
- All motivation calculations use round-accurate historical standings for correctness.
