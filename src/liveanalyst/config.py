from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    api_football_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    api_football_base_url: str = "https://v3.football.api-sports.io"
    league_ids: tuple = (39,)   # Premier League default; extend via LEAGUE_IDS env var
    season: int = 2025

    @staticmethod
    def from_env() -> "Settings":
        raw = os.getenv("LEAGUE_IDS", os.getenv("LEAGUE_ID", "39"))
        league_ids = tuple(int(x.strip()) for x in raw.split(",") if x.strip())
        return Settings(
            postgres_dsn=os.environ["POSTGRES_DSN"],
            api_football_key=os.environ["API_FOOTBALL_KEY"],
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            api_football_base_url=os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io"),
            league_ids=league_ids,
            season=int(os.getenv("SEASON", "2025")),
        )
