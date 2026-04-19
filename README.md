# SportLiveAnalyst

Deterministic Python worker for **Premier League live match analysis** and **pre-match prediction**.

---

## Architecture

<<<<<<< Updated upstream
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
=======
```text
.
├── README.md
├── requirements.txt
├── .env.example
├── docs/
│   ├── example_live_fixture.log
│   ├── run_checklist.md
│   └── signal_review_template.txt
├── sql/
│   ├── migrations/
│   │   └── 001_init.sql
│   └── queries.sql
└── src/liveanalyst/
    ├── __init__.py
    ├── api_football.py
    ├── config.py
    ├── db.py
    ├── domain.py
    ├── logic.py
    ├── main.py
    ├── replay.py
    ├── telegram.py
    └── worker.py
>>>>>>> Stashed changes
```

---

<<<<<<< Updated upstream
## Processing flow

### On startup (`bootstrap`)
1. Run DB migrations `001` → `002` → `003`
2. `backfill_motivation()` — fills `home_motivation / away_motivation / stake` on all historical
   signals that have `NULL`, using the correct round's standings from API-Football
=======
1. Create Python env:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Create PostgreSQL database:
   ```bash
   createdb liveanalyst
   ```
3. Configure env vars:
   ```bash
   cp .env.example .env
   # edit .env with API-Football and Telegram values
   set -a && source .env && set +a   # Windows: use .env with python-dotenv (auto-loaded)
   ```

## How to run: live mode

```bash
PYTHONPATH=src python -m liveanalyst.main
```

The worker:
- runs `sql/migrations/001_init.sql` automatically on startup
- polls every 5 seconds for live Premier League fixtures
- writes `market_ticks`, `events`, `signals`, `signal_outcomes` to PostgreSQL
- emits Telegram messages for actionable signals
- schedules and processes follow-ups at +30s / +60s / +120s

Logs to stdout. To capture to file while watching live:
```bash
PYTHONPATH=src python -m liveanalyst.main 2>&1 | tee run_saturday.log
```

Key log events to watch:
- `processing_fixture fixture_id=... minute=...` — worker polled a live fixture
- `signal_created id=... blocked=False` — actionable signal emitted
- `signal_blocked signal_id=... reason=...` — signal suppressed and why
- `telegram_sent signal_id=...` — Telegram message dispatched
- `followup signal_id=... checkpoint=120s status=...` — outcome written

## How to run: replay mode

Replays saved `market_ticks` and `events` for a fixture through the same
signal pipeline. Requires data already in the DB from a live run.

```bash
# Instant replay (no sleep — completes in seconds)
PYTHONPATH=src python -m liveanalyst.replay --fixture 12345 --speed instant

# 5× faster than real time
PYTHONPATH=src python -m liveanalyst.replay --fixture 12345 --speed 5x

# Real-time replay (useful for manual observation)
PYTHONPATH=src python -m liveanalyst.replay --fixture 12345 --speed 1x

# Also emit Telegram messages during replay (off by default)
PYTHONPATH=src python -m liveanalyst.replay --fixture 12345 --speed instant --telegram
```

Replay guarantees:
- Uses identical `detect_signal()` logic as the live worker — no divergence.
- **Idempotent**: running replay twice on the same fixture does not create
  duplicate signals (keyed on `fixture_id + minute + cause_type + primary_outcome`).
- Blocked signals are not written to DB — only actionable signals are stored.
- `is_early_signal` is evaluated in the follow-up phase only, same as live.
- `signal_latency_ms` will be 0 for replay signals (event is processed at
  event time, not wall clock time). This is expected and correct.

## How to inspect DB results

All inspection queries are in `sql/queries.sql`. Replace `:fixture_id` with
the actual integer ID before running.

```bash
# Open psql and paste queries from sql/queries.sql, or pipe directly:
psql $POSTGRES_DSN -f sql/queries.sql

# Run a single named query:
psql $POSTGRES_DSN -c "
  SELECT id, ts_created, tier, cause_type, blocked, block_reason
  FROM signals
  WHERE fixture_id = 12345
  ORDER BY ts_created ASC;
"
```

