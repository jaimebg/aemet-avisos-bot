"""Tests for handlers.py: command handlers, callbacks, and D7 hardening.

Covers G1 (/nivel), G2 (/avisos) and D7 (a handler must not crash when
update.effective_message is None, e.g. for an edited message or channel post).
"""

from __future__ import annotations

import sqlite3

import pytest

import database
import handlers
from config import REGIONS
from rss_parser import Alert


def make_alert(
    level: str | None,
    *,
    canonical_id: str,
    published_at: str = "20260224101529",
) -> Alert:
    """Build a minimal Alert with a realistic AEMET guid shape."""
    if level is None:
        title = "Aviso. Fenómenos costeros. Litoral de Cádiz"
    else:
        title = f"Aviso. Nivel {level}. Lluvias. Litoral de Cádiz"
    guid = f"Z_CAP_C_LEMM_{published_at}_{canonical_id}"
    return Alert(
        title=title,
        description="Precipitación acumulada en una hora.",
        link="https://www.aemet.es/es/eltiempo/prediccion/avisos",
        guid=guid,
        pub_date="Tue, 24 Feb 2026 10:15:29 +0100",
        level=level,
    )


class FakeMessage:
    """Stand-in for telegram.Message: records replies and edits."""

    def __init__(self) -> None:
        self.replies: list[FakeMessage] = []
        self.reply_texts: list[str] = []
        self.reply_kwargs: list[dict] = []
        self.edited_texts: list[str] = []
        self.edited_kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs: object) -> FakeMessage:
        child = FakeMessage()
        self.replies.append(child)
        self.reply_texts.append(text)
        self.reply_kwargs.append(kwargs)
        return child

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edited_texts.append(text)
        self.edited_kwargs.append(kwargs)


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeCallbackQuery:
    """Stand-in for telegram.CallbackQuery: records answer() and edits."""

    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.answered = False
        self.edited_texts: list[str] = []
        self.edited_kwargs: list[dict] = []

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, **kwargs: object) -> None:
        self.edited_texts.append(text)
        self.edited_kwargs.append(kwargs)


class FakeUpdate:
    """Stand-in for telegram.Update exposing only what handlers.py touches."""

    def __init__(
        self,
        *,
        message: FakeMessage | None = None,
        user: FakeUser | None = None,
        callback_query: FakeCallbackQuery | None = None,
    ) -> None:
        self.effective_message = message
        self.effective_user = user
        self.callback_query = callback_query


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
    monkeypatch.setattr(handlers, "build_client", FakeClient)
    monkeypatch.setattr(handlers, "fetch_alerts_for_regions", fake)
    return fake


COMMAND_HANDLERS = [
    handlers.start_command,
    handlers.help_command,
    handlers.subscribe_command,
    handlers.unsubscribe_command,
    handlers.my_subscriptions_command,
    handlers.current_alerts_command,
    handlers.level_command,
]

ALL_COMMAND_NAMES = [
    "start",
    "suscribir",
    "desuscribir",
    "mis_avisos",
    "avisos",
    "nivel",
    "ayuda",
]


async def test_start_and_help_mention_all_seven_commands(temp_db):
    start_update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.start_command(start_update, None)
    welcome_text = start_update.effective_message.reply_texts[0]

    help_update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.help_command(help_update, None)
    help_text = help_update.effective_message.reply_texts[0]

    for name in ALL_COMMAND_NAMES:
        assert f"/{name}" in welcome_text
        assert f"/{name}" in help_text


@pytest.mark.parametrize("handler", COMMAND_HANDLERS)
async def test_handler_returns_silently_when_message_is_none(temp_db, handler):
    update = FakeUpdate(message=None, user=FakeUser(1))
    await handler(update, None)  # must not raise


