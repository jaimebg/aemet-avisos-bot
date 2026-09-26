from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import MutableMapping, Sequence
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

# The level as AEMET states it, e.g. "Aviso. Nivel amarillo. ...". Anchoring on
# "Nivel" keeps a level word elsewhere in the title (a zone name, say) from
# being mistaken for the alert's severity.
_LEVEL_RE = re.compile(r"\bnivel (amarillo|naranja|rojo)\b", re.IGNORECASE)

# Matches AEMET's validity phrasing, e.g.:
#   "... de 13:00 31-08-2026 CEST (UTC+2) a 20:59 31-08-2026 CEST (UTC+2)."
# The offset digits are optional to allow a bare "(UTC)", treated as UTC+0.
# The zone abbreviation (CEST, WEST, ...) is kept so messages can show it.
_VALIDITY_RE = re.compile(
    r"de (?P<sh>\d{2}):(?P<smin>\d{2}) (?P<sd>\d{2})-(?P<smo>\d{2})-(?P<sy>\d{4})"
    r" (?P<stz>\w+) \(UTC(?P<soff>[+-]?\d+)?\)"
    r" a (?P<eh>\d{2}):(?P<emin>\d{2}) (?P<ed>\d{2})-(?P<emo>\d{2})-(?P<ey>\d{4})"
    r" (?P<etz>\w+) \(UTC(?P<eoff>[+-]?\d+)?\)"
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
            lines.append(f"🕒 {_format_window(self.starts_at, self.ends_at)}")

        if self.description:
            lines.append("")
            lines.append(description)

        lines.append("")
        lines.append(f'🔗 <a href="{link}">Más información</a>')
        return "\n".join(lines)

    def is_expired(self, now: datetime) -> bool:
        """True once the validity window has closed. Unknown windows never expire."""
        return self.ends_at is not None and self.ends_at <= now


def _format_window(starts_at: datetime, ends_at: datetime) -> str:
    """Render a validity window in the alert's own time zone, labelled.

    Canarias runs an hour behind the peninsula, so a bare "13:00" is
    ambiguous for anyone following more than one region.
    """
    start = starts_at.strftime("%d/%m %H:%M")
    end = ends_at.strftime("%d/%m %H:%M")
    start_tz = starts_at.tzname()
    end_tz = ends_at.tzname()
    if start_tz == end_tz:
        return f"{start} → {end} ({start_tz})"
    return f"{start} ({start_tz}) → {end} ({end_tz})"


def _parse_level(title: str) -> str | None:
    match = _LEVEL_RE.search(title)
    if match:
        return match.group(1).lower()
    # Fallback for a reworded title that no longer says "Nivel X": any level
    # word, most severe first, so a doubtful alert is never under-ranked.
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
            tzinfo=timezone(timedelta(hours=start_offset), g["stz"]),
        )
        ends_at = datetime(
            int(g["ey"]),
            int(g["emo"]),
            int(g["ed"]),
            int(g["eh"]),
            int(g["emin"]),
            tzinfo=timezone(timedelta(hours=end_offset), g["etz"]),
        )
    except ValueError:
        return None, None
    return starts_at, ends_at


def _now() -> datetime:
    """Current time; a seam so tests can pin the clock expiry is judged against."""
    return datetime.now(timezone.utc)


def build_client() -> httpx.AsyncClient:
    """Build an HTTP client configured to talk to AEMET.

    Callers own the client's lifecycle. The bot itself goes through
    shared_client() so connections are reused across poll cycles.
    """
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": HTTP_USER_AGENT},
        follow_redirects=True,
    )


# Key under which the application's long-lived client lives in bot_data.
HTTP_CLIENT_KEY = "http_client"


def shared_client(store: MutableMapping[str, object]) -> httpx.AsyncClient:
    """Return the application-wide client kept in `store`, creating it lazily.

    One client for the life of the process keeps TCP/TLS connections to
    aemet.es alive between cycles instead of re-handshaking every few minutes.
    """
    client = store.get(HTTP_CLIENT_KEY)
    if client is None or client.is_closed:
        client = build_client()
        store[HTTP_CLIENT_KEY] = client
    return client


async def close_shared_client(store: MutableMapping[str, object]) -> None:
    """Close and forget the client created by shared_client(), if any."""
    client = store.pop(HTTP_CLIENT_KEY, None)
    if client is not None:
        await client.aclose()


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
) -> list[Alert] | None:
    """Discover, fetch and parse all feeds for one region, deduplicated.

    Returns None when the region could not be read at all: no feed links on
    its index page (unreachable, or AEMET changed the markup) or not one feed
    body fetched. Every region's index lists its zone feeds even when there
    are no alerts, so an empty discovery is a failure, never "all clear".
    Expired alerts are dropped here, so no caller ever shows or sends one.
    """
    feed_urls = await _discover_feed_urls(region_code, client, semaphore)
    if not feed_urls:
        logger.warning("No RSS feeds found for region %s", region_code)
        return None

    bodies = await asyncio.gather(
        *(_get(client, url, semaphore) for url in feed_urls),
        return_exceptions=False,
    )
    if all(body is None for body in bodies):
        logger.warning(
            "None of the %d feed(s) of region %s could be fetched",
            len(feed_urls),
            region_code,
        )
        return None

    now = _now()
    all_alerts: list[Alert] = []
    seen_ids: set[str] = set()
    for url, body in zip(feed_urls, bodies, strict=True):
        if body is None:
            continue
        for alert in _parse_feed_bytes(body, url):
            if alert.is_expired(now):
                continue
            if alert.canonical_id not in seen_ids:
                seen_ids.add(alert.canonical_id)
                all_alerts.append(alert)
    return all_alerts


async def fetch_alerts(
    region_code: str, client: httpx.AsyncClient
) -> list[Alert] | None:
    """Fetch all alerts for a single region by discovering and parsing its feeds.

    Alerts are deduplicated across the region's own feeds by canonical_id.
    Returns None if the region could not be read (see _fetch_region_alerts).
    Shares the process-wide HTTP semaphore with every other caller, so it is
    safe to call directly (outside of fetch_alerts_for_regions).
    """
    return await _fetch_region_alerts(region_code, client, _get_semaphore())


async def fetch_alerts_for_regions(
    region_codes: Sequence[str], client: httpx.AsyncClient
) -> dict[str, list[Alert] | None]:
    """Fetch alerts for several regions concurrently.

    The process-wide semaphore bounds HTTP concurrency across every region --
    and across concurrent calls to this function -- so real concurrency never
    exceeds HTTP_MAX_CONCURRENCY. A region that could not be read maps to
    None (as opposed to [] for "read fine, no alerts") and is logged; it never
    aborts the others.
    """
    semaphore = _get_semaphore()

    async def _safe_fetch(region_code: str) -> list[Alert] | None:
        try:
            return await _fetch_region_alerts(region_code, client, semaphore)
        except Exception:
            logger.exception("Error fetching alerts for region %s", region_code)
            return None

    results = await asyncio.gather(*(_safe_fetch(code) for code in region_codes))
    return dict(zip(region_codes, results, strict=True))
