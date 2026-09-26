"""Tests for the async, concurrent, retrying HTTP layer of rss_parser.py.

Every test runs offline through httpx.MockTransport. No test reaches
aemet.es.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

import rss_parser
from config import RSS_INDEX_URL_TEMPLATE
from rss_parser import (
    _discover_feed_urls,
    _parse_feed_bytes,
    fetch_alerts,
    fetch_alerts_for_regions,
)

# The fixtures describe alerts valid on 31-08-2026. Judge expiry against a
# clock from before then, so these tests do not start failing as time passes.
FIXTURE_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(rss_parser, "_now", lambda: FIXTURE_NOW)


async def _instant_sleep(*_args, **_kwargs) -> None:
    """Drop-in replacement for asyncio.sleep that returns immediately."""
    return None


def _index_html(paths: list[str]) -> bytes:
    """Build a minimal AEMET-style index page listing the given feed paths."""
    links = "".join(f'<a href="{path}">RSS</a>' for path in paths)
    return f"<html><body>{links}</body></html>".encode("iso-8859-15")


def _feed_xml(guid_suffix: str, title: str) -> bytes:
    """Build a minimal single-item RSS 2.0 feed with a real alert entry."""
    guid = f"Z_CAP_C_LEMM_20260830090212_{guid_suffix}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test feed</title>
<item>
<title>{title}</title>
<description>Descripcion de prueba.</description>
<link>https://www.aemet.es/aviso/{guid}</link>
<guid>{guid}</guid>
<pubDate>2026-08-30T09:02:12+00:00</pubDate>
</item>
</channel></rss>""".encode()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _index_url(region_code: str) -> str:
    return RSS_INDEX_URL_TEMPLATE.format(code=region_code)


# --- test 1: cross-feed de-duplication -----------------------------------


