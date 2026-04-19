from __future__ import annotations

import logging
<<<<<<< Updated upstream
from datetime import datetime, timezone

from liveanalyst.api_football import APIFootballClient
from liveanalyst.config import Settings
from liveanalyst.db import Database
from liveanalyst.domain import PreMatchPrediction, SeasonStake, TeamStanding
from liveanalyst.logic import classify_stake, compute_motivation, compute_prematch_prediction, key_player_from_lineup_player


class PreMatchEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.postgres_dsn)
        self.api = APIFootballClient(settings.api_football_base_url, settings.api_football_key)

    def predict(self, fixture_id: int) -> PreMatchPrediction | None:
        fixture_info = self.db.get_fixture_info(fixture_id)
        if not fixture_info:
            raw = self.api.get_fixture_info(fixture_id)
            if not raw:
                logging.warning("prematch: fixture %s not found", fixture_id)
                return None
            self.db.upsert_fixture_info(**raw)
            fixture_info = raw

        home_id   = fixture_info["home_team_id"]
        away_id   = fixture_info["away_team_id"]
        league_id = fixture_info["league_id"]
        season    = fixture_info["season"]
        round_num = fixture_info["round"]

        # ── Odds ─────────────────────────────────────────────────────────
        odds = self.api.get_odds_1x2(fixture_id)
        if not odds:
            logging.warning("prematch: no odds for fixture %s", fixture_id)
            return None
        home_odds, draw_odds, away_odds, _ = odds

        # ── Motivation ───────────────────────────────────────────────────
        standings = self.db.get_standings_for_round(home_id, away_id, league_id, season, round_num)
        if len(standings) < 2:
            rows = self.api.get_standings_by_round(league_id, season, round_num)
            for entry in rows:
                self.db.upsert_team_standing_for_round(
                    team_id=entry["team"]["id"],
                    league_id=league_id,
                    season=season,
                    round_num=round_num,
                    position=entry["rank"],
                    points=entry["points"],
                    games_played=entry["all"]["played"],
                )
            standings = self.db.get_standings_for_round(home_id, away_id, league_id, season, round_num)

        home_motivation = away_motivation = None
        home_stake = away_stake = None
        if len(standings) >= 2:
            home_row, away_row = standings[0], standings[1]
            home_s = TeamStanding(home_row["team_id"], home_row["position"], home_row["points"], home_row["games_played"], 38)
            away_s = TeamStanding(away_row["team_id"], away_row["position"], away_row["points"], away_row["games_played"], 38)
            home_stake = classify_stake(home_s, league_id)
            away_stake = classify_stake(away_s, league_id)
            home_motivation = compute_motivation(home_stake, home_s.games_remaining)
            away_motivation = compute_motivation(away_stake, away_s.games_remaining)

        # ── Form (home/away split, last 5) ───────────────────────────────
        home_form = self.api.get_team_recent_form(home_id, season, venue="home", last=5)
        away_form = self.api.get_team_recent_form(away_id, season, venue="away", last=5)

        # ── Injuries (key players only) ───────────────────────────────────
        home_absences = self._count_key_absences(fixture_id, home_id)
        away_absences = self._count_key_absences(fixture_id, away_id)

        return compute_prematch_prediction(
            fixture_id=fixture_id,
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            home_motivation=home_motivation,
            away_motivation=away_motivation,
            home_stake=home_stake,
            away_stake=away_stake,
            home_form_results=home_form,
            away_form_results=away_form,
            home_key_absences=home_absences,
            away_key_absences=away_absences,
        )

    def _count_key_absences(self, fixture_id: int, team_id: int) -> int:
        data = self.api._get("/injuries", fixture=fixture_id, team=team_id)
        count = 0
        for entry in data.get("response", []):
            player = entry.get("player", {})
            if key_player_from_lineup_player({"pos": player.get("pos"), "statistics": {}}):
                count += 1
        return count
=======
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from liveanalyst.api_football import APIFootballClient
from liveanalyst.logic import normalize_probabilities

log = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.65

