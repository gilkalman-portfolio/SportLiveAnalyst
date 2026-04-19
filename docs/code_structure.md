# Code Structure — LiveAnalyst

_עדכון: 2026-04-18_

---

## מבנה תיקיות

```
SportLiveAnalyst/
├── .env                          ← secrets (לא ב-git)
├── HANDOVER.md                   ← session handover + TODO
├── pyproject.toml                ← pytest config
├── requirements.txt              ← psycopg, requests, python-dotenv, pytest
│
├── logs/
│   └── worker.log                ← לוג רץ (FileHandler + StreamHandler)
│
├── docs/
│   ├── code_structure.md         ← הקובץ הזה
│   ├── data_structure.md         ← DB schema + domain objects
│   ├── run_checklist.md          ← checklist לפני הפעלה
│   └── signal_review_template.txt← טופס סקירה אחרי ריצה
│
├── sql/
│   ├── migrations/
│   │   ├── 001_init.sql          ← טבלאות בסיס
│   │   ├── 002_add_event_to_odds_ms.sql
│   │   ├── 003_add_league_id.sql
│   │   ├── 004_add_bets_log.sql  ← טבלת bets_log + CLV
│   │   └── 005_add_is_early_signal.sql
│   └── queries.sql               ← queries לניתוח תוצאות
│
├── src/liveanalyst/
│   ├── config.py                 ← Settings (env vars)
│   ├── domain.py                 ← dataclasses: MarketTick, SignalContext, Probabilities
│   ├── logic.py                  ← פונקציות טהורות (ללא I/O)
│   ├── api_football.py           ← HTTP client + QuotaWatcher (burst detection)
│   ├── db.py                     ← כל שאילתות PostgreSQL
│   ├── worker.py                 ← live worker + detect_signal()
│   ├── main_worker.py            ← orchestrator: live+prematch+lineup (שעות 12–23 IL)
│   ├── prematch_worker.py        ← pre-match polling (5 min / 15 min idle)
│   ├── lineup_worker.py          ← תיקוני הרכב לפני kickoff
│   ├── prematch.py               ← מודל חיזוי pre-match + fetch_predictions()
│   ├── replay.py                 ← replay worker (מ-DB, לא API)
│   ├── telegram.py               ← שליחת הודעות טלגרם
│   └── main_worker.py            ← entrypoint (טוען .env, מפעיל MainWorker)
│
└── tests/
    ├── test_logic.py             ← 35 בדיקות לפונקציות logic.py
    ├── test_detect_signal.py     ← 33 בדיקות ל-detect_signal()
    ├── test_data_integrity.py    ← 26 בדיקות על תקינות נתונים (domain, signals, blocks)
    └── test_api_polling.py       ← 18 בדיקות על polling intervals ואי-זליגת API calls
```

---

## שכבות הארכיטקטורה

```
┌─────────────────────────────────────────────┐
│                  main.py                    │  ← נקודת כניסה
└────────────────────┬────────────────────────┘
                     │
┌────────────────────▼────────────────────────┐
│               worker.py                     │  ← לולאה ראשית
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ run_once │  │ _process │  │ follow_   │  │
│  │          │  │ _fixture │  │ ups       │  │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
└───────┼─────────────┼──────────────┼─────────┘
        │             │              │
   ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
   │   API   │   │  logic  │   │   DB    │
   │ client  │   │ (pure)  │   │ layer   │
   └─────────┘   └─────────┘   └─────────┘
        │                           │
  api-football              PostgreSQL
   .com (REST)               localhost
```

---

## קבצים — תפקיד כל אחד

### `config.py` — הגדרות
קורא את כל ה-secrets מ-`.env` ומייצר `Settings` dataclass (frozen).
שדות: `postgres_dsn`, `api_football_key`, `telegram_bot_token`, `telegram_chat_id`, `league_ids`, `season`.

---

### `domain.py` — אובייקטי נתונים
שלושה dataclasses בלבד — אין לוגיקה, רק מבנה:

| Class | תפקיד |
|---|---|
| `Probabilities` | הסתברויות home/draw/away (מנורמלות, סכום=1) |
| `MarketTick` | snapshot של odds בנקודת זמן, נשמר ל-DB |
| `SignalContext` | סיגנל שזוהה — כל המידע לשמירה ולטלגרם |

---

### `logic.py` — לוגיקה טהורה (ללא I/O)
כל הפונקציות כאן הן `pure` — אין DB, אין API, אין side effects. בדוקות ב-112 unit tests (35 logic + 33 detect_signal + 26 data_integrity + 18 api_polling).

| פונקציה | מה עושה |
|---|---|
| `normalize_probabilities(h, d, a)` | ממיר אודס להסתברויות (מסיר margin) |
| `compute_delta(p_prev, p_now)` | מחזיר שינוי מקסימלי בין שני snapshots |
| `classify_tier(delta)` | LOW/MEDIUM/HIGH/None לפי סף |
| `cause_confidence(cause)` | 1.0 אם סיבה מוכרת, 0.0 אחרת |
| `evaluate_signal_outcome(signal, ticks)` | confirmed/failed/neutral אחרי 120s |
| `is_early_signal(ticks, ts)` | האם הסיגנל הגיע לפני השוק? |
| `clamp(v, low, high)` | מגביל ערך לטווח |

