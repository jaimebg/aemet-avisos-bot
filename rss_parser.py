from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from config import (
    AEMET_BASE_URL,
    HTTP_MAX_CONCURRENCY,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    HTTP_USER_AGENT,
    LEVEL_RANK,
    REGIONS,
    RSS_INDEX_URL_TEMPLATE,
    UNKNOWN_LEVEL_RANK,
)

logger = logging.getLogger(__name__)

# One semaphore for the whole process, not one per call. Both callers -- the
# polling job and the per-user /avisos command -- share it, so N concurrent
# /avisos invocations cannot multiply the request rate we present to AEMET and
# get the source IP throttled (which would degrade the push deliveries too).
#
# It is built lazily on first use inside a running loop rather than at import
# time: an asyncio.Semaphore created outside a loop binds to the wrong one. It
# is also rebuilt whenever the running loop changes, so a semaphore bound to a
# closed loop can never leak into the next one (pytest-asyncio gives every test
# its own loop).
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Return the semaphore bounding HTTP concurrency on the running loop."""
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(HTTP_MAX_CONCURRENCY)
        _semaphore_loop = loop
    return _semaphore


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


def build_client() -> httpx.AsyncClient:
    """Build the shared HTTP client used to talk to AEMET.

    Callers own the client's lifecycle (use it as an `async with` context).
    """
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": HTTP_USER_AGENT},
        follow_redirects=True,
    )


async def _get(
    client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
) -> bytes | None:
    """Fetch a URL's body, retrying on network errors and 5xx responses.

    Never raises: a 4xx response is logged and returns None immediately
    (not retried); a network error or 5xx response is retried up to
    HTTP_MAX_RETRIES times with exponential backoff, then logged and
    returns None. The semaphore bounds how many requests are in flight at
    once and is only held for the duration of the actual request.
    """
    last_error: Exception | None = None
    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            async with semaphore:
                response = await client.get(url)
            response.raise_for_status()
            return response.content
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                logger.warning(
                    "HTTP %s fetching %s: %s", exc.response.status_code, url, exc
                )
                return None
            last_error = exc
        except httpx.HTTPError as exc:
            last_error = exc

        if attempt < HTTP_MAX_RETRIES:
            await asyncio.sleep(0.5 * 2**attempt)

    logger.warning(
        "Giving up on %s after %d attempt(s): %s",
        url,
        HTTP_MAX_RETRIES + 1,
        last_error,
    )
    return None


async def _discover_feed_urls(
    region_code: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore
) -> list[str]:
    """Scrape the AEMET index page for a region to find per-zone RSS XML links."""
    index_url = RSS_INDEX_URL_TEMPLATE.format(code=region_code)
    data = await _get(client, index_url, semaphore)
    if data is None:
        return []

    page_html = data.decode("iso-8859-15", errors="replace")
    paths = _RSS_LINK_RE.findall(page_html)
    # Deduplicate while preserving order
    seen: set[str] = set()
    urls: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            urls.append(AEMET_BASE_URL + path)
    return urls


def _parse_feed_bytes(data: bytes, source_url: str) -> list[Alert]:
    """Parse a single RSS feed body and return alerts (skipping summary items).

    Pure and synchronous: no network access, so it stays testable offline.
    Never raises: malformed feed data simply yields no alerts.
    """
    try:
        feed = feedparser.parse(data)
    except Exception:
        logger.exception("Error parsing feed %s", source_url)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("Feed error for %s: %s", source_url, feed.bozo_exception)
        return []

    alerts: list[Alert] = []
    for entry in feed.entries:
        title = entry.get("title", "")
        guid = entry.get("id") or entry.get("link", "")
        # Skip "Estado completo de avisos" summary items.
        if title.startswith("Estado completo"):
            continue
        # Belt and braces on the same summary item, in case AEMET rewords that
        # title: it carries no level word, so it would rank UNKNOWN_LEVEL_RANK
        # and reach even min_level="rojo" subscribers. Real alerts are CAP
        # .xml documents; the summary is a .tar.gz. This rule fails closed, so
        # log what it drops -- if AEMET ever changes the guid shape, that log
        # line is the only warning an operator gets.
        if not guid.endswith(".xml"):
            logger.warning(
                "Skipping non-CAP entry in %s: guid %r, title %r",
                source_url,
                guid,
                title,
            )
            continue
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


async def _fetch_region_alerts(
    region_code: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore
) -> list[Alert]:
    """Discover, fetch and parse all feeds for one region, deduplicated."""
    feed_urls = await _discover_feed_urls(region_code, client, semaphore)
    if not feed_urls:
        logger.info("No RSS feeds found for region %s", region_code)
        return []

    bodies = await asyncio.gather(
        *(_get(client, url, semaphore) for url in feed_urls),
        return_exceptions=False,
    )

    all_alerts: list[Alert] = []
    seen_ids: set[str] = set()
    for url, body in zip(feed_urls, bodies, strict=True):
        if body is None:
            continue
        for alert in _parse_feed_bytes(body, url):
            if alert.canonical_id not in seen_ids:
                seen_ids.add(alert.canonical_id)
                all_alerts.append(alert)
    return all_alerts


async def fetch_alerts(region_code: str, client: httpx.AsyncClient) -> list[Alert]:
    """Fetch all alerts for a single region by discovering and parsing its feeds.

    Alerts are deduplicated across the region's own feeds by canonical_id.
    Shares the process-wide HTTP semaphore with every other caller, so it is
    safe to call directly (outside of fetch_alerts_for_regions).
    """
    return await _fetch_region_alerts(region_code, client, _get_semaphore())


async def fetch_alerts_for_regions(
    region_codes: Sequence[str], client: httpx.AsyncClient
) -> dict[str, list[Alert]]:
    """Fetch alerts for several regions concurrently.

    The process-wide semaphore bounds HTTP concurrency across every region --
    and across concurrent calls to this function -- so real concurrency never
    exceeds HTTP_MAX_CONCURRENCY. A region whose fetch fails maps to an empty
    list and is logged; it never aborts the others.
    """
    semaphore = _get_semaphore()

    async def _safe_fetch(region_code: str) -> list[Alert]:
        try:
            return await _fetch_region_alerts(region_code, client, semaphore)
        except Exception:
            logger.exception("Error fetching alerts for region %s", region_code)
            return []

    results = await asyncio.gather(*(_safe_fetch(code) for code in region_codes))
    return dict(zip(region_codes, results, strict=True))
