"""Regression tests pinning the current behaviour of rss_parser.py.

These tests describe what the module does today, including two known bugs
that later tasks fix. They must keep passing unmodified until the task that
fixes the underlying behaviour also updates the corresponding test.
"""

from __future__ import annotations

import feedparser

from rss_parser import _RSS_LINK_RE, Alert, _discover_feed_urls, _parse_level

BASE_URL = "https://www.aemet.es"


def _alerts_from_feed(feed) -> list[Alert]:
    """Build Alerts from a parsed feed using the same field mapping as
    rss_parser._parse_feed, without going through _parse_feed itself (which
    takes a URL and would hit the network).
    """
    alerts = []
    for entry in feed.entries:
        title = entry.get("title", "")
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


# --- canonical_id -----------------------------------------------------


def test_canonical_id_strips_timestamp_from_bare_filename_guid():
    alert = Alert(
        title="t",
        description="",
        link="",
        guid="Z_CAP_C_LEMM_20260830090212_AFAZ611402ATTA3119.xml",
        pub_date="",
        level=None,
    )
    assert alert.canonical_id == "AFAZ611402ATTA3119.xml"


def test_canonical_id_strips_timestamp_from_full_url_guid():
    alert = Alert(
        title="t",
        description="",
        link="",
        guid=(
            "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/"
            "cap/Z_CAP_C_LEMM_20260319104642_AFAZ659201VIRM2121.xml"
        ),
        pub_date="",
        level=None,
    )
    assert alert.canonical_id == "AFAZ659201VIRM2121.xml"


def test_canonical_id_falls_back_to_raw_guid_when_pattern_does_not_match():
    alert = Alert(
        title="t",
        description="",
        link="",
        guid="something-else",
        pub_date="",
        level=None,
    )
    assert alert.canonical_id == "something-else"


def test_canonical_id_matches_across_republications_of_same_alert():
    republished_once = Alert(
        title="t",
        description="",
        link="",
        guid="Z_CAP_C_LEMM_20260830090212_AFAZ611402ATTA3119.xml",
        pub_date="",
        level=None,
    )
    republished_again = Alert(
        title="t",
        description="",
        link="",
        guid="Z_CAP_C_LEMM_20260319104642_AFAZ611402ATTA3119.xml",
        pub_date="",
        level=None,
    )
    assert republished_once.canonical_id == republished_again.canonical_id


# --- _parse_level -------------------------------------------------------


def test_parse_level_amarillo():
    title = "Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa"
    assert _parse_level(title) == "amarillo"


def test_parse_level_naranja():
    title = "Aviso. Nivel naranja. Viento. Costa de Huelva"
    assert _parse_level(title) == "naranja"


def test_parse_level_rojo():
    title = "Aviso. Nivel rojo. Lluvias. Pirineo Occidental"
    assert _parse_level(title) == "rojo"


def test_parse_level_returns_none_without_a_level_word():
    assert _parse_level("Estado completo de avisos para Córdoba") is None


def test_parse_level_prefers_most_severe_word_when_title_has_several():
    # _parse_level checks level words in a fixed "rojo", "naranja", "amarillo"
    # order and returns on the first substring match, regardless of where
    # each word actually sits in the title. That makes rojo win here even
    # though "amarillo" appears later in the sentence.
    title = "Aviso. Nivel rojo. Lluvia y nivel amarillo residual"
    assert _parse_level(title) == "rojo"


# --- feed parsing (fixture bytes only, no network) -----------------------


def test_parsing_cordoba_amarillo_feed_yields_one_alert(read_fixture):
    feed = feedparser.parse(read_fixture("feed_cordoba_amarillo.xml"))
    alerts = _alerts_from_feed(feed)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.title == (
        "Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa"
    )
    assert alert.level == "amarillo"
    assert alert.canonical_id == "AFAZ611402ATTA3119.xml"


def test_parsing_madrid_sin_avisos_feed_yields_no_alerts(read_fixture):
    feed = feedparser.parse(read_fixture("feed_madrid_sin_avisos.xml"))
    alerts = _alerts_from_feed(feed)

    assert alerts == []


# --- RSS index page scraping ---------------------------------------------


def test_rss_link_re_finds_raw_matches_including_duplicate_and_ignores_atom(
    read_fixture,
):
    html = read_fixture("index_mad.html").decode("iso-8859-15")
    matches = _RSS_LINK_RE.findall(html)

    assert len(matches) == 4
    assert all(path.endswith("_RSS.xml") for path in matches)
    assert not any("_ATOM.xml" in path for path in matches)
    assert (
        matches.count(
            "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAZ722802_RSS.xml"
        )
        == 2
    )


def test_discover_feed_urls_deduplicates_to_three_ordered_urls(
    monkeypatch, read_fixture
):
    html = read_fixture("index_mad.html").decode("iso-8859-15")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return html.encode("iso-8859-15")

    monkeypatch.setattr("rss_parser.urlopen", lambda url, timeout=30: _FakeResponse())

    urls = _discover_feed_urls("mad")

    assert urls == [
        BASE_URL + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAP7228_RSS.xml",
        BASE_URL
        + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAZ722801_RSS.xml",
        BASE_URL
        + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAZ722802_RSS.xml",
    ]