# Composite weight: odds implied prob carries most of the signal
W_ODDS = 0.70
W_FORM = 0.20
W_H2H  = 0.10

_LEAGUE_NAMES = {
    39:  "פרמייר ליג 🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    140: "לה ליגה 🇪🇸",
    78:  "בונדסליגה 🇩🇪",
    135: "סרייה א 🇮🇹",
    61:  "ליג 1 🇫🇷",
}

_RESULT_EMOJIS = {"W": "✅", "D": "🟡", "L": "❌"}

# EMA weights for form: index 0 = most recent match, index 4 = oldest
_EMA_WEIGHTS = [0.35, 0.25, 0.20, 0.12, 0.08]

# Approximate average goals per game per venue for the 5 leagues (2024-25 season)
_LEAGUE_AVG_GOALS: dict[int, tuple[float, float]] = {
    39:  (1.55, 1.20),  # Premier League: home, away
    140: (1.60, 1.10),  # La Liga
    78:  (1.65, 1.25),  # Bundesliga
    135: (1.45, 1.05),  # Serie A
    61:  (1.50, 1.15),  # Ligue 1
}
_DEFAULT_AVG_GOALS = (1.55, 1.15)

# Standings gap: weight applied per rank-position gap (capped)
_STANDINGS_GAP_WEIGHT = 0.003
_STANDINGS_GAP_CAP = 0.03

# Position-based injury penalty (fraction of composite prob adjustment per missing player)
_INJURY_PENALTY: dict[str, float] = {"G": 0.03, "D": 0.04, "M": 0.06, "F": 0.08}


@dataclass
class PreMatchPrediction:
    fixture_id: int
    league_id: int
    home_team: str
    away_team: str
    home_team_id: int
    away_team_id: int
    kickoff: datetime
    home_odds: float
    draw_odds: float
    away_odds: float
    p_home_implied: float
    p_draw_implied: float
    p_away_implied: float
    p_home_composite: float
    p_draw_composite: float
    p_away_composite: float
    recommended_outcome: str   # 'home' | 'draw' | 'away'
    confidence: float
    form_home: float           # 0-1  (EMA-weighted, overall)
    form_away: float           # 0-1
    h2h_home_rate: float       # fraction of H2H wins for home team (0-1)
    form_home_str: str
    form_away_str: str
    games_in_14d_home: int
    games_in_14d_away: int
    days_since_last_home: int
    days_since_last_away: int
    # Optional enrichment fields (None when data unavailable)
    under_over: str | None = None          # API prediction e.g. "-3.5" or "+2.5"
    form_home_home: float | None = None    # home team's home-venue form
    form_home_away: float | None = None    # home team's away-venue form (travel form)
    form_away_home: float | None = None    # away team's home-venue form
    form_away_away: float | None = None    # away team's away-venue form
    mu_home: float | None = None           # expected goals home (Dixon-Coles input)
    mu_away: float | None = None           # expected goals away
    p_home_dc: float | None = None         # Dixon-Coles 1X2 probability
    p_draw_dc: float | None = None
    p_away_dc: float | None = None
    standings_gap: int | None = None       # away_rank - home_rank (positive = home ranked higher)
    injury_penalty_home: float | None = None
    injury_penalty_away: float | None = None


def _form_score_from_pct(pct_str: str) -> float:
    """Parse API last_5.form like '20%' → 0.20. Falls back to 0.5."""
    try:
        return float(pct_str.strip("%")) / 100
    except (ValueError, AttributeError):
        return 0.5


def _form_score(fixtures: list[dict], team_id: int) -> tuple[float, str]:
    """Return (ema_score 0-1, result_string) from completed fixtures — most recent first."""
    results = []
    for f in fixtures:
        home_id = f["teams"]["home"]["id"]
        away_id = f["teams"]["away"]["id"]
        home_goals = f["goals"]["home"] or 0
        away_goals = f["goals"]["away"] or 0
        if team_id == home_id:
            if home_goals > away_goals:
                results.append("W")
            elif home_goals == away_goals:
                results.append("D")
            else:
                results.append("L")
        elif team_id == away_id:
            if away_goals > home_goals:
                results.append("W")
            elif away_goals == home_goals:
                results.append("D")
            else:
                results.append("L")
    if not results:
        return 0.5, ""
    # EMA: index 0 = most recent (API returns newest first for last=N queries)
    weights = _EMA_WEIGHTS[:len(results)]
    total_weight = sum(weights)
    score = sum(
        (weights[i] / total_weight) * (1.0 if r == "W" else 0.5 if r == "D" else 0.0)
        for i, r in enumerate(results)
    )
    return score, "".join(results)


