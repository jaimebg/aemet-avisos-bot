from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Numeric settings stay plain module-level constants (tests monkeypatch them
# directly), but a malformed value must not escape as a raw ValueError
# traceback at import time -- that would bypass main()'s friendly ConfigError
# path entirely. The helpers below fall back to the default and record the
# problem for validate() to report.
_ENV_ERRORS: list[str] = []


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment; defer a malformed value to validate()."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        _ENV_ERRORS.append(f"{name} must be a whole number (got {raw!r})")
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment; defer a malformed value to validate()."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        _ENV_ERRORS.append(f"{name} must be a number (got {raw!r})")
        return default


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

POLL_INTERVAL_SECONDS = _env_int("POLL_INTERVAL_SECONDS", 300)

DATABASE_PATH = os.getenv("DATABASE_PATH", "subscriptions.db")

# How long a delivered alert stays in seen_alerts before being pruned, and how
# often that pruning runs (it is far cheaper than one sweep per poll cycle).
SEEN_RETENTION_DAYS = _env_int("SEEN_RETENTION_DAYS", 7)
CLEANUP_INTERVAL_SECONDS = _env_int("CLEANUP_INTERVAL_SECONDS", 3600)

AEMET_BASE_URL = "https://www.aemet.es"

HTTP_TIMEOUT_SECONDS = _env_float("HTTP_TIMEOUT_SECONDS", 20.0)
HTTP_MAX_CONCURRENCY = _env_int("HTTP_MAX_CONCURRENCY", 8)
HTTP_MAX_RETRIES = _env_int("HTTP_MAX_RETRIES", 2)
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


def rank_for(min_level: str | None) -> int:
    """Rank of a *stored minimum-level preference*, defaulting when unrecognised.

    Answers only "how severe must an alert be before this user wants it?".
    A missing row (None) or a value the code no longer knows about degrades to
    the rank of DEFAULT_MIN_LEVEL rather than raising, so a hand-edited or
    future-schema row can never crash a delivery.

    Deliberately NOT the same question as Alert.level_rank (an unknown
    *incoming* alert level ranks UNKNOWN_LEVEL_RANK so the filter never
    discards it) or bot._stored_rank (an unknown *stored alert* level ranks 0
    so any known level escalates over it). The three fallbacks differ on
    purpose; do not unify them.
    """
    default_rank = LEVEL_RANK[DEFAULT_MIN_LEVEL]
    if min_level is None:
        return default_rank
    return LEVEL_RANK.get(min_level, default_rank)


class ConfigError(RuntimeError):
    pass


def validate() -> None:
    """Validate required configuration. Called from bot.main() before startup."""
    if _ENV_ERRORS:
        raise ConfigError(
            "Invalid value(s) in the environment (check your .env): "
            + "; ".join(_ENV_ERRORS)
            + "."
        )
    if not TELEGRAM_TOKEN:
        raise ConfigError(
            "TELEGRAM_TOKEN is not set. Copy .env.example to .env and add your "
            "token from @BotFather."
        )
    if POLL_INTERVAL_SECONDS < 60:
        raise ConfigError(
            f"POLL_INTERVAL_SECONDS must be at least 60 (got {POLL_INTERVAL_SECONDS})."
        )