async def test_subscribe_excludes_existing_regions_and_reports_when_all_subscribed(
    temp_db,
):
    user_id = 1
    database.add_subscription(user_id, "mad")

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(user_id))
    await handlers.subscribe_command(update, None)

    msg = update.effective_message
    keyboard = msg.reply_kwargs[0]["reply_markup"]
    codes = {
        button.callback_data[len(handlers.CB_SUBSCRIBE) :]
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert "mad" not in codes
    assert codes == set(REGIONS) - {"mad"}

    for code in REGIONS:
        database.add_subscription(user_id, code)

    update2 = FakeUpdate(message=FakeMessage(), user=FakeUser(user_id))
    await handlers.subscribe_command(update2, None)

    assert update2.effective_message.reply_texts == [
        "Ya estás suscrito a todas las comunidades."
    ]


async def test_unsubscribe_with_no_subscriptions_shows_no_keyboard(temp_db):
    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.unsubscribe_command(update, None)

    msg = update.effective_message
    assert msg.reply_texts == ["No tienes suscripciones activas."]
    assert msg.reply_kwargs == [{}]


async def test_my_subscriptions_lists_region_names_not_codes(temp_db):
    database.add_subscription(1, "mad")
    database.add_subscription(1, "and")

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.my_subscriptions_command(update, None)

    text = update.effective_message.reply_texts[0]
    assert text == "📋 <b>Tus suscripciones:</b>\n• Andalucía\n• Madrid"


async def test_sub_unsub_and_unsub_all_callbacks_still_work(temp_db):
    user_id = 1

    query = FakeCallbackQuery(f"{handlers.CB_SUBSCRIBE}mad", user_id)
    update = FakeUpdate(callback_query=query)
    await handlers.callback_handler(update, None)
    assert query.answered is True
    assert database.get_user_subscriptions(user_id) == ["mad"]
    assert "Suscrito a <b>Madrid</b>" in query.edited_texts[0]

    query_dup = FakeCallbackQuery(f"{handlers.CB_SUBSCRIBE}mad", user_id)
    await handlers.callback_handler(FakeUpdate(callback_query=query_dup), None)
    assert "Ya estabas suscrito" in query_dup.edited_texts[0]

    query_unsub = FakeCallbackQuery(f"{handlers.CB_UNSUBSCRIBE}mad", user_id)
    await handlers.callback_handler(FakeUpdate(callback_query=query_unsub), None)
    assert database.get_user_subscriptions(user_id) == []
    assert "eliminada" in query_unsub.edited_texts[0]

    database.add_subscription(user_id, "and")
    database.add_subscription(user_id, "cat")
    query_all = FakeCallbackQuery(handlers.CB_UNSUB_ALL, user_id)
    await handlers.callback_handler(FakeUpdate(callback_query=query_all), None)
    assert database.get_user_subscriptions(user_id) == []
    assert "2 suscripción" in query_all.edited_texts[0]


async def test_nivel_renders_three_buttons_and_marks_current_level(temp_db):
    database.set_min_level(1, "naranja")

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.level_command(update, None)

    keyboard = update.effective_message.reply_kwargs[0]["reply_markup"]
    rows = keyboard.inline_keyboard
    assert len(rows) == 3

    labels = [row[0].text for row in rows]
    assert labels == [
        "🟡 Amarillo y superiores",
        "✅ 🟠 Naranja y rojo",
        "🔴 Solo rojo",
    ]
    callbacks = [row[0].callback_data for row in rows]
    assert callbacks == ["lvl:amarillo", "lvl:naranja", "lvl:rojo"]


async def test_lvl_rojo_and_lvl_naranja_callback_phrasing(temp_db):
    query_rojo = FakeCallbackQuery("lvl:rojo", 1)
    await handlers.callback_handler(FakeUpdate(callback_query=query_rojo), None)
    assert database.get_min_level(1) == "rojo"
    assert query_rojo.edited_texts[0] == (
        "✅ Nivel mínimo: <b>Rojo</b>. Solo recibirás avisos rojos."
    )

    query_naranja = FakeCallbackQuery("lvl:naranja", 2)
    await handlers.callback_handler(FakeUpdate(callback_query=query_naranja), None)
    assert database.get_min_level(2) == "naranja"
    assert query_naranja.edited_texts[0] == (
        "✅ Nivel mínimo: <b>Naranja</b>. Recibirás avisos de ese nivel o superiores."
    )


async def test_unknown_callback_data_is_ignored_without_raising(temp_db):
    query = FakeCallbackQuery("garbage", 1)
    await handlers.callback_handler(FakeUpdate(callback_query=query), None)
    assert query.answered is True
    assert query.edited_texts == []

    # A stale button from an old keyboard revision with an unknown level.
    query_bad_level = FakeCallbackQuery("lvl:verde", 1)
    await handlers.callback_handler(FakeUpdate(callback_query=query_bad_level), None)
    assert query_bad_level.edited_texts == []


async def test_avisos_with_no_subscriptions_never_fetches(temp_db, fetcher):
    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.current_alerts_command(update, None)

    assert update.effective_message.reply_texts == [
        "No tienes suscripciones. Usa /suscribir para añadir."
    ]
    assert fetcher.calls == 0


async def test_avisos_sends_one_message_per_alert_and_does_not_touch_seen_alerts(
    temp_db, fetcher
):
    database.add_subscription(1, "and")
    fetcher.alerts_by_region = {
        "and": [
            make_alert("amarillo", canonical_id="A1"),
            make_alert("rojo", canonical_id="A2"),
        ]
    }

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.current_alerts_command(update, None)

    msg = update.effective_message
    placeholder = msg.replies[0]
    assert msg.reply_texts[0] == "⏳ Consultando avisos activos…"
    assert placeholder.edited_texts[0] == "📢 2 aviso(s) activo(s):"
    # placeholder + 2 alert messages
    assert len(msg.reply_texts) == 3
    assert "AMARILLO" in msg.reply_texts[1]
    assert "ROJO" in msg.reply_texts[2]

    conn = sqlite3.connect(temp_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM seen_alerts").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


async def test_avisos_filters_by_users_minimum_level(temp_db, fetcher):
    database.add_subscription(1, "and")
    database.set_min_level(1, "rojo")
    fetcher.alerts_by_region = {
        "and": [
            make_alert("amarillo", canonical_id="A1"),
            make_alert("rojo", canonical_id="A2"),
        ]
    }

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.current_alerts_command(update, None)

    msg = update.effective_message
    placeholder = msg.replies[0]
    assert placeholder.edited_texts[0] == "📢 1 aviso(s) activo(s):"
    assert len(msg.reply_texts) == 2
    assert "ROJO" in msg.reply_texts[1]


async def test_avisos_caps_at_ten_messages_with_a_tail_message(temp_db, fetcher):
    database.add_subscription(1, "and")
    alerts = [make_alert("amarillo", canonical_id=f"A{i}") for i in range(12)]
    fetcher.alerts_by_region = {"and": alerts}

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.current_alerts_command(update, None)

    msg = update.effective_message
    # placeholder + 10 alert messages + 1 tail message
    assert len(msg.reply_texts) == 12
    assert msg.reply_texts[-1] == (
        "… y 2 avisos más. Consulta https://www.aemet.es/es/eltiempo/prediccion/avisos"
    )


async def test_avisos_with_no_active_alerts_reports_current_minimum_level(
    temp_db, fetcher
):
    database.add_subscription(1, "and")
    database.set_min_level(1, "naranja")
    fetcher.alerts_by_region = {"and": []}

    update = FakeUpdate(message=FakeMessage(), user=FakeUser(1))
    await handlers.current_alerts_command(update, None)

    msg = update.effective_message
    placeholder = msg.replies[0]
    assert placeholder.edited_texts[0] == (
        "✅ No hay avisos activos en tus comunidades (nivel mínimo: <b>Naranja</b>)."
    )