def _home_away_form_from_stats(stats: dict) -> tuple[float, float]:
    """(home_form, away_form) from /teams/statistics — points-based per venue."""
    f = stats.get("fixtures", {})
    ph = f.get("played", {}).get("home", 0)
    pa = f.get("played", {}).get("away", 0)
    wh = f.get("wins", {}).get("home", 0)
    dh = f.get("draws", {}).get("home", 0)
    wa = f.get("wins", {}).get("away", 0)
    da = f.get("draws", {}).get("away", 0)
    home_form = (wh * 3 + dh) / (ph * 3) if ph else 0.5
    away_form = (wa * 3 + da) / (pa * 3) if pa else 0.5
    return home_form, away_form


def _attack_defense_from_stats(stats: dict) -> tuple[float, float]:
    """(avg_goals_scored, avg_goals_conceded) per game from team statistics."""
    goals = stats.get("goals", {})

    def _parse(val) -> float:
        try:
            return float(val) if val else 0.0
        except (ValueError, TypeError):
            return 0.0

    scored = _parse(goals.get("for", {}).get("average", {}).get("total"))
    conceded = _parse(goals.get("against", {}).get("average", {}).get("total"))
    return scored or 1.3, conceded or 1.1


def _standings_gap_adjustment(home_rank: int, away_rank: int) -> float:
    """Small positive adjustment for home team when it is clearly higher-ranked."""
    gap = away_rank - home_rank  # positive means home is ranked higher (better)
    if gap <= 3:
        return 0.0
    return min(gap * _STANDINGS_GAP_WEIGHT, _STANDINGS_GAP_CAP)


def _injury_penalty(injuries: list[dict], team_id: int) -> float:
    """Total penalty for injured players on a given team, weighted by position."""
    total = 0.0
    for inj in injuries:
        if inj.get("team", {}).get("id") != team_id:
            continue
        pos = (inj.get("player", {}).get("type") or "")[:1].upper()  # G/D/M/F
        total += _INJURY_PENALTY.get(pos, 0.05)
    return min(total, 0.20)  # cap at 20%


def _h2h_home_rate(h2h_fixtures: list[dict], home_team_id: int) -> float:
    """Fraction of H2H games won by the current home team."""
    if not h2h_fixtures:
        return 0.5
    wins = 0
    for f in h2h_fixtures:
        fh_id = f["teams"]["home"]["id"]
        fa_id = f["teams"]["away"]["id"]
        fh_g = f["goals"]["home"] or 0
        fa_g = f["goals"]["away"] or 0
        # Did home_team_id win regardless of which side they played?
        if fh_id == home_team_id and fh_g > fa_g:
            wins += 1
        elif fa_id == home_team_id and fa_g > fh_g:
            wins += 1
    return wins / len(h2h_fixtures)


_MAX_CONFIDENCE = 0.80
_DRAW_MIN_PROB  = 0.28
_WIN_OR_DRAW_FALSE_BOOST = 1.06  # multiply winner's prob when API says clear winner (not double-chance)

# Composite weights — adjustments applied on top of implied odds probabilities
W_FORM    = 0.17   # form advantage (last 5)
W_H2H     = 0.10   # H2H win rate advantage
W_API_CMP = 0.08   # API composite model advantage (comparison.total)
W_REST    = 0.05   # rest/fatigue advantage