Available queries in `sql/queries.sql`:

| Query name | What it shows |
|---|---|
| `last_20_ticks_for_fixture` | Most recent 20 market ticks |
| `all_events_for_fixture` | All events in chronological order |
| `all_signals_for_fixture` | All signals (actionable + blocked) |
| `all_signal_outcomes_for_fixture` | Signals joined to outcomes |
| `blocked_signals_by_reason` | Block reason frequency (all fixtures) |
| `avg_signal_latency_by_fixture` | Detection latency stats per fixture |
| `avg_source_latency_by_fixture` | API source latency stats per fixture |
| `outcome_counts_by_fixture` | Confirmed / failed / neutral counts |
| `early_vs_late_signals` | Signal quality by first vs second half |

## What to collect after Saturday's run

Before clearing anything:

1. **Logs**: `cp run_saturday.log docs/run_saturday_YYYY-MM-DD.log`
2. **DB dump**: `pg_dump $POSTGRES_DSN > docs/saturday_dump_YYYY-MM-DD.sql`
3. **Signal review**: fill in `docs/signal_review_template.txt` for the 3 most
   interesting signals. Query `all_signal_outcomes_for_fixture` to pick them.
4. **Run checklist**: note any step that failed or surprised you.
5. **Replay verification**: after the match, run instant replay and confirm
   signal counts match between live run and replay.

## How to run tests

Tests cover all pure functions in `logic.py` and the new `detect_signal()`.
No DB or API needed — they run fully offline.

```bash
pip install -r requirements.txt   # installs pytest if not already present
PYTHONPATH=src pytest tests/ -v
```

Expected output: **68 passed** in under 1 second.

What is tested:
- `normalize_probabilities` — margin removal, sums to 1, ordering
- `compute_delta` — max of three abs diffs
- `classify_tier` — all tier boundaries (0.03 / 0.06 / 0.10)
- `clamp` — upper and lower bounds
- `cause_confidence` — allowed vs blocked causes
- `evaluate_signal_outcome` — confirmed / failed / neutral / insufficient
- `is_early_signal` — prior movement, future movement, insufficient data
- `detect_signal` — returns None below threshold, all block rules, all
  confidence penalties, cooldown key format, signal field propagation

What is NOT tested (requires live infrastructure):
- `Database` (needs PostgreSQL)
- `APIFootballClient` (needs API key)
- `TelegramSender` (needs bot token)
- Full worker loop or replay end-to-end

## What "edge" means

We are not measuring P&L. We are measuring three signal properties:

**Early** — The signal fired before the market had fully moved. Technically:
no significant drift in the 30 seconds before the signal, but a move of
>2% probability occurs within 120 seconds after. Measured by `is_early_signal`.

**Stable** — The signal direction held within the 120-second follow-up window
without reversing. A reversal of >1.5% in the opposite direction marks a
signal as `failed`. Measured by `reversed_within_120s`.

**Actionable** — The signal passed all block rules and had confidence ≥ 0.6.
Only actionable signals are emitted to Telegram and tracked with outcomes.

A signal that is early + stable + actionable is what this harness is designed
to find. Saturday's run measures how often that happens, and what the latency,
delta, and tier distribution looks like in real match conditions.
>>>>>>> Stashed changes

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

<<<<<<< Updated upstream
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
=======
Queries are in `sql/queries.sql`:
- `last_tick_per_fixture`
- `last_signal_per_cooldown_key`
- Saturday inspection queries (see table above)

## Notes

- League is hard-locked to Premier League (`LEAGUE_ID=39`).
- Allowed causes are hard-limited to `GOAL`, `RED_CARD`, `LINEUP_KEY_PLAYER_OUT`.
- Unsupported causes are blocked.
- Confidence and follow-up logic follow v0 rules exactly.
- Replay mode requires existing ticks + events in the DB (from a live run).
>>>>>>> Stashed changes
