"""Tests for the polling loop: delivery accounting, escalation and pruning.

Covers D3 (never mark an undelivered alert as seen), D4 (prune users who
blocked the bot), D6 (deliver escalations), G1 (per-user minimum severity)
and G6 (prune seen alerts on an interval, not every cycle).
"""

from __future__ import annotations

import time

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

import bot
import database
from rss_parser import Alert

CANONICAL_ID = "AFAZ611402ATTA3119.xml"
OTHER_CANONICAL_ID = "AFAZ659201VIRM2121.xml"


def make_alert(
    level: str | None,
    *,
    canonical_id: str = CANONICAL_ID,
    published_at: str = "20260224101529",
) -> Alert:
    """Build an Alert with a realistic AEMET guid.

    The 14-digit timestamp changes on every republication while the suffix
    (the canonical id) stays put, which is exactly the D6 scenario.
    """
    if level is None:
        title = "Aviso. Fenómenos costeros. Litoral de Cádiz"
    else:
        title = f"Aviso. Nivel {level}. Lluvias. Litoral de Cádiz"
    guid = (
        "https://www.aemet.es/documentos_d/eltiempo/prediccion/avisos/rss/"
        f"Z_CAP_C_LEMM_{published_at}_{canonical_id}"
    )
    return Alert(
        title=title,
        description="Precipitación acumulada en una hora.",
        link="https://www.aemet.es/es/eltiempo/prediccion/avisos",
        guid=guid,
        pub_date="Tue, 24 Feb 2026 10:15:29 +0100",
        level=level,
    )


