# Data Structure — LiveAnalyst

_עדכון: 2026-04-17_

---

## סקירה — מה נשמר ולמה

```
כל tick של odds       → market_ticks   (time-series, בסיס לכל הניתוח)
כל אירוע מגרש        → events         (GOAL / RED_CARD / LINEUP)
כל סיגנל שזוהה       → signals        (גם blocked וגם actionable)
תוצאת כל סיגנל       → signal_outcomes(מה קרה 30s/60s/120s אחרי)
הימורים ומעקב CLV    → bets_log       (עתידי — לאחר V0)
```

---

## טבלאות DB — Schema מלא

### `market_ticks` — pulse של השוק
```sql
id               BIGSERIAL PRIMARY KEY
fixture_id       BIGINT NOT NULL
ts               TIMESTAMPTZ NOT NULL       -- זמן הקריאה מה-API
minute           INTEGER                    -- דקת המשחק
home_odds        DOUBLE PRECISION NOT NULL  -- אודס גולמי
draw_odds        DOUBLE PRECISION NOT NULL
away_odds        DOUBLE PRECISION NOT NULL
p_home           DOUBLE PRECISION NOT NULL  -- הסתברות מנורמלת (סכום=1)
p_draw           DOUBLE PRECISION NOT NULL
p_away           DOUBLE PRECISION NOT NULL
source_latency_ms INTEGER NOT NULL          -- פיגור בין אירוע לAPI (ms)
league_id        INTEGER                    -- migration 003
```
**Index:** `(fixture_id, ts DESC)` — לשליפת last/prev tick.
**קצב כתיבה:** ~1 שורה לדקה לmatch (quiet) / כל 15s (alert).

---

### `events` — אירועי מגרש
```sql
id               BIGSERIAL PRIMARY KEY
fixture_id       BIGINT NOT NULL
ts               TIMESTAMPTZ NOT NULL       -- זמן האירוע: kickoff_ts + timedelta(minutes=elapsed)
                                             -- (events embedded בfixtures חסרים elapsed_at; ±30s approximation)
minute           INTEGER
event_type       TEXT NOT NULL              -- GOAL | RED_CARD | LINEUP_KEY_PLAYER_OUT
team_side        TEXT                       -- 'home' | 'away'
player_name      TEXT
is_key_player    BOOLEAN DEFAULT FALSE      -- רק רלוונטי ל-LINEUP
raw_payload      JSONB NOT NULL             -- ה-event המלא מה-API
league_id        INTEGER                    -- migration 003
```
**Index:** `(fixture_id, ts DESC)`.
**כפילויות:** מטופלות עם `ON CONFLICT DO NOTHING` + fingerprint ב-memory.

---

### `signals` — סיגנלים שזוהו
```sql
id               BIGSERIAL PRIMARY KEY
fixture_id       BIGINT NOT NULL
ts_created       TIMESTAMPTZ NOT NULL
minute           INTEGER
signal_type      TEXT NOT NULL              -- תמיד 'SHIFT' כרגע
tier             TEXT NOT NULL              -- LOW | MEDIUM | HIGH
primary_outcome  TEXT NOT NULL             -- 'home' | 'draw' | 'away'
direction        TEXT NOT NULL             -- 'up' | 'down'
p_prev           DOUBLE PRECISION NOT NULL  -- הסתברות לפני
p_now            DOUBLE PRECISION NOT NULL  -- הסתברות אחרי
delta_abs        DOUBLE PRECISION NOT NULL  -- שינוי מוחלט (max של שלושת הcols)
cause_type       TEXT NOT NULL             -- GOAL | RED_CARD | LINEUP_KEY_PLAYER_OUT | ODDS_MOVE
cause_confidence DOUBLE PRECISION NOT NULL  -- 1.0 אם סיבה מוכרת
confidence       DOUBLE PRECISION NOT NULL  -- 0.0–1.0 (לאחר penalties)
actionable       BOOLEAN NOT NULL          -- TRUE = נשלח לטלגרם
blocked          BOOLEAN NOT NULL          -- TRUE = חסום (מסיבה כלשהי)
block_reason     TEXT                      -- "cooldown_300s,minute_gte_88" וכו'
telegram_sent    BOOLEAN DEFAULT FALSE
cooldown_key     TEXT NOT NULL             -- "{fixture_id}:{cause}:{direction}"
                                           -- ODDS_MOVE: cooldown 600s; events: 300s
event_ts         TIMESTAMPTZ               -- זמן האירוע שגרם לסיגנל (NULL ב-ODDS_MOVE)
signal_latency_ms INTEGER NOT NULL         -- זמן מאירוע לzignal_detection (ms)
source_latency_ms INTEGER NOT NULL         -- פיגור ה-API (ms)
league_id        INTEGER                   -- migration 003
```
**Indexes:** `(cooldown_key, ts_created DESC)`, `(fixture_id, ts_created DESC)`.
**חשוב:** גם signals חסומים נשמרים — לצורך ניתוח block patterns.