def _fatigue_score(fixtures: list[dict], team_id: int, before: datetime, days: int = 14) -> tuple[int, int]:
    """
    Return (games_in_window, days_since_last) for team_id
    looking at completed fixtures within `days` before `before`.
    """
    cutoff = before - timedelta(days=days)
    relevant = sorted(
        [
            f for f in fixtures
            if (f["teams"]["home"]["id"] == team_id or f["teams"]["away"]["id"] == team_id)
            and cutoff <= datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")) < before
        ],
        key=lambda f: f["fixture"]["date"],
        reverse=True,
    )
    games_in_window = len(relevant)
    if not relevant:
        return 0, days  # unknown — assume rested
    last_game = datetime.fromisoformat(relevant[0]["fixture"]["date"].replace("Z", "+00:00"))
    days_since = max(int((before - last_game).total_seconds() / 86400), 0)
    return games_in_window, days_since


def _fatigue_penalty(games_in_14d: int, days_since_last: int) -> float:
    """0.0 (fresh) → higher (fatigued). Max ~0.10."""
    penalty = 0.0
    if games_in_14d >= 4:
        penalty += 0.08
    elif games_in_14d == 3:
        penalty += 0.04
    if days_since_last <= 3:
        penalty += 0.04
    elif days_since_last <= 5:
        penalty += 0.02
    return min(penalty, 0.10)


def _composite_probs(
    p_home_imp: float, p_draw_imp: float, p_away_imp: float,
    form_home: float, form_away: float,
    h2h_home: float,
    fatigue_home: float = 0.0,
    fatigue_away: float = 0.0,
    api_cmp_home: float = 0.5,
    p_home_dc: float | None = None,
    p_draw_dc: float | None = None,
    p_away_dc: float | None = None,
    standings_adj: float = 0.0,
    injury_penalty_home: float = 0.0,
    injury_penalty_away: float = 0.0,
) -> tuple[float, float, float]:
    # When Dixon-Coles probabilities are available, blend them with implied odds
    W_DC = 0.30
    if p_home_dc is not None and p_draw_dc is not None and p_away_dc is not None:
        p_home_base = (1 - W_DC) * p_home_imp + W_DC * p_home_dc
        p_draw_base = (1 - W_DC) * p_draw_imp + W_DC * p_draw_dc
        p_away_base = (1 - W_DC) * p_away_imp + W_DC * p_away_dc
    else:
        p_home_base, p_draw_base, p_away_base = p_home_imp, p_draw_imp, p_away_imp

    form_advantage_home = (form_home - form_away) / 2
    h2h_advantage_home  = h2h_home - 0.5
    api_advantage_home  = api_cmp_home - 0.5

    adj_home = (p_home_base
                + W_FORM    * form_advantage_home
                + W_H2H     * h2h_advantage_home
                + W_API_CMP * api_advantage_home
                + standings_adj
                - fatigue_home
                - injury_penalty_home)
    adj_away = (p_away_base
                - W_FORM    * form_advantage_home
                - W_H2H     * h2h_advantage_home
                - W_API_CMP * api_advantage_home
                - standings_adj
                - fatigue_away
                - injury_penalty_away)
    adj_draw = p_draw_base + (fatigue_home + fatigue_away) * 0.3

    adj_home = max(adj_home, 0.01)
    adj_draw = max(adj_draw, 0.01)
    adj_away = max(adj_away, 0.01)

    total = adj_home + adj_draw + adj_away
    return adj_home / total, adj_draw / total, adj_away / total