**סף Tier:**
```
delta < 0.03  → None (אין סיגנל)
0.03 – 0.06   → LOW
0.06 – 0.10   → MEDIUM
≥ 0.10        → HIGH
```

---

### `api_football.py` — HTTP Client + QuotaWatcher
עוטף את api-football.com. כל קריאה לוגת את `x-ratelimit-requests-remaining`.

| Method | Endpoint | אנחנו קוראים |
|---|---|---|
| `get_live_fixtures(league_ids)` | `/fixtures?live=all` | כל 60s |
| `get_odds_1x2(fixture_id)` | `/odds/live` | 60s (quiet) / 15s (alert) |
| `get_fixture_lineups(fixture_id)` | `/fixtures/lineups` | כל 15 דקות |
| `get_fixtures_by_date(date, league_ids, season)` | `/fixtures` | פעם ב-5 דקות (prematch) |
| `get_prematch_odds(fixture_id)` | `/odds` | per-fixture (prematch) |
| `get_api_predictions(fixture_id)` | `/predictions` | per-fixture (prematch) |
| `get_team_statistics(team_id, ...)` | `/teams/statistics` | per-fixture × 2 (prematch) |
| `get_standings(league_id, season)` | `/standings` | per-league עם fixtures חדשים בלבד |
| `get_league_injuries(league_id, ...)` | `/injuries` | per-league עם fixtures חדשים בלבד |

**`QuotaWatcher`** — מחלקה פנימית שעוקבת אחרי כמות הקריאות בחלון של 60 שניות:
- `≥ 40 calls/60s` → `WARNING quota_watcher: HIGH — N calls | /predictions×14, ...`
- `≥ 70 calls/60s` → `WARNING quota_watcher: CRITICAL — N calls | ...`
- ה-breakdown מציג כמה קריאות לכל endpoint — מאפשר לזהות מה גרם לburst.

> **הערה**: events מגיעים embedded ב-`/fixtures?live=all` — אין קריאה נפרדת.

---

### `db.py` — שכבת נתונים
כל query ל-PostgreSQL. פשוט וישיר — autocommit, psycopg3, `dict_row`.

**פעולות עיקריות:**

| Method | מה עושה |
|---|---|
| `run_migration(path)` | מריץ קובץ SQL (idempotent IF NOT EXISTS) |
| `insert_market_tick(tick)` | שומר MarketTick |
| `insert_event(event)` | שומר event (ON CONFLICT DO NOTHING) |
| `insert_signal(signal)` | שומר SignalContext, מחזיר id |
| `upsert_signal_outcome(...)` | מעדכן תוצאה ב-30s/60s/120s checkpoints |
| `last_tick / prev_tick` | שני ה-ticks האחרונים לfixture |
| `recent_ticks_window` | ticks בחלון זמן (לoscillation check) |
| `prior_same_direction_exists` | האם כבר היה סיגנל באותו כיוון? |
| `cooldown_blocked` | האם יש סיגנל דומה ב-N דקות האחרונות? |
| `get_tick_before(fixture_id, ts)` | הtick האחרון **לפני** ts — baseline לחישוב event_to_odds_ms |
| `get_tick_minutes_ago(fixture_id, cutoff)` | הtick הקרוב ביותר לcutoff — baseline לסיגנלי ODDS_MOVE |
| `get_unresolved_signals(min_age_seconds)` | signals actionable ללא signal_outcome — לrecovery בbootstrap |
| `get_all_ticks/events_for_fixture` | לreplay בלבד |

---

### `main_worker.py` — Orchestrator

מפעיל את שלושת ה-sub-workers בלולאה אחת:

```python
ACTIVE_HOUR_START = 12   # 12:00 שעון ישראל (Asia/Jerusalem — DST aware)
ACTIVE_HOUR_END   = 23   # 23:59 שעון ישראל
MAIN_LOOP_SLEEP_S = 5
```

**זרימת `run_forever`:**
```
bootstrap()   → מריץ migrations + _recover_outcomes()
while True:
  בדיקת שעה (Asia/Jerusalem) — אם חוץ ל-12:00–23:59 → sleep(60s) + continue
  live.run_once()           → live signals
  live.process_follow_ups() → תוצאות 30/60/120s
  prematch.run_once(now)    → pre-match predictions
  lineup.run_once(now)      → תיקוני הרכב
  sleep(5s)
```

---

### `worker.py` — Live Worker