---

### `signal_outcomes` — מה קרה אחרי הסיגנל
```sql
signal_id            BIGINT PRIMARY KEY REFERENCES signals(id)
status               TEXT NOT NULL              -- confirmed | failed | neutral
time_to_move         INTEGER                    -- שניות עד תזוזה ≥ 2%
max_move_within_120s DOUBLE PRECISION NOT NULL  -- גודל תזוזה מקסימלי ב-120s
reversed_within_120s BOOLEAN NOT NULL           -- האם השוק התהפך?
event_to_odds_ms     INTEGER                    -- ms מהאירוע עד תזוזת 2% (migration 002)
                                               --   מחושב: pre-event tick → tick שבו השינוי ≥ 2%
                                               --   baseline = get_tick_before(ev_ts) לא הtick הראשון אחרי
is_early_signal      BOOLEAN                    -- האם הסיגנל הגיע לפני השוק? (migration 005)
                                               --   מחושב ב-120s checkpoint בלבד; נשמר לDB (לא רק logged)
```
**נכתב** ב-3 checkpoints: 30s, 60s, 120s אחרי הסיגנל.
**is_early_signal** מחושב רק ב-120s checkpoint (צריך חלון מלא) ונשמר ל-DB.
**event_to_odds_ms** — baseline הוא הtick לפני האירוע (`get_tick_before`), לא הtick הראשון אחרי. כך מדידת תגובת השוק מדויקת גם כשהתזוזה הגדולה מגיעה ב-tick הראשון.
**recovery** — אם הworker נופל בין הסיגנל לcheckpoints, `_recover_outcomes()` בbootstrap מחזיר את המידע מticks קיימים ב-DB.

---

### `bets_log` — מעקב CLV (migration 004) — ישמש ב-V1
> ראה spec מלא: `docs/v1_preMatch_spec.md`
```sql
id               SERIAL PRIMARY KEY
fixture_id       INTEGER NOT NULL
league_id        INTEGER
ts_prediction    TIMESTAMPTZ NOT NULL
primary_outcome  VARCHAR(10) NOT NULL       -- 'home' | 'draw' | 'away'
prob_model       FLOAT NOT NULL             -- p מהמודל שלנו
odds_taken       FLOAT                      -- אודס שלקחנו (NULL אם לא הומר)
odds_open        FLOAT                      -- אודס פתיחה
odds_closing     FLOAT                      -- אודס סגירה (CLV base)
clv              FLOAT GENERATED ALWAYS AS  -- odds_closing / odds_taken - 1
                 (CASE WHEN odds_taken IS NOT NULL AND odds_closing IS NOT NULL
                       THEN odds_closing / odds_taken - 1 ELSE NULL END) STORED
result           SMALLINT                   -- 1=ניצחנו, 0=הפסדנו, NULL=ממתין
signal_id        INTEGER REFERENCES signals(id) ON DELETE SET NULL
notes            TEXT
```
**CLV חיובי** = האודס שלקחנו טוב מ-closing = edge אמיתי.

---

## Migrations — סדר הרצה

| מספר | קובץ | מה מוסיף |
|---|---|---|
| 001 | `001_init.sql` | 4 טבלאות בסיס |
| 002 | `002_add_event_to_odds_ms.sql` | `event_to_odds_ms` ל-signal_outcomes |
| 003 | `003_add_league_id.sql` | `league_id` + indexes ל-3 טבלאות |
| 004 | `004_add_bets_log.sql` | טבלת bets_log + CLV computed column |
| 005 | `005_add_is_early_signal.sql` | `is_early_signal` ל-signal_outcomes |

> Bootstrap מריץ את כל 5 בכל הפעלה — כולם idempotent (`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`).

---

## Domain Objects (Python)

### `Probabilities`
```python
@dataclass
class Probabilities:
    home: float  # סכום home + draw + away = 1.0 תמיד
    draw: float
    away: float
```

