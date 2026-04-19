# LiveAnalyst — Session Handover
_עדכון אחרון: 2026-04-18 (סשן 6)_

---

## מה הפרויקט הזה

מערכת live + pre-match לניתוח משחקי כדורגל ושידור סיגנלים בעברית לטלגרם.

**V0** = measurement harness בלבד — מודד האם יש edge (זמן הקדמה לשוק), לא מוצר.
**שאלת המחקר**: כמה שניות בין אירוע מגרש לתמחור מחדש של ה-odds?

---

## סטטוס נוכחי — מה עובד

| רכיב | סטטוס |
|---|---|
| Worker חי — polling לAPI | ✅ רץ |
| Multi-league (39,140,78,135,61) | ✅ פעיל |
| Alert/Quiet adaptive polling | ✅ פעיל |
| Idle exponential backoff | ✅ פעיל (5→10→20→40→60 דקות) |
| שמירת ticks + events לDB | ✅ פעיל |
| זיהוי סיגנלים event-driven (GOAL, RED_CARD) | ✅ פעיל |
| זיהוי סיגנלים odds-driven (ODDS_MOVE) | ✅ פעיל |
| Stale event guard (מניעת latency artifacts) | ✅ פעיל |
| Telegram בעברית + דגלים | ✅ פעיל |
| מדידת `event_to_odds_ms` | ✅ פעיל |
| `signal_outcomes` — מילוי (30/60/120s) | ✅ פעיל |
| Recovery בbootstrap | ✅ פעיל |
| Worker health check `/health` (port 8765) | ✅ חדש — 2026-04-18 |
| Rate limit monitoring (INFO + Telegram alert < 100) | ✅ חדש — 2026-04-18 |
| Pre-match model (V1) | ✅ חדש — 2026-04-18 |
| EMA form weighting [0.35,0.25,0.20,0.12,0.08] | ✅ חדש — 2026-04-18 |
| Home/Away form split via `/teams/statistics` | ✅ חדש — 2026-04-18 |
| xG attack/defense strength + Dixon-Coles Poisson model | ✅ חדש — 2026-04-18 |
| Dixon-Coles τ correction (rho=0.13) | ✅ חדש — 2026-04-18 |
| Under/Over API signal in Telegram message | ✅ חדש — 2026-04-18 |
| Standings gap adjustment | ✅ חדש — 2026-04-18 |
| Injury position-based penalty (G/D/M/F) | ✅ חדש — 2026-04-18 |
| Backtest pipeline (`prediction_outcomes` table + Brier Score) | ✅ חדש — 2026-04-18 |
| **QuotaWatcher** — burst detection (40/70 calls per 60s, endpoint breakdown) | ✅ חדש — 2026-04-18 |
| **שעות פעילות** — worker פועל רק 12:00–23:59 שעון ישראל | ✅ חדש — 2026-04-18 |
| **Prematch idle poll** — הורד מ-60 דקות ל-15 דקות | ✅ חדש — 2026-04-18 |
| **Prematch restart optimization** — standings/injuries נטענים רק עבור leagues עם fixtures חדשים | ✅ חדש — 2026-04-18 |
| bets_log / CLV tracking | ❌ לא התחיל |

---

## .env — ערכים נוכחיים

```
POSTGRES_DSN=postgresql://postgres:jojo123@localhost:5432/postgres
API_FOOTBALL_KEY=dfe30b2ed8d08960348582f2495c9e31   ← Pro plan ($19/month)
TELEGRAM_BOT_TOKEN=8657005539:AAF92Inr2AX_WWlC14UjLX57UjuUHQ6VKEk
TELEGRAM_CHAT_ID=307448954                          ← chat_id אמיתי (לא update_id)
LEAGUE_IDS=39,140,78,135,61                         ← PL, La Liga, Bundesliga, Serie A, Ligue 1
SEASON=2025
```

---

## Migrations שקיימות בDB

| קובץ | תוכן | הורץ? |
|---|---|---|
| `sql/migrations/001_init.sql` | טבלאות בסיס (market_ticks, events, signals, signal_outcomes) | ✅ |
| `sql/migrations/002_add_event_to_odds_ms.sql` | `event_to_odds_ms` ל-signal_outcomes | ✅ |
| `sql/migrations/003_add_league_id.sql` | league_id + indexes על 3 טבלאות | ✅ |
| `sql/migrations/004_add_bets_log.sql` | טבלת bets_log + CLV computed column | ✅ |
| `sql/migrations/005_add_is_early_signal.sql` | `is_early_signal` ל-signal_outcomes | ✅ |
| `sql/migrations/006_add_prematch_predictions.sql` | טבלת prematch_predictions | ✅ |
| `sql/migrations/007_add_lineup_checks.sql` | `lineup_check_sent` column | ✅ |
| `sql/migrations/008_add_prematch_columns.sql` | xG/DC/under_over columns + `prediction_outcomes` table | ✅ הורץ ידנית 2026-04-18 |