def build_prediction(
    fixture: dict,
    odds: tuple[float, float, float],
    form_home_fixtures: list[dict],
    form_away_fixtures: list[dict],
    h2h_fixtures: list[dict],
    all_season_fixtures: list[dict] | None = None,
) -> PreMatchPrediction:
    home_team_id = fixture["teams"]["home"]["id"]
    away_team_id = fixture["teams"]["away"]["id"]
    kickoff      = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))

    home_odds, draw_odds, away_odds = odds
    probs = normalize_probabilities(home_odds, draw_odds, away_odds)

    form_home, form_home_str = _form_score(form_home_fixtures, home_team_id)
    form_away, form_away_str = _form_score(form_away_fixtures, away_team_id)
    h2h_home = _h2h_home_rate(h2h_fixtures, home_team_id)

    # Fatigue — needs full season fixture list to look back 14 days
    season_fixtures = all_season_fixtures or form_home_fixtures + form_away_fixtures
    games_14d_home, days_last_home = _fatigue_score(season_fixtures, home_team_id, kickoff)
    games_14d_away, days_last_away = _fatigue_score(season_fixtures, away_team_id, kickoff)
    fat_home = _fatigue_penalty(games_14d_home, days_last_home)
    fat_away = _fatigue_penalty(games_14d_away, days_last_away)

    c_home, c_draw, c_away = _composite_probs(
        probs.home, probs.draw, probs.away,
        form_home, form_away, h2h_home,
        fatigue_home=fat_home,
        fatigue_away=fat_away,
    )

    # Draw gets recommended if its composite prob exceeds the threshold,
    # even if home/away have a higher value — prevents systematic draw blindness
    if c_draw >= _DRAW_MIN_PROB and c_draw >= min(c_home, c_away):
        candidates = [("home", c_home), ("draw", c_draw), ("away", c_away)]
    else:
        candidates = [("home", c_home), ("away", c_away)]

    best_outcome, confidence = max(candidates, key=lambda x: x[1])

    # Cap confidence to avoid overconfidence on heavy favourites
    confidence = min(confidence, _MAX_CONFIDENCE)

    kickoff_str = fixture["fixture"]["date"]
    kickoff = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))

    return PreMatchPrediction(
        fixture_id=fixture["fixture"]["id"],
        league_id=fixture["league"]["id"],
        home_team=fixture["teams"]["home"]["name"],
        away_team=fixture["teams"]["away"]["name"],
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        kickoff=kickoff,
        home_odds=home_odds,
        draw_odds=draw_odds,
        away_odds=away_odds,
        p_home_implied=probs.home,
        p_draw_implied=probs.draw,
        p_away_implied=probs.away,
        p_home_composite=c_home,
        p_draw_composite=c_draw,
        p_away_composite=c_away,
        recommended_outcome=best_outcome,
        confidence=confidence,
        form_home=form_home,
        form_away=form_away,
        h2h_home_rate=h2h_home,
        form_home_str=form_home_str,
        form_away_str=form_away_str,
        games_in_14d_home=games_14d_home,
        games_in_14d_away=games_14d_away,
        days_since_last_home=days_last_home,
        days_since_last_away=days_last_away,
    )


