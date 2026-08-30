from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "subscriptions.db")

AEMET_BASE_URL = "https://www.aemet.es"

HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
HTTP_MAX_CONCURRENCY = int(os.getenv("HTTP_MAX_CONCURRENCY", "8"))
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "2"))
HTTP_USER_AGENT = "aemet-avisos-bot/0.2 (+https://github.com/jaimebg/aemet-avisos-bot)"

LEVELS: tuple[str, ...] = ("amarillo", "naranja", "rojo")
LEVEL_RANK: dict[str, int] = {"amarillo": 1, "naranja": 2, "rojo": 3}
DEFAULT_MIN_LEVEL = "amarillo"
UNKNOWN_LEVEL_RANK = 3

REGIONS: dict[str, str] = {
    "and": "Andalucía",
    "ara": "Aragón",
    "ast": "Asturias",
    "bal": "Illes Balears",
    "can": "Cantabria",
    "cat": "Cataluña",
    "ceu": "Ceuta",
    "clm": "Castilla-La Mancha",
    "coo": "Canarias",
    "cyl": "Castilla y León",
    "ext": "Extremadura",
    "gal": "Galicia",
    "mad": "Madrid",
    "mel": "Melilla",
    "mur": "Murcia",
    "nav": "Navarra",
    "pva": "País Vasco",
    "rio": "La Rioja",
    "val": "C. Valenciana",
}

RSS_INDEX_URL_TEMPLATE = "https://www.aemet.es/es/rss_info/avisos/{code}"


class ConfigError(RuntimeError):
    pass


def validate() -> None:
    """Validate required configuration. Called from bot.main() before startup."""
    if not TELEGRAM_TOKEN:
        raise ConfigError(
            "TELEGRAM_TOKEN is not set. Copy .env.example to .env and add your "
            "token from @BotFather."
        )
    if POLL_INTERVAL_SECONDS < 60:
        raise ConfigError(
            f"POLL_INTERVAL_SECONDS must be at least 60 (got {POLL_INTERVAL_SECONDS})."
        )
