from __future__ import annotations

import logging
from dataclasses import dataclass

import feedparser

from config import REGIONS, RSS_URL_TEMPLATE

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {
    "amarillo": "🟡",
    "naranja": "🟠",
    "rojo": "🔴",
}


@dataclass
class Alert:
    title: str
    description: str
    link: str
    guid: str
    pub_date: str
    level: str | None  # amarillo / naranja / rojo

    @property
    def emoji(self) -> str:
        return LEVEL_EMOJI.get(self.level or "", "⚠️")

    def format_message(self, region_code: str) -> str:
        region_name = REGIONS.get(region_code, region_code)
        level_display = (self.level or "Desconocido").upper()

        lines = [
            f"{self.emoji} Aviso {level_display}",
            f"📍 {region_name}",
            f"📝 {self.title}",
        ]
        if self.description:
            lines.append(f"\n{self.description}")
        lines.append(f"\n🔗 <a href=\"{self.link}\">Más información</a>")
        return "\n".join(lines)


def _parse_level(title: str) -> str | None:
    title_lower = title.lower()
    for level in ("rojo", "naranja", "amarillo"):
        if level in title_lower:
            return level
    return None


def fetch_alerts(region_code: str) -> list[Alert]:
    url = RSS_URL_TEMPLATE.format(code=region_code)
    try:
        feed = feedparser.parse(url)
    except Exception:
        logger.exception("Error fetching RSS for %s", region_code)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("Feed error for %s: %s", region_code, feed.bozo_exception)
        return []

    alerts: list[Alert] = []
    for entry in feed.entries:
        guid = entry.get("id") or entry.get("link", "")
        title = entry.get("title", "")
        alerts.append(
            Alert(
                title=title,
                description=entry.get("summary", ""),
                link=entry.get("link", ""),
                guid=guid,
                pub_date=entry.get("published", ""),
                level=_parse_level(title),
            )
        )
    return alerts