def build_prediction_from_api_data(
    fixture: dict,
    odds: tuple[float, float, float],
    pred_data: dict,
    home_stats: dict | None = None,
    away_stats: dict | None = None,
    standings: list[dict] | None = None,
    injuries: list[dict] | None = None,
) -> PreMatchPrediction:
    """Build prediction using /predictions endpoint data — saves 2 API calls vs separate form+h2h."""
    from liveanalyst.logic import dc_1x2

    home_team_id = fixture["teams"]["home"]["id"]
    away_team_id = fixture["teams"]["away"]["id"]
    league_id    = fixture["league"]["id"]
    kickoff      = datetime.fromisoformat(fixture["fixture"]["date"].replace("Z", "+00:00"))

    home_odds, draw_odds, away_odds = odds
    probs = normalize_probabilities(home_odds, draw_odds, away_odds)

    teams    = pred_data.get("teams", {})
    last5_h  = teams.get("home", {}).get("last_5", {})
    last5_a  = teams.get("away", {}).get("last_5", {})
    comp     = pred_data.get("comparison", {})
    pred_sec = pred_data.get("predictions", {})

    form_home     = _form_score_from_pct(last5_h.get("form", ""))
    form_away     = _form_score_from_pct(last5_a.get("form", ""))
    form_home_str = f"{form_home:.0%}"
    form_away_str = f"{form_away:.0%}"
    h2h_home      = _h2h_home_rate(pred_data.get("h2h", []), home_team_id)
    under_over    = pred_sec.get("under_over")  # e.g. "-3.5" or "+2.5"

    # API composite signal: comparison.total.home e.g. "72.2%" → 0.722
    api_cmp_home = _form_score_from_pct(comp.get("total", {}).get("home", "50%"))

    # Home/Away split form from /teams/statistics
    form_home_home = form_home_away = None
    form_away_home = form_away_away = None
    if home_stats:
        form_home_home, form_home_away = _home_away_form_from_stats(home_stats)
        # Override overall form with venue-specific: use home team's HOME form vs away team's AWAY form
        form_home = form_home_home
    if away_stats:
        form_away_home, form_away_away = _home_away_form_from_stats(away_stats)
        form_away = form_away_away

    # Dixon-Coles expected goals from team statistics
    mu_home = mu_away = None
    p_home_dc = p_draw_dc = p_away_dc = None
    if home_stats and away_stats:
        avg_for_h, avg_against_h = _attack_defense_from_stats(home_stats)
        avg_for_a, avg_against_a = _attack_defense_from_stats(away_stats)
        league_avg_h, league_avg_a = _LEAGUE_AVG_GOALS.get(league_id, _DEFAULT_AVG_GOALS)
        # Expected goals: attack × defense × league_average
        mu_home = (avg_for_h / league_avg_h) * (avg_against_a / league_avg_a) * league_avg_h
        mu_away = (avg_for_a / league_avg_a) * (avg_against_h / league_avg_h) * league_avg_a
        mu_home = max(mu_home, 0.3)
        mu_away = max(mu_away, 0.3)
        p_home_dc, p_draw_dc, p_away_dc = dc_1x2(mu_home, mu_away)

    # Standings gap adjustment
    standings_gap = None
    standings_adj = 0.0
    if standings:
        rank_map = {s["team"]["id"]: s["rank"] for s in standings}
        home_rank = rank_map.get(home_team_id)
        away_rank = rank_map.get(away_team_id)
        if home_rank and away_rank:
            standings_gap = away_rank - home_rank
            standings_adj = _standings_gap_adjustment(home_rank, away_rank)

    # Injury penalties
    inj_penalty_home = inj_penalty_away = 0.0
    if injuries:
        inj_penalty_home = _injury_penalty(injuries, home_team_id)
        inj_penalty_away = _injury_penalty(injuries, away_team_id)

    c_home, c_draw, c_away = _composite_probs(
        probs.home, probs.draw, probs.away,
        form_home, form_away, h2h_home,
        api_cmp_home=api_cmp_home,
        p_home_dc=p_home_dc,
        p_draw_dc=p_draw_dc,
        p_away_dc=p_away_dc,
        standings_adj=standings_adj,
        injury_penalty_home=inj_penalty_home,
        injury_penalty_away=inj_penalty_away,
    )

    if c_draw >= _DRAW_MIN_PROB and c_draw >= min(c_home, c_away):
        candidates = [("home", c_home), ("draw", c_draw), ("away", c_away)]
    else:
        candidates = [("home", c_home), ("away", c_away)]

    best_outcome, confidence = max(candidates, key=lambda x: x[1])

    # win_or_draw=False means the API is very confident about a specific winner — boost slightly
    if pred_sec.get("win_or_draw") is False:
        if best_outcome == "home":
            c_home = min(c_home * _WIN_OR_DRAW_FALSE_BOOST, _MAX_CONFIDENCE)
        elif best_outcome == "away":
            c_away = min(c_away * _WIN_OR_DRAW_FALSE_BOOST, _MAX_CONFIDENCE)
        total = max(c_home, 0.01) + max(c_draw, 0.01) + max(c_away, 0.01)
        c_home /= total; c_draw /= total; c_away /= total
        confidence = c_home if best_outcome == "home" else (c_away if best_outcome == "away" else c_draw)

    confidence = min(confidence, _MAX_CONFIDENCE)

    return PreMatchPrediction(
        fixture_id=fixture["fixture"]["id"],
        league_id=league_id,
        home_team=fixture["teams"]["home"]["name"],
        away_team=fixture["teams"]["away"]["name"],
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        kickoff=kickoff,
        home_odds=home_odds,
        draw_odds=draw_odds,
        away_odds=away_odds,
        p_home_implied=probs.home,
        p_draw_implied=probs.draw,
        p_away_implied=probs.away,
        p_home_composite=c_home,
        p_draw_composite=c_draw,
        p_away_composite=c_away,
        recommended_outcome=best_outcome,
        confidence=confidence,
        form_home=form_home,
        form_away=form_away,
        h2h_home_rate=h2h_home,
        form_home_str=form_home_str,
        form_away_str=form_away_str,
        games_in_14d_home=0,
        games_in_14d_away=0,
        days_since_last_home=7,
        days_since_last_away=7,
        under_over=under_over,
        form_home_home=form_home_home,
        form_home_away=form_home_away,
        form_away_home=form_away_home,
        form_away_away=form_away_away,
        mu_home=mu_home,
        mu_away=mu_away,
        p_home_dc=p_home_dc,
        p_draw_dc=p_draw_dc,
        p_away_dc=p_away_dc,
        standings_gap=standings_gap,
        injury_penalty_home=inj_penalty_home if injuries else None,
        injury_penalty_away=inj_penalty_away if injuries else None,
    )


