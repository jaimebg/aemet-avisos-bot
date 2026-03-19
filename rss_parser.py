from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.request import urlopen

import feedparser

from config import REGIONS, RSS_INDEX_URL_TEMPLATE

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {
    "amarillo": "🟡",
    "naranja": "🟠",
    "rojo": "🔴",
}

# AEMET GUIDs look like Z_CAP_C_LEMM_20260224101529_AFAZ711501COCO2521.xml
# The timestamp (14 digits) changes on every republication; the suffix is stable.
_GUID_TIMESTAMP_RE = re.compile(r"Z_CAP_C_LEMM_\d{14}_(.+)")

_RSS_LINK_RE = re.compile(
    r'href="(/documentos_d/eltiempo/prediccion/avisos/rss/[^"]*_RSS\.xml)"'
)

BASE_URL = "https://www.aemet.es"


@dataclass
class Alert:
    title: str
    description: str
    link: str
    guid: str
    pub_date: str
    level: str | None  # amarillo / naranja / rojo

    @property
    def canonical_id(self) -> str:
        """Stable identifier that doesn't change when AEMET republishes the same alert.

        GUIDs may be full URLs like:
          https://www.aemet.es/.../Z_CAP_C_LEMM_20260319104642_AFAZ659201VIRM2121.xml
        or bare filenames like:
          Z_CAP_C_LEMM_20260319104642_AFAZ659201VIRM2121.xml
        We use search() (not match()) so the pattern works regardless of prefix.
        """
        m = _GUID_TIMESTAMP_RE.search(self.guid)
        return m.group(1) if m else self.guid

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


def _discover_feed_urls(region_code: str) -> list[str]:
    """Scrape the AEMET index page for a region to find per-zone RSS XML links."""
    index_url = RSS_INDEX_URL_TEMPLATE.format(code=region_code)
    try:
        with urlopen(index_url, timeout=30) as resp:
            html = resp.read().decode("iso-8859-15", errors="replace")
    except Exception:
        logger.exception("Error fetching RSS index for %s", region_code)
        return []

    paths = _RSS_LINK_RE.findall(html)
    # Deduplicate while preserving order
    seen = set()
    urls = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            urls.append(BASE_URL + path)
    return urls


def _parse_feed(url: str) -> list[Alert]:
    """Parse a single RSS feed URL and return alerts (skipping summary items)."""
    try:
        feed = feedparser.parse(url)
    except Exception:
        logger.exception("Error parsing feed %s", url)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("Feed error for %s: %s", url, feed.bozo_exception)
        return []

    alerts: list[Alert] = []
    for entry in feed.entries:
        title = entry.get("title", "")
        # Skip "Estado completo de avisos" summary items
        if title.startswith("Estado completo"):
            continue
        guid = entry.get("id") or entry.get("link", "")
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


def fetch_alerts(region_code: str) -> list[Alert]:
    """Fetch all alerts for a region by discovering and parsing its RSS feeds."""
    feed_urls = _discover_feed_urls(region_code)
    if not feed_urls:
        logger.info("No RSS feeds found for region %s", region_code)
        return []

    all_alerts: list[Alert] = []
    seen_ids: set[str] = set()
    for url in feed_urls:
        for alert in _parse_feed(url):
            if alert.canonical_id not in seen_ids:
                seen_ids.add(alert.canonical_id)
                all_alerts.append(alert)
    return all_alerts