### `MarketTick`
```python
@dataclass
class MarketTick:
    fixture_id: int
    ts: datetime
    minute: int
    home_odds: float       # אודס גולמי (כולל margin)
    draw_odds: float
    away_odds: float
    p_home: float          # מנורמל (בלי margin)
    p_draw: float
    p_away: float
    source_latency_ms: int # ms בין זמן האירוע בAPI לזמן הקריאה
    league_id: int | None
```

### `SignalContext`
```python
@dataclass
class SignalContext:
    fixture_id: int
    ts_created: datetime
    minute: int
    primary_outcome: str    # 'home' | 'draw' | 'away' — זה שהכי זז
    direction: str          # 'up' | 'down'
    p_prev: float           # הסתברות primary_outcome לפני
    p_now: float            # הסתברות primary_outcome אחרי
    delta_abs: float        # שינוי מקסימלי (max של שלושת הcols)
    cause_type: str         # GOAL | RED_CARD | LINEUP_KEY_PLAYER_OUT | ODDS_MOVE
    cause_confidence: float
    confidence: float       # 0.0–1.0 (penalties מפחיתות)
    actionable: bool        # True = נשלח לטלגרם
    blocked: bool
    block_reason: str | None
    cooldown_key: str       # "{fixture_id}:{cause}:{direction}"
    event_ts: datetime | None
    signal_latency_ms: int
    source_latency_ms: int
    tier: str               # LOW | MEDIUM | HIGH
    signal_type: str = "SHIFT"
    league_id: int | None = None
```

### `TickSnapshot` (logic.py בלבד)
```python
@dataclass
class TickSnapshot:
    ts: int        # epoch integer (לא datetime!) — legacy מ-SQL EXTRACT
    p_home: float
    p_draw: float
    p_away: float
```
> ⚠️ `ts` כאן הוא `int` (epoch), לא `datetime`. שונה מ-`MarketTick.ts`.

---

## KPIs — המספרים שחשובים

### 1. `avg_lead_time_ms`
```
event_to_odds_ms − signal_latency_ms
```
- **חיובי** → הסיגנל שלנו הגיע לפני שהשוק תמחר = יש edge
- **שלילי** → השוק תמחר מהר יותר = אין live product

### 2. `reversed_within_120s`
אחוז סיגנלים שהתהפכו תוך 120s. גבוה = הרבה רעש.

### 3. `max_move_within_120s`
גודל התזוזה. צריך > 0.05 כדי שיהיה betting-relevant.

---

## Query — ניתוח V0 לאחר 18/4

```sql
SELECT
    s.tier,
    COUNT(*) AS n,
    ROUND(AVG(s.signal_latency_ms)) AS avg_signal_latency_ms,
    ROUND(AVG(so.event_to_odds_ms)) AS avg_event_to_odds_ms,
    ROUND(AVG(so.event_to_odds_ms - s.signal_latency_ms)) AS avg_lead_time_ms,
    COUNT(*) FILTER (WHERE so.event_to_odds_ms > s.signal_latency_ms) AS ahead_of_market,
    ROUND(100.0 * AVG(so.reversed_within_120s::int), 1) AS pct_reversed,
    ROUND(AVG(so.max_move_within_120s), 4) AS avg_max_move
FROM signals s
JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
  AND so.event_to_odds_ms IS NOT NULL
GROUP BY s.tier
ORDER BY s.tier;
```

---

## זרימת נתונים — סכמה ויזואלית

```
API-Football
    │
    │  /fixtures?live=all  ──────────────────────────────────┐
    │  (כולל events embedded)                                │
    │                                                        ▼
    │  /odds/live?fixture=X  ──────► MarketTick ──► market_ticks
    │                                    │
    │  /fixtures/lineups?fixture=X        │  (לcompare)
    │  (כל 15 דקות בחלון pre-kickoff)    │
    │                                    ▼
    └── events (embedded) ──────► events ──────► signals
                                              │
                                    ┌─────────┘
                                    │
                              detect_signal()
                                    │
                                    ▼
                              SignalContext
                              ┌────┴────┐
                              │         │
                         blocked=True  blocked=False
                              │         │
                         DB only   DB + Telegram
                                        │
                                   follow_ups (30/60/120s)
                                        │
                                        ▼
                                 signal_outcomes
                                 (status, lead_time,
                                  is_early_signal)
```
