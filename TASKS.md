# SportLiveAnalyst — Backlog

## Model Improvements

### HIGH PRIORITY

- [ ] **xG data per team** (`/teams/statistics`)
  - Replace raw goals with xG/xGA in attack/defense strength calculation
  - Fetch once per team per season → cache in DB
  - Source: Doc 1 (Dixon-Coles), Doc 2 (β·xG signal)
  - API cost: 2 calls per fixture (home + away), cacheable all season

- [ ] **Home/Away split in form**
  - Current: overall form %
  - Target: separate HomeForm / AwayForm from `/teams/statistics`
  - E.g. a team with 80% home form but 20% away form — today's model misses this
  - API cost: same call as xG (already fetching `/teams/statistics`)

- [ ] **Injury weighting by player importance**
  - Current: all players = position default (G/D/M/F)
  - Target: key player detection — Bruno Guimaraes, Lukaku, Dybala = 0.15–0.20 penalty
  - Approach: cross-reference player minutes + xG contribution from `/players/statistics`
  - Without this: Newcastle penalty is same whether Joelinton or a backup is out

### MEDIUM PRIORITY

- [ ] **EMA (Exponential Moving Average) on form**
  - Current: flat win% over last 5
  - Target: recent matches weighted higher — last game ×0.35, -1 ×0.25, -2 ×0.20, -3 ×0.12, -4 ×0.08
  - Source: Doc 1 — "Time-weighting: משחק אחרון חשוב יותר"
  - No extra API calls — just change how we process existing form data

- [ ] **Dixon-Coles τ correction for low-score draws**
  - Current: Poisson-independent probabilities
  - Target: correct overestimation of 0-0, 1-0, 0-1, 1-1 patterns
  - Source: Doc 1 — τ(x,y) with ρ parameter (~0.1–0.2)
  - Useful for Over/Under and Correct Score predictions

- [ ] **Calibration layer (Brier Score / Platt scaling)**
  - Current: raw confidence score, no calibration
  - Target: if model says 70% but historical accuracy is 58% at that confidence band → scale down
  - Source: Doc 1 & Doc 2 — "RPS / Brier Score לבדיקת דיוק לאורך זמן"
  - Requires: collecting ground-truth results in DB → backtest against predictions

### LOW PRIORITY

- [ ] **Standings gap signal**
  - Analysis shows effect is small (~1%) when rank gap ≤ 3
  - Worth adding only when combined with xG and home/away split
  - API cost: 1 call per league per day (cacheable)

- [ ] **Monte Carlo simulation**
  - Replace logistic blending with 10,000 Poisson simulations
  - Gives proper 1X2 + Over/Under + BTTS probabilities from same model
  - Source: Doc 1 — "Monte Carlo לקבל % ניצחון / ציון צפוי"

- [ ] **Under/Over signal in Telegram message**
  - Already fetching `under_over` from `/predictions`
  - Not yet displayed or used in confidence calculation
  - Add to message: "API צופה: Under -3.5" as context

## Infrastructure

- [ ] **Rate limit monitoring in logs**
  - Log `x-ratelimit-requests-remaining` at INFO level (currently DEBUG)
  - Alert via Telegram when < 100 remaining for the day

- [ ] **Worker health check endpoint**
  - Simple HTTP /health that returns last successful poll time
  - Detect if worker silently dies

- [ ] **Backtest pipeline with ground truth**
  - After each match: fetch final score, compare to prediction
  - Store in `prediction_outcomes` table
  - Weekly Brier Score report via Telegram
