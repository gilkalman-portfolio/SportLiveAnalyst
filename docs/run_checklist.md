# Saturday Live Run Checklist

Work through this top-to-bottom before the first fixture kicks off.
Check each item only when it is confirmed, not just assumed.

---

## Before kick-off

- [ ] **API key present**
  `echo $API_FOOTBALL_KEY` returns a non-empty value.
  Test: `curl -s -H "x-apisports-key: $API_FOOTBALL_KEY" "https://v3.football.api-sports.io/status" | python -m json.tool`
  Expect `"account"` object with `"requests_remaining"` > 0.

- [ ] **Telegram bot configured**
  `echo $TELEGRAM_BOT_TOKEN` and `echo $TELEGRAM_CHAT_ID` both return values.
  Test: send a manual message via `curl` or run the worker once — confirm a test message arrives in the chat.

- [ ] **DB connection working**
  `psql $POSTGRES_DSN -c "SELECT 1;"` returns `1`.

- [ ] **Tables migrated**
  `psql $POSTGRES_DSN -c "\dt"` shows `market_ticks`, `events`, `signals`, `signal_outcomes`.
  (Migration runs automatically on worker start, but verify manually before go-live.)

- [ ] **Fixture selection confirmed**
  Identify today's Premier League fixture IDs via API-Football:
  `curl -s -H "x-apisports-key: $API_FOOTBALL_KEY" "https://v3.football.api-sports.io/fixtures?league=39&season=2025&live=all"`
  Note the `fixture.id` values — you will need them for DB queries and replay.

---

## At kick-off

- [ ] **Polling active**
  Worker is running: `python -m liveanalyst.main_worker`
  ⚠️ Worker פועל רק בין **12:00–23:59 שעון ישראל**. הפעל לפני 12:00 — הוא יתחיל לעבוד אוטומטית.
  Logs show `processing_fixture fixture_id=...` every ~5 seconds.

- [ ] **אין quota burst בסטארטאפ**
  בדוק שבלוג לא מופיע `quota_watcher: HIGH` או `quota_watcher: CRITICAL` — אם כן, בדוק את ה-endpoint breakdown כדי לזהות את הסיבה.

- [ ] **Ticks arriving**
  ```sql
  SELECT COUNT(*), MAX(ts) FROM market_ticks WHERE fixture_id = :fixture_id;
  ```
  Row count increases between checks. `MAX(ts)` is within the last 10 seconds.

- [ ] **Events arriving**
  ```sql
  SELECT event_type, ts FROM events WHERE fixture_id = :fixture_id ORDER BY ts DESC LIMIT 5;
  ```
  Rows appear after first in-game event (goal, card, etc.).

---

## During the match

- [ ] **Signals arriving**
  ```sql
  SELECT id, ts_created, tier, cause_type, blocked FROM signals
  WHERE fixture_id = :fixture_id ORDER BY ts_created DESC LIMIT 10;
  ```
  ציפייה: סיגנלי `ODDS_MOVE` כל כמה דקות כשיש תנועה, סיגנלי `GOAL`/`RED_CARD` לאחר אירועים.
  לפחות סיגנל actionable אחד (`blocked = false`) לכל אירוע משמעותי.

- [ ] **Stale events מסוננים (לא מייצרים סיגנל)**
  בלוג: `stale_event_skipped fingerprint=... age_s=...` — לא אמור להופיע בריצה תקינה ללא restart.

- [ ] **Follow-up jobs writing outcomes**
  ```sql
  SELECT so.status, so.max_move_within_120s
  FROM signal_outcomes so
  JOIN signals s ON s.id = so.signal_id
  WHERE s.fixture_id = :fixture_id;
  ```
  Rows appear within 2 minutes of each actionable signal.

- [ ] **Latency fields populated**
  ```sql
  SELECT AVG(signal_latency_ms), AVG(source_latency_ms)
  FROM signals WHERE fixture_id = :fixture_id;
  ```
  Both averages should be non-null and non-zero.

---

## After the match

Collect the following before clearing anything:

1. Full log output (copy terminal or redirect to file: `... 2>&1 | tee run_saturday.log`)
2. DB dump: `pg_dump $POSTGRES_DSN > saturday_dump.sql`
3. Fill in `docs/signal_review_template.txt` for the 3 most interesting signals.
4. Run `sql/queries.sql` inspection queries and note any anomalies.
