from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from config import LEVELS, REGIONS, rank_for
from database import (
    add_subscription,
    get_min_level,
    get_user_subscriptions,
    remove_all_subscriptions,
    remove_subscription,
    set_min_level,
)
from rss_parser import Alert, build_client, fetch_alerts_for_regions

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 ¡Bienvenido al bot de Avisos AEMET!\n\n"
    "Recibe alertas meteorológicas de la AEMET directamente en Telegram.\n\n"
    "Comandos disponibles:\n"
    "/start — Empezar\n"
    "/suscribir — Elegir comunidad autónoma\n"
    "/desuscribir — Eliminar suscripción\n"
    "/mis_avisos — Ver suscripciones activas\n"
    "/avisos — Ver avisos activos ahora\n"
    "/nivel — Elegir nivel mínimo de aviso\n"
    "/ayuda — Ayuda"
)

HELP_TEXT = (
    "ℹ️ <b>Ayuda</b>\n\n"  # noqa: RUF001 (user-facing Spanish text, not code)
    "Este bot consulta periódicamente los avisos meteorológicos de la AEMET "
    "y te notifica si hay alertas en las comunidades autónomas a las que "
    "estés suscrito.\n\n"
    "<b>Comandos:</b>\n"
    "/start — Empezar\n"
    "/suscribir — Suscribirse a una comunidad\n"
    "/desuscribir — Eliminar una suscripción\n"
    "/mis_avisos — Ver tus suscripciones\n"
    "/avisos — Ver avisos activos ahora\n"
    "/nivel — Elegir nivel mínimo de aviso\n"
    "/ayuda — Este mensaje\n\n"
    "El nivel mínimo que elijas con /nivel se aplica tanto a las notificaciones "
    "automáticas como a los resultados de /avisos.\n\n"
    "Los niveles de aviso son:\n"
    "🟡 Amarillo — Riesgo bajo\n"
    "🟠 Naranja — Riesgo importante\n"
    "🔴 Rojo — Riesgo extremo"
)

# Callback data prefixes
CB_SUBSCRIBE = "sub:"
CB_UNSUBSCRIBE = "unsub:"
CB_UNSUB_ALL = "unsub_all"
CB_LEVEL = "lvl:"

COLUMNS = 2

MAX_AVISOS_ALERTS = 10
AEMET_AVISOS_URL = "https://www.aemet.es/es/eltiempo/prediccion/avisos"

# Key under which /avisos keeps the ids of the users whose lookup is running.
AVISOS_IN_FLIGHT_KEY = "avisos_in_flight"
AVISOS_BUSY_TEXT = "⏳ Ya estoy consultando tus avisos. Espera unos segundos."

LEVEL_LABELS = {
    "amarillo": "🟡 Amarillo y superiores",
    "naranja": "🟠 Naranja y rojo",
    "rojo": "🔴 Solo rojo",
}


def _message(update: Update) -> Message | None:
    return update.effective_message


def _region_name(region_code: str) -> str:
    """Display name for a region code, HTML-escaped for parse_mode="HTML".

    No name in REGIONS contains a character that needs escaping today, and the
    fallback code comes from callback data a client could forge. Escape it
    rather than reason about it.
    """
    return html.escape(REGIONS.get(region_code, region_code), quote=True)


