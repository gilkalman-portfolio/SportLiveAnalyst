# V1 — Pre-Match Model Spec

_עדכון: 2026-04-12_

> **מתי מתחילים:** אחרי ניתוח תוצאות V0 (18/4). רק אם `avg_lead_time_ms > 0` — ממשיכים גם live. אם שלילי — V1 הוא המוצר העיקרי.

---

## מטרה

מודל שמייצר `prob_model` עצמאי (לא מבוסס שוק) לפני kickoff.
השוואה ל-opening odds → זיהוי value bets → CLV tracking.

---

## Input — מה נכנס (לפני kickoff בלבד)

| נתון | מקור | הערות |
|---|---|---|
| xG for/against לכל קבוצה | `/fixtures/statistics` (API-Football) | לאמת זמינות לכל 5 ליגות לפני build |
| Fixtures היסטוריים | `/fixtures?league=X&season=Y` | מינימום 2 עונות |
| Odds פתיחה | `/odds/live` בסמוך ל-kickoff | snapshot ראשון בלבד |
| Injuries | `/injuries?fixture=X` | override בלבד — שלב מאוחר |

**לא להשתמש ב:** H2H, league position, form streak, odds היסטוריים כ-feature.

---

## Build Steps

### שלב 1 — דירוג קבוצות

```
attack_strength[team]  = f(xG_for)   עם time decay אקספוננציאלי
defense_strength[team] = f(xG_against) עם time decay אקספוננציאלי
home_advantage[league] = דינמי per-league (לא קבוע גלובלי)
```

**Time decay:**
```
weight(match) = exp(-λ × days_ago)
λ = 0.002  ← נקודת התחלה, לכייל אחר כך
```

לאמת λ על grid קטן: `[0.001, 0.002, 0.003, 0.005]` — בחירה לפי log loss, לא accuracy.

---

### שלב 2 — Dixon-Coles

```
λ_home = attack_strength[home] × defense_strength[away] × home_advantage
λ_away = attack_strength[away] × defense_strength[home]
```

כולל rho correction לתוצאות נמוכות (0-0, 1-0, 0-1, 1-1).

fitting: MLE עם scipy.optimize.

---

### שלב 3 — מטריצת תוצאות → 1X2

```
P(goals_home=i, goals_away=j) = Poisson(λ_home, i) × Poisson(λ_away, j) × ρ(i,j)

סכום עד 6×6 (מספיק לכל תוצאה מעשית)

p_home = Σ P(i>j)
p_draw = Σ P(i=j)
p_away = Σ P(i<j)
```

---

### שלב 4 — Calibration

- **שיטה:** isotonic regression
- **על:** validation set בלבד (עונה נפרדת מה-training)
- **לא:** לכייל על כל הנתונים — overfitting
- **פלט:** `prob_model_calibrated` (0–1, sum=1)

Split:
```
train:      עונה N-2, N-1
validation: עונה N (השנה הנוכחית — out-of-sample)
```

---

### שלב 5 — השוואה לשוק

```python
# הסר margin מהodds
implied_prob = 1 / odds
total = sum(implied_probs)
market_prob = implied_prob / total  # ללא vigorish

# EV
EV = prob_model × odds_taken - 1
```

---

### שלב 6 — סינון Value Bets

הימור נכנס רק אם:
```
EV > 2%                          # edge מינימלי
prob_model > market_prob + 0.03  # המודל שלנו בטוח יותר מהשוק
odds לא זזו > 15% מהפתיחה      # sanity check — לא נכנסים לשוק שכבר זזז
```

---

### שלב 7 — Logging (קריטי)

לשמור לכל משחק שמריצים עליו מודל — **גם אם לא הימרנו**:

```sql
-- bets_log (migration 004, כבר קיים)
INSERT INTO bets_log (
    fixture_id, league_id, ts_prediction,
    primary_outcome,
    prob_model,        -- תמיד — לכל משחק
    odds_open,         -- תמיד
    odds_taken,        -- NULL אם לא הימרנו
    odds_closing,      -- נמלא אחרי המשחק
    result,            -- נמלא אחרי המשחק
    is_bet             -- 0/1 — קריטי לselection bias
)
```

> ⚠️ **Selection bias**: אם נשמור רק rows שהימרנו — CLV יהיה מנופח. חייבים `is_bet` flag וחישוב CLV על כל הdataset.

---

### שלב 8 — Evaluation

לאחר 300+ משחקים:

| מדד | חישוב | target |
|---|---|---|
| **CLV** | `odds_closing / odds_taken - 1` (ממוצע) | > 0 |
| **ROI** | `Σ profit / Σ stake` | > 0 |
| **Log Loss** | על כל המשחקים (כולל non-bets) | ירידה לאורך זמן |
| **Calibration** | reliability diagram — 10 buckets | קרוב לdiagonal |

**Query CLV:**
```sql
SELECT
    AVG(clv) AS avg_clv,
    COUNT(*) FILTER (WHERE clv > 0) AS positive_clv,
    COUNT(*) AS total_bets,
    AVG(clv) FILTER (WHERE is_bet = 1) AS clv_bets_only,
    AVG(clv) AS clv_all_predictions  -- השוואה לbets_only
FROM bets_log
WHERE odds_closing IS NOT NULL;
```