def fetch_predictions(
    api: APIFootballClient,
    league_ids: tuple,
    season: int,
    date: str | None = None,
    db=None,
) -> list[PreMatchPrediction]:
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fixtures = api.get_fixtures_by_date(date, league_ids, season)
    log.info("prematch: found %d fixtures on %s", len(fixtures), date)

    # Filter out already-processed fixtures before making any more API calls
    if db is not None:
        pending = []
        for f in fixtures:
            if db.get_prematch_prediction(f["fixture"]["id"]):
                log.debug("prematch: fixture_id=%s already in DB — skip", f["fixture"]["id"])
            else:
                pending.append(f)
        fixtures = pending

    if not fixtures:
        return []

    # Only fetch standings/injuries for leagues that have unprocessed fixtures
    needed_leagues = {f["league"]["id"] for f in fixtures}
    standings_cache: dict[int, list] = {}
    injuries_cache: dict[int, list] = {}
    for lid in needed_leagues:
        try:
            standings_cache[lid] = api.get_standings(lid, season)
        except Exception:
            standings_cache[lid] = []
        try:
            injuries_cache[lid] = api.get_league_injuries(lid, season, date)
        except Exception:
            injuries_cache[lid] = []

    predictions = []
    for fixture in fixtures:
        fixture_id = fixture["fixture"]["id"]
        league_id  = fixture["league"]["id"]
        home_id    = fixture["teams"]["home"]["id"]
        away_id    = fixture["teams"]["away"]["id"]
        home_name  = fixture["teams"]["home"]["name"]
        away_name  = fixture["teams"]["away"]["name"]

        odds = api.get_prematch_odds(fixture_id)
        if not odds:
            log.info("prematch: no odds for fixture_id=%s (%s vs %s)", fixture_id, home_name, away_name)
            continue

        # Fetch team statistics for xG and home/away split (2 calls, cached all season)
        home_stats = away_stats = None
        try:
            home_stats = api.get_team_statistics(home_id, league_id, season)
        except Exception as e:
            log.debug("prematch: team_stats home failed fixture_id=%s: %s", fixture_id, e)
        try:
            away_stats = api.get_team_statistics(away_id, league_id, season)
        except Exception as e:
            log.debug("prematch: team_stats away failed fixture_id=%s: %s", fixture_id, e)

        standings = standings_cache.get(league_id, [])
        injuries  = injuries_cache.get(league_id, [])

        pred_data = api.get_api_predictions(fixture_id)
        if pred_data:
            pred = build_prediction_from_api_data(
                fixture, odds, pred_data,
                home_stats=home_stats,
                away_stats=away_stats,
                standings=standings,
                injuries=injuries,
            )
        else:
            # Fallback: separate form + h2h calls
            form_home_fix = api.get_team_form(home_id, league_id, season)
            form_away_fix = api.get_team_form(away_id, league_id, season)
            h2h           = api.get_h2h(home_id, away_id)
            pred = build_prediction(fixture, odds, form_home_fix, form_away_fix, h2h)

        predictions.append(pred)
        log.info(
            "prematch: fixture_id=%s %s vs %s → %s %.0f%% mu=(%.2f,%.2f) dc=(%.0f%%,%.0f%%,%.0f%%)",
            fixture_id, home_name, away_name,
            pred.recommended_outcome, pred.confidence * 100,
            pred.mu_home or 0, pred.mu_away or 0,
            (pred.p_home_dc or 0) * 100, (pred.p_draw_dc or 0) * 100, (pred.p_away_dc or 0) * 100,
        )

    return predictions


