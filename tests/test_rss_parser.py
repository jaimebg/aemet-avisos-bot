"""Regression tests pinning the current behaviour of rss_parser.py.

These tests describe what the module does today, including two known bugs
that later tasks fix. They must keep passing unmodified until the task that
fixes the underlying behaviour also updates the corresponding test.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from config import AEMET_BASE_URL
from rss_parser import (
    _RSS_LINK_RE,
    Alert,
    _discover_feed_urls,
    _parse_feed_bytes,
    _parse_level,
    _parse_validity,
    _parse_zone,
)

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
    alerts = _parse_feed_bytes(read_fixture("feed_cordoba_amarillo.xml"), "source")

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.title == (
        "Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa"
    )
    assert alert.level == "amarillo"
    assert alert.canonical_id == "AFAZ611402ATTA3119.xml"


def test_parsing_madrid_sin_avisos_feed_yields_no_alerts(read_fixture):
    alerts = _parse_feed_bytes(read_fixture("feed_madrid_sin_avisos.xml"), "source")

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


async def test_discover_feed_urls_deduplicates_to_three_ordered_urls(read_fixture):
    index_html = read_fixture("index_mad.html")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=index_html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        urls = await _discover_feed_urls("mad", client, asyncio.Semaphore(8))

    assert urls == [
        AEMET_BASE_URL
        + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAP7228_RSS.xml",
        AEMET_BASE_URL
        + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAZ722801_RSS.xml",
        AEMET_BASE_URL
        + "/documentos_d/eltiempo/prediccion/avisos/rss/CAP_AFAZ722802_RSS.xml",
    ]


# --- level_rank -----------------------------------------------------------


def _alert_with_level(level: str | None) -> Alert:
    return Alert(title="t", description="", link="", guid="g", pub_date="", level=level)


def test_level_rank_follows_severity_order_and_unknown_is_maximal():
    assert _alert_with_level("amarillo").level_rank == 1
    assert _alert_with_level("naranja").level_rank == 2
    assert _alert_with_level("rojo").level_rank == 3
    assert _alert_with_level(None).level_rank == 3


# --- _parse_zone ------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected_zone"),
    [
        (
            "Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa",
            "Campiña cordobesa",
        ),
        ("Aviso. Nivel naranja. Viento. Costa de Huelva", "Costa de Huelva"),
        ("Aviso. Nivel rojo. Lluvias. Pirineo Occidental", "Pirineo Occidental"),
        ("Aviso. Nivel rojo. Lluvia", None),
    ],
)
def test_parse_zone_on_real_title_shapes(title, expected_zone):
    assert _parse_zone(title) == expected_zone


# --- _parse_validity ---------------------------------------------------------


def test_parse_validity_parses_real_cordoba_description():
    description = (
        "Aviso de temperatura máxima de nivel amarillo de 13:00 31-08-2026 CEST "
        "(UTC+2) a 20:59 31-08-2026 CEST (UTC+2)."
    )

    starts_at, ends_at = _parse_validity(description)

    assert starts_at == datetime(
        2026, 8, 31, 13, 0, tzinfo=timezone(timedelta(hours=2))
    )
    assert ends_at == datetime(2026, 8, 31, 20, 59, tzinfo=timezone(timedelta(hours=2)))
    assert starts_at.utcoffset() == timedelta(hours=2)
    assert ends_at.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    "description",
    [
        "",
        "Aviso sin ninguna referencia a la validez temporal.",
        "de 99:99 31-13-2026 CEST (UTC+2) a 20:59 31-08-2026 CEST (UTC+2)",
    ],
)
def test_parse_validity_returns_none_pair_for_missing_or_malformed_input(description):
    assert _parse_validity(description) == (None, None)


def test_parse_validity_handles_negative_utc_offset():
    description = (
        "Aviso de viento de 10:00 01-01-2026 WET (UTC-1) a 12:00 01-01-2026 WET "
        "(UTC-1)."
    )

    starts_at, ends_at = _parse_validity(description)

    assert starts_at.utcoffset() == timedelta(hours=-1)
    assert ends_at.utcoffset() == timedelta(hours=-1)


# --- format_message: HTML escaping (D1) --------------------------------------


def test_format_message_escapes_ampersand_and_angle_brackets():
    alert = Alert(
        title="Aviso. Nivel rojo. Viento & lluvia <fuerte>",
        description="Riesgo de daños & cortes en <zonas> bajas.",
        link="https://www.aemet.es/x",
        guid="g",
        pub_date="",
        level="rojo",
    )

    message = alert.format_message("and")

    assert "&amp;" in message
    assert "&lt;fuerte&gt;" in message
    assert "&lt;zonas&gt;" in message
    assert "<fuerte>" not in message
    assert "<zonas>" not in message
    assert message.count("<a href=") == 1


def test_format_message_escapes_link_quotes_and_ampersand_in_href():
    alert = Alert(
        title="Aviso. Nivel amarillo. Viento",
        description="",
        link='https://www.aemet.es/aviso?x=1&y="raro"',
        guid="g",
        pub_date="",
        level="amarillo",
    )

    message = alert.format_message("and")

    assert '<a href="https://www.aemet.es/aviso?x=1&amp;y=&quot;raro&quot;">' in message
    assert message.count("<a href=") == 1
    assert '&y="raro"' not in message


# --- format_message: exact contract ------------------------------------------


def test_format_message_matches_exact_contract_for_cordoba_fixture(read_fixture):
    alert = _parse_feed_bytes(read_fixture("feed_cordoba_amarillo.xml"), "source")[0]

    message = alert.format_message("and")

    expected = (
        "🟡 Aviso AMARILLO\n"
        "📍 Andalucía · Campiña cordobesa\n"
        "📝 Aviso. Nivel amarillo. Temperaturas máximas. Campiña cordobesa\n"
        "🕒 31/08 13:00 → 31/08 20:59\n"
        "\n"
        "Aviso de temperatura máxima de nivel amarillo de 13:00 31-08-2026 CEST "
        "(UTC+2) a 20:59 31-08-2026 CEST (UTC+2).\n"
        "\n"
        '🔗 <a href="https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/'
        'cap/Z_CAP_C_LEMM_20260830090212_AFAZ611402ATTA3119.xml">Más información</a>'
    )
    assert message == expected


# --- format_message: escalation header ---------------------------------------


def _red_alert() -> Alert:
    return Alert(
        title="Aviso. Nivel rojo. Viento. Costa de Huelva",
        description="",
        link="https://www.aemet.es/x",
        guid="g",
        pub_date="",
        level="rojo",
    )


def test_format_message_shows_escalation_header_when_previous_level_differs():
    message = _red_alert().format_message("and", previous_level="naranja")

    assert message.startswith("🔺 Aviso ACTUALIZADO: NARANJA → ROJO\n")
    assert "🔴 Aviso ROJO" not in message


def test_format_message_shows_normal_header_when_previous_level_matches():
    message = _red_alert().format_message("and", previous_level="rojo")

    assert message.startswith("🔴 Aviso ROJO\n")
    assert "ACTUALIZADO" not in message


# --- format_message: optional blocks -----------------------------------------


def test_format_message_omits_description_block_when_empty():
    alert = Alert(
        title="Aviso. Nivel amarillo. Viento",
        description="",
        link="https://www.aemet.es/x",
        guid="g",
        pub_date="",
        level="amarillo",
    )

    message = alert.format_message("and")
    lines = message.split("\n")

    assert lines[-1] == '🔗 <a href="https://www.aemet.es/x">Más información</a>'
    assert lines[-2] == ""
    # Exactly one blank line in the whole message: the one right before the
    # link line. No leftover blank line from a skipped description block.
    assert message.count("\n\n") == 1