class FakeBot:
    """Stand-in for telegram.Bot: records sends, raises for chosen chat ids."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.errors: dict[int, Exception] = {}

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        error = self.errors.get(chat_id)
        if error is not None:
            raise error
        self.sent.append((chat_id, text))

    def recipients(self) -> list[int]:
        return [chat_id for chat_id, _ in self.sent]

    def messages_for(self, chat_id: int) -> list[str]:
        return [text for target, text in self.sent if target == chat_id]


class FakeContext:
    """The slice of ContextTypes.DEFAULT_TYPE that poll_alerts actually uses."""

    def __init__(self, fake_bot: FakeBot) -> None:
        self.bot = fake_bot
        self.bot_data: dict[str, object] = {}


class FakeClient:
    """Async context manager standing in for the httpx client."""

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeFetcher:
    """Replacement for rss_parser.fetch_alerts_for_regions."""

    def __init__(self) -> None:
        self.alerts_by_region: dict[str, list[Alert]] = {}
        self.calls = 0

    async def __call__(
        self, region_codes: list[str], client: object
    ) -> dict[str, list[Alert]]:
        self.calls += 1
        return {
            code: list(self.alerts_by_region.get(code, [])) for code in region_codes
        }


@pytest.fixture
def fetcher(monkeypatch):
    """Replace the network layer entirely: no HTTP happens in these tests."""
    fake = FakeFetcher()
    monkeypatch.setattr(bot, "build_client", FakeClient)
    monkeypatch.setattr(bot, "fetch_alerts_for_regions", fake)
    return fake


@pytest.fixture
def fake_bot():
    return FakeBot()


@pytest.fixture
def context(fake_bot):
    return FakeContext(fake_bot)


async def test_new_alert_is_sent_to_every_subscriber_and_recorded(
    temp_db, fetcher, context, fake_bot
):
    database.add_subscription(1, "and")
    database.add_subscription(2, "and")
    fetcher.alerts_by_region = {"and": [make_alert("amarillo")]}

    await bot.poll_alerts(context)

    assert sorted(fake_bot.recipients()) == [1, 2]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "amarillo"}


async def test_already_seen_alert_is_not_sent_again(
    temp_db, fetcher, context, fake_bot
):
    database.add_subscription(1, "and")
    fetcher.alerts_by_region = {"and": [make_alert("amarillo")]}

    await bot.poll_alerts(context)
    fake_bot.sent.clear()
    await bot.poll_alerts(context)

    assert fake_bot.sent == []


async def test_alert_republished_at_a_higher_level_is_sent_again(
    temp_db, fetcher, context, fake_bot
):
    """D6: an upgrade from yellow to red must reach everyone again."""
    database.add_subscription(1, "and")
    database.add_subscription(2, "and")
    fetcher.alerts_by_region = {"and": [make_alert("amarillo")]}
    await bot.poll_alerts(context)
    fake_bot.sent.clear()

    fetcher.alerts_by_region = {
        "and": [make_alert("rojo", published_at="20260224180000")]
    }
    await bot.poll_alerts(context)

    assert sorted(fake_bot.recipients()) == [1, 2]
    assert "Aviso ACTUALIZADO: AMARILLO → ROJO" in fake_bot.sent[0][1]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "rojo"}


async def test_alert_republished_at_the_same_level_is_not_sent_again(
    temp_db, fetcher, context, fake_bot
):
    database.add_subscription(1, "and")
    fetcher.alerts_by_region = {"and": [make_alert("naranja")]}
    await bot.poll_alerts(context)
    fake_bot.sent.clear()

    fetcher.alerts_by_region = {
        "and": [make_alert("naranja", published_at="20260224180000")]
    }
    await bot.poll_alerts(context)

    assert fake_bot.sent == []
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "naranja"}


async def test_alert_republished_at_a_lower_level_is_not_sent_and_keeps_its_level(
    temp_db, fetcher, context, fake_bot
):
    database.add_subscription(1, "and")
    fetcher.alerts_by_region = {"and": [make_alert("rojo")]}
    await bot.poll_alerts(context)
    fake_bot.sent.clear()

    fetcher.alerts_by_region = {
        "and": [make_alert("amarillo", published_at="20260224180000")]
    }
    await bot.poll_alerts(context)

    assert fake_bot.sent == []
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "rojo"}


async def test_alert_with_a_legacy_null_stored_level_is_sent_again(
    temp_db, fetcher, context, fake_bot
):
    """A pre-migration row ranks 0, so any known level escalates over it."""
    database.add_subscription(1, "and")
    database.mark_alert_seen(CANONICAL_ID, None)
    fetcher.alerts_by_region = {"and": [make_alert("amarillo")]}

    await bot.poll_alerts(context)

    assert fake_bot.recipients() == [1]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "amarillo"}


async def test_alert_is_not_recorded_when_every_send_fails_and_is_retried(
    temp_db, fetcher, context, fake_bot
):
    """D3: a fully failed delivery must not consume the alert."""
    database.add_subscription(1, "and")
    database.add_subscription(2, "and")
    fake_bot.errors = {1: TelegramError("timeout"), 2: TelegramError("timeout")}
    fetcher.alerts_by_region = {"and": [make_alert("naranja")]}

    await bot.poll_alerts(context)

    assert fake_bot.sent == []
    assert database.get_seen_levels([CANONICAL_ID]) == {}

    fake_bot.errors = {}
    await bot.poll_alerts(context)

    assert sorted(fake_bot.recipients()) == [1, 2]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "naranja"}


async def test_alert_is_recorded_when_at_least_one_send_succeeds(
    temp_db, fetcher, context, fake_bot
):
    """D3 boundary: one delivery out of two is enough to consume the alert."""
    database.add_subscription(1, "and")
    database.add_subscription(2, "and")
    fake_bot.errors = {1: TelegramError("timeout")}
    fetcher.alerts_by_region = {"and": [make_alert("naranja")]}

    await bot.poll_alerts(context)

    assert fake_bot.recipients() == [2]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "naranja"}


async def test_blocked_user_is_unsubscribed_and_others_still_receive_the_alert(
    temp_db, fetcher, context, fake_bot
):
    """D4: Forbidden means the chat is gone; drop every subscription of that user."""
    database.add_subscription(1, "and")
    database.add_subscription(1, "mad")
    database.add_subscription(2, "and")
    fake_bot.errors = {1: Forbidden("Forbidden: bot was blocked by the user")}
    fetcher.alerts_by_region = {"and": [make_alert("rojo")]}

    await bot.poll_alerts(context)

    assert fake_bot.recipients() == [2]
    assert database.get_user_subscriptions(1) == []
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "rojo"}


async def test_minimum_level_preference_filters_recipients(
    temp_db, fetcher, context, fake_bot
):
    """G1: a 'rojo' subscriber gets only the red alert; the default gets both."""
    database.add_subscription(1, "and")
    database.add_subscription(2, "and")
    database.set_min_level(2, "rojo")
    fetcher.alerts_by_region = {
        "and": [
            make_alert("amarillo"),
            make_alert("rojo", canonical_id=OTHER_CANONICAL_ID),
        ]
    }

    await bot.poll_alerts(context)

    assert len(fake_bot.messages_for(1)) == 2
    assert len(fake_bot.messages_for(2)) == 1
    assert "Aviso ROJO" in fake_bot.messages_for(2)[0]


async def test_alert_with_an_unknown_level_reaches_even_a_rojo_subscriber(
    temp_db, fetcher, context, fake_bot
):
    """G1 boundary: an unparseable level ranks 3, so it is never filtered away."""
    database.add_subscription(1, "and")
    database.set_min_level(1, "rojo")
    fetcher.alerts_by_region = {"and": [make_alert(None)]}

    await bot.poll_alerts(context)

    assert fake_bot.recipients() == [1]
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: None}


async def test_alert_with_no_eligible_recipients_is_recorded_without_sending(
    temp_db, fetcher, context, fake_bot
):
    database.add_subscription(1, "and")
    database.set_min_level(1, "rojo")
    fetcher.alerts_by_region = {"and": [make_alert("amarillo")]}

    await bot.poll_alerts(context)

    assert fake_bot.sent == []
    assert database.get_seen_levels([CANONICAL_ID]) == {CANONICAL_ID: "amarillo"}


async def test_poll_alerts_without_subscriptions_never_fetches(
    temp_db, fetcher, context, fake_bot
):
    await bot.poll_alerts(context)

    assert fetcher.calls == 0
    assert fake_bot.sent == []


async def test_cleanup_runs_at_most_once_per_interval(
    temp_db, fetcher, context, monkeypatch
):
    """G6: pruning is time-based, not once per poll cycle."""
    database.add_subscription(1, "and")
    calls: list[int] = []

    def fake_cleanup(days: int) -> int:
        calls.append(days)
        return 0

    monkeypatch.setattr(bot, "cleanup_old_alerts", fake_cleanup)

    await bot.poll_alerts(context)
    await bot.poll_alerts(context)

    assert calls == [bot.SEEN_RETENTION_DAYS]

    context.bot_data["last_cleanup_ts"] = (
        time.monotonic() - 10 * bot.CLEANUP_INTERVAL_SECONDS
    )
    await bot.poll_alerts(context)

    assert calls == [bot.SEEN_RETENTION_DAYS, bot.SEEN_RETENTION_DAYS]


async def test_region_without_alerts_is_skipped_without_querying_the_database(
    temp_db, fetcher, context, fake_bot, monkeypatch
):
    database.add_subscription(1, "and")
    fetcher.alerts_by_region = {"and": []}
    seen_lookups: list[object] = []
    recipient_lookups: list[object] = []

    monkeypatch.setattr(
        bot, "get_seen_levels", lambda guids: seen_lookups.append(guids) or {}
    )
    monkeypatch.setattr(
        bot,
        "get_subscribers_with_min_rank",
        lambda region_code: recipient_lookups.append(region_code) or [],
    )

    await bot.poll_alerts(context)

    assert seen_lookups == []
    assert recipient_lookups == []
    assert fake_bot.sent == []


async def test_send_alert_return_value_for_success_and_each_caught_error(
    temp_db, fake_bot
):
    assert await bot._send_alert(fake_bot, 1, "hola") is True
    assert fake_bot.sent == [(1, "hola")]

    database.add_subscription(7, "and")
    fake_bot.errors = {
        7: Forbidden("Forbidden: bot was blocked by the user"),
        8: BadRequest("Can't parse entities"),
        9: RetryAfter(3),
        10: TelegramError("connection reset"),
    }

    assert await bot._send_alert(fake_bot, 7, "hola") is False
    assert database.get_user_subscriptions(7) == []
    assert await bot._send_alert(fake_bot, 8, "hola") is False
    assert await bot._send_alert(fake_bot, 9, "hola") is False
    assert await bot._send_alert(fake_bot, 10, "hola") is False