def telegram_prematch_message(pred: PreMatchPrediction) -> str:
    league_name = _LEAGUE_NAMES.get(pred.league_id, "ליגה לא ידועה")
    kickoff_str = pred.kickoff.strftime("%H:%M")

    outcome_label = {
        "home": f"ניצחון {pred.home_team}",
        "draw": "תיקו",
        "away": f"ניצחון {pred.away_team}",
    }[pred.recommended_outcome]

    form_h = "".join(_RESULT_EMOJIS.get(r, "?") for r in pred.form_home_str)
    form_a = "".join(_RESULT_EMOJIS.get(r, "?") for r in pred.form_away_str)

    # Home/Away split form lines (shown when available)
    home_split = ""
    if pred.form_home_home is not None and pred.form_home_away is not None:
        home_split = f"  (בית: {pred.form_home_home:.0%} | חוץ: {pred.form_home_away:.0%})\n"
    away_split = ""
    if pred.form_away_home is not None and pred.form_away_away is not None:
        away_split = f"  (בית: {pred.form_away_home:.0%} | חוץ: {pred.form_away_away:.0%})\n"

    # Dixon-Coles / Poisson model line
    dc_line = ""
    if pred.p_home_dc is not None:
        dc_line = (
            f"🧮 Poisson: בית {pred.p_home_dc:.0%} | תיקו {pred.p_draw_dc:.0%} | חוץ {pred.p_away_dc:.0%}"
            f"  (μ={pred.mu_home:.2f}/{pred.mu_away:.2f})\n"
        )

    # Under/Over signal
    uo_line = ""
    if pred.under_over:
        direction = "Over" if pred.under_over.startswith("+") else "Under"
        uo_line = f"📉 API צופה: {direction} {pred.under_over}\n"

    # Injury summary
    inj_line = ""
    if pred.injury_penalty_home is not None and pred.injury_penalty_away is not None:
        if pred.injury_penalty_home > 0 or pred.injury_penalty_away > 0:
            inj_line = (
                f"🏥 פגיעות: {pred.home_team} -{pred.injury_penalty_home:.0%} | "
                f"{pred.away_team} -{pred.injury_penalty_away:.0%}\n"
            )

    return (
        f"🔮 תחזית טרום משחק | {league_name}\n"
        f"{pred.home_team} נגד {pred.away_team}\n"
        f"⏰ קיקאוף: {kickoff_str}\n\n"
        f"📊 אודס: {pred.home_odds:.2f} | {pred.draw_odds:.2f} | {pred.away_odds:.2f}\n"
        f"📈 הסתברות (אודס): בית {pred.p_home_implied:.0%} | תיקו {pred.p_draw_implied:.0%} | חוץ {pred.p_away_implied:.0%}\n"
        f"{dc_line}"
        f"{uo_line}\n"
        f"📋 פורם (EMA — 5 משחקים, אחרון משוקלל יותר):\n"
        f"  {pred.home_team}: {form_h} ({pred.form_home:.0%})\n"
        f"{home_split}"
        f"  {pred.away_team}: {form_a} ({pred.form_away:.0%})\n"
        f"{away_split}\n"
        f"⚔️ H2H: {pred.home_team} ניצח {pred.h2h_home_rate:.0%} מהמפגשים\n"
        f"{inj_line}\n"
        f"🎯 המלצה: {outcome_label}\n"
        f"📊 ביטחון: {pred.confidence:.0%}\n\n"
        f"⚠️ לצורכי מחקר בלבד — לא המלצה פיננסית."
    )
>>>>>>> Stashed changes