---

## קבועי Polling

### worker.py (live)
```python
FIXTURES_POLL_INTERVAL_S  = 60    # רענון רשימת משחקים חיים
QUIET_ODDS_INTERVAL_S     = 60    # odds בלי אירועים
ALERT_ODDS_INTERVAL_S     = 15    # odds אחרי אירוע
ALERT_DURATION_S          = 120   # משך Alert Mode
LINEUP_POLL_INTERVAL_S    = 900   # lineups — כל 15 דקות
MAIN_LOOP_SLEEP_S         = 5     # heartbeat
STALE_EVENT_THRESHOLD_S   = 300   # event ישן מ-5 דקות → skip signal
ODDS_SIGNAL_BASELINE_MIN  = 3     # baseline ל-ODDS_MOVE: 3 דקות אחורה
ODDS_SIGNAL_COOLDOWN_S    = 600   # cooldown בין ODDS_MOVE signals: 10 דקות
```

### prematch_worker.py
```python
POLL_INTERVAL_S = 300   # כל 5 דקות כשיש fixtures חדשים
POLL_IDLE_S     = 900   # כל 15 דקות אחרי שכל ה-fixtures עובדו (היה 3600)
```

### main_worker.py (שעות פעילות)
```python
ACTIVE_HOUR_START = 12   # 12:00 שעון ישראל
ACTIVE_HOUR_END   = 23   # 23:59 שעון ישראל (מחוץ לטווח → שינה של 60s)
```
Worker לא פועל בין 00:00–11:59 (חצות עד צהריים). מתעורר אוטומטית ב-12:00.

**חישוב requests/יום** (Pro limit = 75,000):
- 5 משחקים × 90 דק × ~2 req/min (odds) ≈ 900 req/match → ~4,500/יום
- Prematch: ~4 req/fixture × 15 fixtures ≈ 60 req
- Lineup: ~15 req כל 15 דקות ≈ 120 req
- **סה"כ**: ~5,000 req/יום — נשאר margin נוח

---

## ארכיטקטורה — קבצים מרכזיים

```
SportLiveAnalyst/
├── HANDOVER.md                        ← הקובץ הזה
├── .env                               ← secrets (לא מ-commit)
├── logs/
│   └── worker.log                     ← לוג קבוע (FileHandler)
├── sql/
│   ├── migrations/
│   │   ├── 001_init.sql
│   │   ├── 002_add_event_to_odds_ms.sql
│   │   ├── 003_add_league_id.sql
│   │   ├── 004_add_bets_log.sql
│   │   └── 005_add_is_early_signal.sql
│   └── queries.sql                    ← queries לניתוח + latency
└── src/liveanalyst/
    ├── api_football.py                ← HTTP client + QuotaWatcher (burst detection)
    ├── config.py                      ← Settings (league_ids: tuple)
    ├── db.py                          ← כל שאילתות DB + get_tick_minutes_ago()
    ├── domain.py                      ← MarketTick, SignalContext (עם league_id)
    ├── logic.py                       ← לוגיקת סיגנלים טהורה (ALLOWED_CAUSES כולל ODDS_MOVE)
    ├── main_worker.py                 ← orchestrator: live + prematch + lineup (שעות 12–23 IL)
    ├── prematch_worker.py             ← pre-match polling (5 min / 15 min idle)
    ├── lineup_worker.py               ← תיקוני הרכב לפני kickoff
    ├── prematch.py                    ← מודל חיזוי + fetch_predictions()
    ├── replay.py                      ← replay worker
    ├── telegram.py                    ← Telegram sender
    └── worker.py                      ← live worker + detect_signal() + _check_odds_driven_signal()
```

---

## שאלות מפתח שצריך לענות בניתוח הבא

```sql
-- השוואת ביצועים: ODDS_MOVE vs event-driven
SELECT
    s.cause_type,
    s.tier,
    COUNT(*) AS n,
    ROUND(AVG(s.signal_latency_ms)) AS avg_signal_latency_ms,
    ROUND(AVG(so.max_move_within_120s)::numeric, 4) AS avg_max_move,
    COUNT(*) FILTER (WHERE so.status = 'confirmed') AS confirmed,
    COUNT(*) FILTER (WHERE so.status = 'failed') AS failed,
    COUNT(*) FILTER (WHERE so.status = 'neutral') AS neutral
FROM signals s
JOIN signal_outcomes so ON so.signal_id = s.id
WHERE s.actionable = TRUE
GROUP BY s.cause_type, s.tier
ORDER BY s.cause_type, s.tier;
```

**פרשנות:**
- ODDS_MOVE עם אחוז confirmed גבוה = השוק אמנם זז, הסיגנל תפס תנועה אמיתית
- ODDS_MOVE עם אחוז failed גבוה = noise / drift שגרתי, צריך להגדיל threshold

---

## TODO — לפי עדיפות