**Polling intervals:**
```python
FIXTURES_POLL_INTERVAL_S  = 60    # fixtures list (כולל events embedded)
QUIET_ODDS_INTERVAL_S     = 60    # odds — מצב רגיל
ALERT_ODDS_INTERVAL_S     = 15    # odds — 120s אחרי אירוע
ALERT_DURATION_S          = 120   # משך alert mode
LINEUP_POLL_INTERVAL_S    = 900   # lineups — כל 15 דקות
MAIN_LOOP_SLEEP_S         = 5     # heartbeat
STALE_EVENT_THRESHOLD_S   = 300   # מתחת לזה: event נחשב ישן, לא מייצר סיגנל
ODDS_SIGNAL_BASELINE_MIN  = 3     # baseline לסיגנלי ODDS_MOVE: 3 דקות אחורה
ODDS_SIGNAL_COOLDOWN_S    = 600   # cooldown בין סיגנלי ODDS_MOVE: 10 דקות
```

**`_recover_outcomes()` — recovery בbootstrap:**
סורק את ה-DB אחרי signals actionable שאין להם signal_outcome (אבדו בעצירת worker).
מריץ עליהם `evaluate_signal_outcome()` על ticks קיימים ושומר תוצאה ב-DB.
פועל פעם אחת בלבד בסטארטאפ, לפני תחילת הלולאה הראשית.

**`_check_odds_driven_signal()` — זיהוי תנועת שוק ללא event:**
נקרא בכל tick חדש. משווה odds נוכחיים לbaseline מ-3 דקות קודם.
אם delta ≥ 3% — מייצר סיגנל עם `cause_type="ODDS_MOVE"`.
cooldown: 10 דקות לאותו כיוון (למנוע spam במהלך ריצה הדרגתית).
**מטרה**: לתפוס תנועת שוק שנגרמה מגול/כרטיס כשה-API מאחר בדיווח האירוע (נצפה: 20+ דקות delay).

**`detect_signal()` — פונקציה טהורה (pure):**
מקבל את כל הנתונים כפרמטרים, מחזיר `SignalContext | None`.
אחראי על: חישוב confidence, בדיקת block rules, בחירת tier.

**Block rules:**
- `cause` לא ב-{GOAL, RED_CARD, LINEUP_KEY_PLAYER_OUT, ODDS_MOVE}
- LINEUP ושחקן לא key
- דקה ≥ 88
- confidence < 0.6
- כבר היה סיגנל באותו כיוון לfixture זה (event-driven בלבד)
- cooldown — אותו cooldown_key בחלון הרלוונטי

**Confidence penalties:**
- source_latency > 30s: −0.25
- ODDS_MOVE (cause לא ידוע): −0.10
- event_ts ישן > 10s (event-driven): −0.20
- LINEUP שחקן לא key: −0.20
- שוק מתנדנד (oscillation): −0.20

**Stale event guard:**
אם event מגיע ב-fixture response ישן מ-5 דקות כשנראה לראשונה (restart/API delay) — נרשם לDB אבל **לא מייצר סיגנל**. מונע latency artifacts של עשרות דקות.

---

### `replay.py`
טוען ticks + events מה-DB ומריץ אותם דרך אותו pipeline כמו ה-worker החי.
שימוש: לאמת reproducibility של signals אחרי ריצה.

---

### `telegram.py`
שולח הודעת טקסט לchat_id דרך Telegram Bot API.
הודעות בעברית עם אמוג'י דגלים, פורמט: tier + ליגה + קבוצות + סיבה + הסתברויות.

---

## כיצד הכל מתחבר — זרימה מלאה

```
1. main.py        → Settings.from_env() → LiveAnalystWorker.run_forever()

2. bootstrap()    → מריץ migrations 001–005
                 → _recover_outcomes() — משחזר signal_outcomes שאבדו בexit קודם

3. run_once()     → get_live_fixtures() → [fixture1, fixture2, ...]

4. _process_fixture(fixture):
   ├── get_odds_1x2()           → MarketTick → insert_market_tick()
   ├── _check_odds_driven_signal() → השווה ל-tick מ-3 דקות קודם → ODDS_MOVE signal אם delta ≥ 3%
   ├── get_fixture_lineups()    → lineup_lookup (רק בחלון pre-kickoff)
   ├── extract_events_from_fixture() → events list (embedded, 0 calls)
   └── לכל event חדש:
       ├── insert_event()
       ├── [stale guard] אם event ישן > 5 דקות → skip signal, continue
       ├── last_tick / prev_tick → Probabilities
       ├── DB checks (oscillation, prior, cooldown)
       ├── detect_signal() → SignalContext | None
       ├── insert_signal()
       └── אם actionable: telegram.send() + schedule follow_ups (30/60/120s)

5. process_follow_ups():
   └── לכל checkpoint due:
       ├── evaluate_signal_outcome() → confirmed/failed/neutral
       ├── חישוב event_to_odds_ms, time_to_move
       ├── is_early_signal() (רק ב-120s checkpoint)
       └── upsert_signal_outcome() → שומר הכל לDB כולל is_early_signal
```

---

## הרצה

```bash
# Worker חי
python src/liveanalyst/main.py

# Replay
PYTHONPATH=src python -m liveanalyst.replay --fixture <id> --speed instant

# Tests
PYTHONPATH=src pytest tests/ -v

# לוגים חיים (PowerShell)
Get-Content logs\worker.log -Wait -Tail 50
```