async def test_fetch_alerts_dedupes_identical_alert_across_three_feeds(read_fixture):
    index_html = read_fixture("index_mad.html")
    feed_bytes = read_fixture("feed_cordoba_amarillo.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _index_url("mad"):
            return httpx.Response(200, content=index_html)
        return httpx.Response(200, content=feed_bytes)

    async with _client(handler) as client:
        alerts = await fetch_alerts("mad", client)

    assert len(alerts) == 1
    assert alerts[0].canonical_id == "AFAZ611402ATTA3119.xml"


# --- test 2: two feeds, two different alerts, feed order preserved --------


async def test_fetch_alerts_returns_both_alerts_from_different_feeds_in_order():
    feed_a_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_A_RSS.xml"
    feed_b_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_B_RSS.xml"
    index_html = _index_html([feed_a_path, feed_b_path])
    feed_a = _feed_xml("AAAA1111.xml", "Aviso. Nivel amarillo. Viento. Zona A")
    feed_b = _feed_xml("BBBB2222.xml", "Aviso. Nivel naranja. Lluvia. Zona B")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _index_url("xx"):
            return httpx.Response(200, content=index_html)
        if request.url.path == feed_a_path:
            return httpx.Response(200, content=feed_a)
        return httpx.Response(200, content=feed_b)

    async with _client(handler) as client:
        alerts = await fetch_alerts("xx", client)

    assert [a.canonical_id for a in alerts] == ["AAAA1111.xml", "BBBB2222.xml"]


# --- test 3: index 404 yields [] without raising --------------------------


async def test_fetch_alerts_returns_none_when_index_page_404s():
    """An unreadable region is None, never a false "no alerts" []."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    async with _client(handler) as client:
        alerts = await fetch_alerts("xx", client)

    assert alerts is None


# --- test 4: one feed 500s persistently, other feed still succeeds -------


async def test_fetch_alerts_skips_a_feed_that_always_500s(monkeypatch):
    monkeypatch.setattr(rss_parser.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(rss_parser, "HTTP_MAX_RETRIES", 2)

    broken_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_BROKEN_RSS.xml"
    ok_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_OK_RSS.xml"
    index_html = _index_html([broken_path, ok_path])
    feed_ok = _feed_xml("OKOK3333.xml", "Aviso. Nivel amarillo. Viento. Zona OK")
    broken_call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal broken_call_count
        if str(request.url) == _index_url("xx"):
            return httpx.Response(200, content=index_html)
        if request.url.path == broken_path:
            broken_call_count += 1
            return httpx.Response(500, content=b"server error")
        return httpx.Response(200, content=feed_ok)

    async with _client(handler) as client:
        alerts = await fetch_alerts("xx", client)

    assert [a.canonical_id for a in alerts] == ["OKOK3333.xml"]
    # HTTP_MAX_RETRIES is pinned to 2 above: 2 retries after the first
    # attempt, 3 attempts total before _get gives up on the broken feed.
    assert broken_call_count == 3


# --- test 5: retry then succeed --------------------------------------------


async def test_get_retries_once_on_500_then_succeeds(monkeypatch):
    monkeypatch.setattr(rss_parser.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(rss_parser, "HTTP_MAX_RETRIES", 2)
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500, content=b"server error")
        return httpx.Response(200, content=b"ok body")

    async with _client(handler) as client:
        body = await rss_parser._get(
            client, "https://www.aemet.es/x", asyncio.Semaphore(8)
        )

    assert body == b"ok body"
    assert call_count == 2


# --- test 6: 404 is not retried ---------------------------------------------


async def test_get_does_not_retry_a_404():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, content=b"not found")

    async with _client(handler) as client:
        body = await rss_parser._get(
            client, "https://www.aemet.es/x", asyncio.Semaphore(8)
        )

    assert body is None
    assert call_count == 1


# --- test 7: fetch_alerts_for_regions isolates a failing region ------------


async def test_fetch_alerts_for_regions_isolates_a_failing_region(monkeypatch):
    monkeypatch.setattr(rss_parser.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(rss_parser, "HTTP_MAX_RETRIES", 2)

    ok_feed_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_MAD_RSS.xml"
    mad_index = _index_html([ok_feed_path])
    mad_feed = _feed_xml("MADMAD44.xml", "Aviso. Nivel amarillo. Viento. Madrid")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _index_url("and"):
            return httpx.Response(500, content=b"server error")
        if str(request.url) == _index_url("mad"):
            return httpx.Response(200, content=mad_index)
        return httpx.Response(200, content=mad_feed)

    async with _client(handler) as client:
        results = await fetch_alerts_for_regions(["mad", "and"], client)

    assert set(results.keys()) == {"mad", "and"}
    assert results["and"] is None
    assert [a.canonical_id for a in results["mad"]] == ["MADMAD44.xml"]


# --- test 8: index page is decoded as ISO-8859-15 ---------------------------


async def test_discover_feed_urls_decodes_index_page_as_iso_8859_15(read_fixture):
    index_bytes = read_fixture("index_mad.html")

    # Pin the premise: the fixture's real bytes are not valid UTF-8, so this
    # test only passes if _discover_feed_urls decodes as ISO-8859-15.
    try:
        index_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("fixture must not be valid UTF-8 for this test to matter")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=index_bytes)

    async with _client(handler) as client:
        urls = await _discover_feed_urls("mad", client, asyncio.Semaphore(8))

    assert (
        "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/rss/"
        "CAP_AFAP7228_RSS.xml" in urls
    )


# --- test 9: concurrency is bounded -----------------------------------------


async def test_http_concurrency_never_exceeds_configured_maximum(monkeypatch):
    monkeypatch.setattr(rss_parser, "HTTP_MAX_CONCURRENCY", 2)

    in_flight = 0
    max_in_flight = 0

    feed_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_RSS.xml"
    index_html = _index_html([feed_path])
    feed_bytes = _feed_xml("NOOP0000.xml", "Aviso. Nivel amarillo. Viento. Zona")

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if request.url.path == feed_path:
            return httpx.Response(200, content=feed_bytes)
        return httpx.Response(200, content=index_html)

    async with _client(handler) as client:
        await fetch_alerts_for_regions(["r1", "r2", "r3"], client)

    assert max_in_flight <= 2
    # Pin the intent: the cap must bound concurrency, not collapse it to a
    # serial fetch (which would also satisfy the <= assertion above).
    assert max_in_flight == 2


# --- test 9b: the semaphore is process-wide, not per call -------------------


async def test_concurrent_fetch_calls_share_one_process_wide_semaphore(monkeypatch):
    """A per-call semaphore would let N callers multiply the ceiling by N.

    /avisos calls fetch_alerts_for_regions once per user command, so the cap
    has to hold across simultaneous calls -- otherwise a user tapping the
    command repeatedly can get the bot's IP throttled by AEMET and take the
    polling job's fetches down with it.
    """
    monkeypatch.setattr(rss_parser, "HTTP_MAX_CONCURRENCY", 2)

    in_flight = 0
    max_in_flight = 0

    feed_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_RSS.xml"
    index_html = _index_html([feed_path])
    feed_bytes = _feed_xml("NOOP0000.xml", "Aviso. Nivel amarillo. Viento. Zona")

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if request.url.path == feed_path:
            return httpx.Response(200, content=feed_bytes)
        return httpx.Response(200, content=index_html)

    async with _client(handler) as client:
        # Two /avisos-shaped calls plus a single-region fetch, all at once:
        # fetch_alerts must share the same semaphore as fetch_alerts_for_regions.
        await asyncio.gather(
            fetch_alerts_for_regions(["r1", "r2", "r3"], client),
            fetch_alerts_for_regions(["r4", "r5", "r6"], client),
            fetch_alerts("r7", client),
        )

    assert max_in_flight == 2


async def test_both_entry_points_use_the_same_semaphore_object():
    assert rss_parser._get_semaphore() is rss_parser._get_semaphore()


def test_semaphore_is_rebuilt_when_the_event_loop_changes():
    """A semaphore bound to a closed loop must never leak into the next one."""

    async def capture() -> asyncio.Semaphore:
        return rss_parser._get_semaphore()

    first = asyncio.run(capture())
    second = asyncio.run(capture())

    assert first is not second


# --- test 10: _parse_feed_bytes never raises --------------------------------


def test_parse_feed_bytes_on_no_alerts_feed_returns_empty_list(read_fixture):
    alerts = _parse_feed_bytes(
        read_fixture("feed_madrid_sin_avisos.xml"), "https://example/feed"
    )

    assert alerts == []


def test_parse_feed_bytes_on_garbage_bytes_returns_empty_list_without_raising():
    alerts = _parse_feed_bytes(b"not xml at all", "https://example/feed")

    assert alerts == []


# --- review fixes --------------------------------------------------------


async def test_fetch_alerts_drops_expired_alerts(read_fixture, monkeypatch):
    feed_path = "/documentos_d/eltiempo/prediccion/avisos/rss/FEED_CO_RSS.xml"
    index_html = _index_html([feed_path])
    feed = read_fixture("feed_cordoba_amarillo.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _index_url("and"):
            return httpx.Response(200, content=index_html)
        return httpx.Response(200, content=feed)

    async with _client(handler) as client:
        assert len(await fetch_alerts("and", client)) == 1
        monkeypatch.setattr(
            rss_parser, "_now", lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        assert await fetch_alerts("and", client) == []


async def test_fetch_alerts_returns_none_when_every_feed_fails(monkeypatch):
    monkeypatch.setattr(rss_parser.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(rss_parser, "HTTP_MAX_RETRIES", 0)
    index_html = _index_html(
        ["/documentos_d/eltiempo/prediccion/avisos/rss/FEED_A_RSS.xml"]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _index_url("xx"):
            return httpx.Response(200, content=index_html)
        return httpx.Response(500, content=b"server error")

    async with _client(handler) as client:
        assert await fetch_alerts("xx", client) is None


async def test_shared_client_is_reused_until_closed():
    store: dict[str, object] = {}
    first = rss_parser.shared_client(store)
    assert rss_parser.shared_client(store) is first

    await rss_parser.close_shared_client(store)
    assert first.is_closed
    second = rss_parser.shared_client(store)
    assert second is not first
    await rss_parser.close_shared_client(store)