### 🔴 גבוהה — ריצת משחקים הבאה

- [x] **הרץ migration 008** — הורץ ידנית 2026-04-18
- [ ] **הפעל worker לפני המשחקים** — `python -m liveanalyst.main_worker` (שים לב: לא `main.py`)
  - ⚠️ Worker מתחיל לעבוד **רק מ-12:00 שעון ישראל** — הפעל לפחות 5 דקות לפני
- [ ] **אחרי המשחקים** — הרץ את ה-query למטה (ODDS_MOVE vs event-driven)
- [ ] **כייל ODDS_MOVE threshold** — אם confirmed rate נמוך, שקול להעלות מ-3% ל-5%
- [ ] **בדוק health check** — `curl http://localhost:8765/health`
- [ ] **עקוב אחרי quota_watcher** — בדוק בלוג אם מופיעות אזהרות HIGH/CRITICAL בסטארטאפ

### 🟡 בינונית

- [ ] **Cron יומי 06:00** — לתזמן `prematch_runner.py` כ-Task Scheduler (או ידנית בוקר)
- [ ] **backtest settlement** — `prematch_runner.py` כבר מסלק תוצאות אוטומטית. בדוק Brier Score אחרי 10+ משחקים
- [ ] **xG validation** — בדוק שה-API מחזיר `goals.for.average` עבור 5 הליגות (לוג של `mu_home/mu_away`)

### 🟢 נמוכה

- [ ] בדיקה האם `time_to_move` קצר = סיגנל איכותי יותר מ-tier
- [ ] `LINEUP_KEY_PLAYER_OUT` — לבדוק האם מייצר סיגנלים אמיתיים
- [ ] טיפול ב-`insufficient_data` outcomes (< 3 ticks ב-120s)
- [ ] Calibration (Platt scaling) — רק אחרי 50+ prediction_outcomes מצטברות

---

## החלטות ארכיטקטורה שנקבעו — אל תשנה ללא דיון

| החלטה | נימוק |
|---|---|
| API-Football בלבד | נבדקו חלופות (Sportmonks, The Odds API) — עלות לא מצדיקה בשלב זה |
| Odds-driven signals (ODDS_MOVE) במקום event-driven בלבד | API Football מאחר בדיווח events עד 20+ דקות; odds זזים תוך דקה |
| Stale event guard (300s) | מניעת latency artifacts מ-restart / API delay |
| PostgreSQL בלבד — אין Redis | פשטות, local |
| Telegram בלבד — אין UI | V0 scope |
| אין Auth/Billing | לא רלוונטי עדיין |
| Worker מופעל ידנית מ-PyCharm | המשתמש לא רוצה Task Scheduler |
| Worker רץ רק בימי משחקים | idle backoff מטפל בהשהייה בין ימים |

---

## פקודות מהירות

```bash
# הפעל worker (מ-PyCharm terminal)
python src/liveanalyst/main.py

# ראה לוגים חיים
Get-Content logs\worker.log -Wait -Tail 50

# התחבר לDB
psql postgresql://postgres:jojo123@localhost:5432/postgres

# הרץ migration
psql postgresql://postgres:jojo123@localhost:5432/postgres -f sql/migrations/002_add_event_to_odds_ms.sql

# הרץ replay
PYTHONPATH=src python -m liveanalyst.replay --fixture <id> --speed instant

# הרץ tests
PYTHONPATH=src pytest tests/ -v
```

---

## DB Schema — טבלאות קיימות

| טבלה | תוכן |
|---|---|
| `market_ticks` | כל tick של odds עם p_home/draw/away + league_id |
| `events` | אירועי מגרש (GOAL, RED_CARD וכו') + league_id |
| `signals` | סיגנלים שזוהו + signal_latency_ms + league_id |
| `signal_outcomes` | תוצאה אחרי 120s: status, time_to_move, max_move, **event_to_odds_ms** |

---

## המספרים שחשובים — ה-3 KPIs של V0

1. **`avg_lead_time_ms`** = `event_to_odds_ms - signal_latency_ms`
   - חיובי = אנחנו לפני השוק → יש מוצר
   - שלילי = השוק מהיר יותר → אין live edge

2. **`reversed_within_120s`** — האם הסיגנל התהפך? אחוז גבוה = רעש

3. **`max_move_within_120s`** — גודל התזוזה. צריך > 0.05 כדי שיהיה betting-relevant

---

## הקשר עסקי

- **קהל יעד**: ישראלים שמהמרים על Bet365 / 1xBet (live) ו-Sport Toto (pre-match)
- **מודל הכנסה**: עדיין לא מוחלט — Subscription? Commission? נחליט אחרי V0
- **Pre-Match**: ימומש אחרי V0. המלצות בוקר יום המשחק. Dixon-Coles כמנוע.
- **CLV** = מדד ההצלחה. אם CLV > 0 בממוצע — המודל שלנו טוב מהשוק.
