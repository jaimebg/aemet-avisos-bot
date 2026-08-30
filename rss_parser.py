from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

import feedparser

from config import (
    AEMET_BASE_URL,
    LEVEL_RANK,
    REGIONS,
    RSS_INDEX_URL_TEMPLATE,
    UNKNOWN_LEVEL_RANK,
)

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

# Matches AEMET's validity phrasing, e.g.:
#   "... de 13:00 31-08-2026 CEST (UTC+2) a 20:59 31-08-2026 CEST (UTC+2)."
# The offset digits are optional to allow a bare "(UTC)", treated as UTC+0.
_VALIDITY_RE = re.compile(
    r"de (?P<sh>\d{2}):(?P<smin>\d{2}) (?P<sd>\d{2})-(?P<smo>\d{2})-(?P<sy>\d{4})"
    r" \w+ \(UTC(?P<soff>[+-]?\d+)?\)"
    r" a (?P<eh>\d{2}):(?P<emin>\d{2}) (?P<ed>\d{2})-(?P<emo>\d{2})-(?P<ey>\d{4})"
    r" \w+ \(UTC(?P<eoff>[+-]?\d+)?\)"
)


@dataclass
class Alert:
    title: str
    description: str
    link: str
    guid: str
    pub_date: str
    level: str | None  # amarillo / naranja / rojo
    zone: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

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

    @property
    def level_rank(self) -> int:
        return LEVEL_RANK.get(self.level, UNKNOWN_LEVEL_RANK)

    def format_message(
        self, region_code: str, *, previous_level: str | None = None
    ) -> str:
        region_name = html.escape(REGIONS.get(region_code, region_code), quote=True)
        title = html.escape(self.title, quote=True)
        description = html.escape(self.description, quote=True)
        link = html.escape(self.link, quote=True)
        level_display = self.level.upper() if self.level else "DESCONOCIDO"

        if previous_level is not None and previous_level != self.level:
            header = f"🔺 Aviso ACTUALIZADO: {previous_level.upper()} → {level_display}"
        else:
            header = f"{self.emoji} Aviso {level_display}"

        location_line = f"📍 {region_name}"
        if self.zone is not None:
            location_line += f" · {html.escape(self.zone, quote=True)}"

        lines = [header, location_line, f"📝 {title}"]

        if self.starts_at is not None and self.ends_at is not None:
            start = self.starts_at.strftime("%d/%m %H:%M")
            end = self.ends_at.strftime("%d/%m %H:%M")
            lines.append(f"🕒 {start} → {end}")

        if self.description:
            lines.append("")
            lines.append(description)

        lines.append("")
        lines.append(f'🔗 <a href="{link}">Más información</a>')
        return "\n".join(lines)


def _parse_level(title: str) -> str | None:
    title_lower = title.lower()
    for level in ("rojo", "naranja", "amarillo"):
        if level in title_lower:
            return level
    return None


def _parse_zone(title: str) -> str | None:
    """Extract the zone name from an AEMET title, e.g.:

    "Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa"
    -> "Campiña cordobesa"

    Titles are dot-separated: "Aviso", the level, the phenomenon, and the
    zone. Only titles with four or more segments carry a zone.
    """
    segments = title.split(". ")
    if len(segments) < 4:
        return None
    return segments[-1].removesuffix(".")


def _parse_validity(description: str) -> tuple[datetime | None, datetime | None]:
    """Parse the validity window out of an AEMET description, e.g.:

    "... de 13:00 31-08-2026 CEST (UTC+2) a 20:59 31-08-2026 CEST (UTC+2)."

    Returns (None, None) if the phrase is missing or malformed. Never raises.
    """
    match = _VALIDITY_RE.search(description)
    if not match:
        return None, None

    g = match.groupdict()
    try:
        start_offset = int(g["soff"]) if g["soff"] else 0
        end_offset = int(g["eoff"]) if g["eoff"] else 0
        starts_at = datetime(
            int(g["sy"]),
            int(g["smo"]),
            int(g["sd"]),
            int(g["sh"]),
            int(g["smin"]),
            tzinfo=timezone(timedelta(hours=start_offset)),
        )
        ends_at = datetime(
            int(g["ey"]),
            int(g["emo"]),
            int(g["ed"]),
            int(g["eh"]),
            int(g["emin"]),
            tzinfo=timezone(timedelta(hours=end_offset)),
        )
    except ValueError:
        return None, None
    return starts_at, ends_at


def _discover_feed_urls(region_code: str) -> list[str]:
    """Scrape the AEMET index page for a region to find per-zone RSS XML links."""
    index_url = RSS_INDEX_URL_TEMPLATE.format(code=region_code)
    try:
        with urlopen(index_url, timeout=30) as resp:
            page_html = resp.read().decode("iso-8859-15", errors="replace")
    except Exception:
        logger.exception("Error fetching RSS index for %s", region_code)
        return []

    paths = _RSS_LINK_RE.findall(page_html)
    # Deduplicate while preserving order
    seen = set()
    urls = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            urls.append(AEMET_BASE_URL + path)
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
        description = entry.get("summary", "")
        starts_at, ends_at = _parse_validity(description)
        alerts.append(
            Alert(
                title=title,
                description=description,
                link=entry.get("link", ""),
                guid=guid,
                pub_date=entry.get("published", ""),
                level=_parse_level(title),
                zone=_parse_zone(title),
                starts_at=starts_at,
                ends_at=ends_at,
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