---

## V0+V1 Hybrid — שיפור איכות סיגנלים Live

> הסעיף הזה נוסף אחרי ניתוח Osasuna vs Villarreal (12/4/2026).
> המסקנה: V0 מבוסס-שוק בלבד מייצר סיגנלים רועשים. V1 יוסיף prior עצמאי שמסנן אותם.

---

### בעיה: סיגנל V0 בלי הקשר

במשחק Osasuna (בית, מקום 9) vs Villarreal (חוץ, מקום 5), בדקה ~70 עם תיקו:
- השוק נתן Osasuna-win 40.8% — כי זה מגרש הבית שלהם
- V0 קיבל nudge קטן ב-home win → שלח סיגנל BUY Osasuna
- **הסיגנל היה שגוי** — בדקה 70 עם תיקו, קבוצת חוץ חזקה יותר מהשחקנות

הבעיה: V0 מכיר רק **שינוי** בהסתברות, לא **רמת** ההסתברות. הוא לא יודע אם 40.8% זה גבוה מדי או נמוך מדי ביחס למציאות.

---

### פתרון V1: סינון `p_model vs p_market`

לאחר שיש לנו `prob_model` (Dixon-Coles) לפני הקיקאוף, סיגנל live ייפתח רק אם:

```python
# בעת קבלת סיגנל V0 לtarget outcome X:
edge = prob_model[outcome] - market_prob[outcome]

if edge < SIGNAL_EDGE_THRESHOLD:   # המודל לא מסכים שזה value
    block_signal(reason="model_below_market")
```

**ערך threshold מוצע:** `SIGNAL_EDGE_THRESHOLD = -0.03`
- `edge > 0`: המודל נותן יותר מהשוק → סיגנל ישלח
- `edge > -0.03`: המודל קרוב מספיק → סיגנל ישלח
- `edge < -0.03`: השוק מעריך גבוה בהרבה מהמודל → חסום

> בדוגמת Osasuna: `prob_model[home_win] = 0.35` (על בסיס xG), `market_prob = 0.408` → `edge = -0.058` → חסום.

---

### סינון הקשר מינוט (Minute Context)

מקרים שבהם nudge קטן הוא רעש ולא סיגנל אמיתי:

| תנאי | פעולה |
|---|---|
| `minute >= 75` + תוצאה תיקו | suppress home-win signals — הסטטוס קוו כבר "ניצחון" לקבוצת החוץ |
| `minute >= 80` + קבוצה מובילה ב-2+ | suppress win signals לקבוצה המובילה — השוק מתמחר lock |
| `minute <= 10` | suppress כל סיגנל שאינו GOAL-based — שוק עדיין מתייצב |

> **לא לממש ב-V0** — V0 נשאר פשוט. הסינון הזה ייכנס ב-V1 כ-`context_filter()`.

---

### Home/Away Win Rates — הבהרה

הספק הנוכחי אומר "אין form streak" — זה נשאר נכון.
**אבל**: שיעורי ניצחון בית/חוץ **כן** נכנסים — דרך Dixon-Coles, לא כ-feature נפרד:

```
attack_strength[team] = פונקציה של xG_for (בית + חוץ נפרד)
home_advantage[league] = מוסיף λ לכל קבוצת בית
```

אין צורך ב-"כמה ניצחונות יש לאוסאסונה בבית?" כ-feature מפורש — זה embeddings בתוך attack/defense + home_advantage.

---

## Stack

| רכיב | כלי |
|---|---|
| חישוב | Python — pandas, numpy |
| MLE fitting | scipy.optimize |
| Calibration | sklearn.isotonic |
| Storage | PostgreSQL (bets_log קיים) |
| Scheduling | cron יומי 06:00 (לפני משחקים) |
| הפצה | Telegram (אותו bot) |

---

## Telegram Output (V1)

```
📊 Pre-Match | פרמייר ליג 🏴󠁧󠁢󠁥󠁮󠁧󠁿
Arsenal נגד Chelsea | 20:00

המודל: Arsenal 58% | Draw 22% | Chelsea 20%
השוק:  Arsenal 55% | Draw 24% | Chelsea 21%

Value: Arsenal ⬆️ (+3%)
EV: +4.2% | Odds: 1.82
```

---

## Pre-conditions לפני Build

- [ ] לאמת ש-API מחזיר xG עבור 5 הליגות (`/fixtures/statistics?fixture=X`)
- [ ] לאסוף 2 עונות היסטוריות (API calls חד-פעמיים)
- [ ] להחליט: V0 `avg_lead_time_ms` חיובי או שלילי → מה המוצר הראשי

---

## מה לא לעשות ב-V1

- אין Redis
- אין UI
- אין H2H כ-feature
- אין form streak (סדרת תוצאות אחרונות — "3 ניצחונות ברצף") ← **שונה** משיעור ניצחון עונתי
- אין league position
- אין ensemble models
- אין neural networks
- אין features מה-live worker (V0 ו-V1 נפרדים)
- **אין** minute-context filter ב-V0 — רק ב-V1
