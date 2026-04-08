# LiveAnalyst v0 (Measurement Harness)

Deterministic Python worker for **Premier League only**.

Flow:
1. ingest fixtures + odds + events from API-Football
2. persist market ticks + events into PostgreSQL
3. compute normalized probabilities
4. detect signal delta tiers (LOW/MEDIUM/HIGH)
5. apply strict block rules + confidence penalties
6. emit Telegram message for actionable signals
7. run follow-ups at +30s / +60s / +120s
8. write `signal_outcomes`

## Project structure

```text
.
├── README.md
├── requirements.txt
├── .env.example
├── docs/
│   └── example_live_fixture.log
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
    ├── telegram.py
    └── worker.py
```

## Exact setup steps (local)

1. Create Python env:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
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
   set -a && source .env && set +a
   ```
4. Run worker:
   ```bash
   PYTHONPATH=src python -m liveanalyst.main
   ```

The worker runs migration `sql/migrations/001_init.sql` at startup.

## SQL queries

Queries are in `sql/queries.sql`:
- `last_tick_per_fixture`
- `last_signal_per_cooldown_key`

## Notes

- League is hard-locked to Premier League (`LEAGUE_ID=39`).
- Allowed causes are hard-limited to `GOAL`, `RED_CARD`, `LINEUP_KEY_PLAYER_OUT`.
- Unsupported causes are blocked.
- Confidence and follow-up logic follow v0 rules exactly.