def _build_region_keyboard(
    prefix: str, exclude: set[str] | None = None
) -> InlineKeyboardMarkup:
    exclude = exclude or set()
    buttons = [
        InlineKeyboardButton(name, callback_data=f"{prefix}{code}")
        for code, name in sorted(REGIONS.items(), key=lambda x: x[1])
        if code not in exclude
    ]
    rows = [buttons[i : i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]
    return InlineKeyboardMarkup(rows)


def _build_level_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for level in LEVELS:
        label = LEVEL_LABELS[level]
        if level == current:
            label = f"✅ {label}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_LEVEL}{level}")])
    return InlineKeyboardMarkup(rows)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _message(update)
    if msg is None:
        return
    await msg.reply_text(WELCOME_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _message(update)
    if msg is None:
        return
    await msg.reply_text(HELP_TEXT, parse_mode="HTML")


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _message(update)
    if msg is None:
        return
    user = update.effective_user
    if user is None:
        return

    user_subs = set(get_user_subscriptions(user.id))
    keyboard = _build_region_keyboard(CB_SUBSCRIBE, exclude=user_subs)
    if not keyboard.inline_keyboard:
        await msg.reply_text("Ya estás suscrito a todas las comunidades.")
        return
    await msg.reply_text(
        "Elige una comunidad autónoma para suscribirte:", reply_markup=keyboard
    )


async def unsubscribe_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = _message(update)
    if msg is None:
        return
    user = update.effective_user
    if user is None:
        return

    subs = get_user_subscriptions(user.id)
    if not subs:
        await msg.reply_text("No tienes suscripciones activas.")
        return

    buttons = [
        InlineKeyboardButton(
            REGIONS.get(code, code), callback_data=f"{CB_UNSUBSCRIBE}{code}"
        )
        for code in subs
    ]
    rows = [buttons[i : i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]
    rows.append([InlineKeyboardButton("❌ Eliminar todas", callback_data=CB_UNSUB_ALL)])
    await msg.reply_text(
        "Elige la suscripción a eliminar:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def my_subscriptions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = _message(update)
    if msg is None:
        return
    user = update.effective_user
    if user is None:
        return

    subs = get_user_subscriptions(user.id)
    if not subs:
        await msg.reply_text("No tienes suscripciones. Usa /suscribir para añadir.")
        return
    names = [f"• {_region_name(c)}" for c in subs]
    await msg.reply_text(
        "📋 <b>Tus suscripciones:</b>\n" + "\n".join(names), parse_mode="HTML"
    )


async def level_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _message(update)
    if msg is None:
        return
    user = update.effective_user
    if user is None:
        return

    current = get_min_level(user.id)
    await msg.reply_text(
        "Elige tu nivel mínimo de aviso:",
        reply_markup=_build_level_keyboard(current),
    )


async def current_alerts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = _message(update)
    if msg is None:
        return
    user = update.effective_user
    if user is None:
        return

    user_id = user.id
    regions = get_user_subscriptions(user_id)
    if not regions:
        await msg.reply_text("No tienes suscripciones. Usa /suscribir para añadir.")
        return

    # One /avisos at a time per user. The HTTP semaphore is process-wide, so
    # without this guard a single user tapping the command repeatedly could
    # queue up unbounded fetches against AEMET and starve the polling job.
    in_flight: set[int] = context.bot_data.setdefault(AVISOS_IN_FLIGHT_KEY, set())
    if user_id in in_flight:
        await msg.reply_text(AVISOS_BUSY_TEXT)
        return

    in_flight.add(user_id)
    try:
        placeholder = await msg.reply_text("⏳ Consultando avisos activos…")

        async with build_client() as client:
            alerts_by_region = await fetch_alerts_for_regions(regions, client)

        min_level = get_min_level(user_id)
        min_rank = rank_for(min_level)
        matched: list[tuple[str, Alert]] = [
            (region_code, alert)
            for region_code, alerts in alerts_by_region.items()
            for alert in alerts
            if min_rank <= alert.level_rank
        ]

        name = min_level.capitalize()
        if not matched:
            text = (
                f"✅ No hay avisos activos en tus comunidades "
                f"(nivel mínimo: <b>{name}</b>)."
            )
            await placeholder.edit_text(text, parse_mode="HTML")
            return

        total = len(matched)
        await placeholder.edit_text(f"📢 {total} aviso(s) activo(s):")

        shown = matched[:MAX_AVISOS_ALERTS]
        for region_code, alert in shown:
            await msg.reply_text(alert.format_message(region_code), parse_mode="HTML")

        remaining = total - len(shown)
        if remaining > 0:
            await msg.reply_text(
                f"… y {remaining} avisos más. Consulta {AEMET_AVISOS_URL}"
            )
    finally:
        # Always release, including on an exception: a stranded id would lock
        # the user out of /avisos for the lifetime of the process.
        in_flight.discard(user_id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        # CallbackQuery.data is optional and this handler has no pattern, so
        # every callback query reaches it -- including ones carrying no data.
        return
    user_id = query.from_user.id

    if data.startswith(CB_SUBSCRIBE):
        region_code = data[len(CB_SUBSCRIBE) :]
        region_name = _region_name(region_code)
        added = add_subscription(user_id, region_code)
        if added:
            text = f"✅ Suscrito a <b>{region_name}</b>."
        else:
            text = f"Ya estabas suscrito a <b>{region_name}</b>."
        await query.edit_message_text(text, parse_mode="HTML")

    elif data.startswith(CB_UNSUBSCRIBE):
        region_code = data[len(CB_UNSUBSCRIBE) :]
        region_name = _region_name(region_code)
        removed = remove_subscription(user_id, region_code)
        if removed:
            text = f"🗑 Suscripción a <b>{region_name}</b> eliminada."
        else:
            text = f"No estabas suscrito a <b>{region_name}</b>."
        await query.edit_message_text(text, parse_mode="HTML")

    elif data == CB_UNSUB_ALL:
        count = remove_all_subscriptions(user_id)
        await query.edit_message_text(f"🗑 Se han eliminado {count} suscripción(es).")

    elif data.startswith(CB_LEVEL):
        level = data[len(CB_LEVEL) :]
        if level not in LEVELS:
            # Stale button from an old keyboard revision: ignore safely.
            return
        set_min_level(user_id, level)
        name = level.capitalize()
        if level == "rojo":
            text = f"✅ Nivel mínimo: <b>{name}</b>. Solo recibirás avisos rojos."
        else:
            text = (
                f"✅ Nivel mínimo: <b>{name}</b>. Recibirás avisos de ese nivel o "
                "superiores."
            )
        await query.edit_message_text(text, parse_mode="HTML")
