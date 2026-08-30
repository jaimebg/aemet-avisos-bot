from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import REGIONS
from database import (
    add_subscription,
    get_user_subscriptions,
    remove_all_subscriptions,
    remove_subscription,
)

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 ¡Bienvenido al bot de Avisos AEMET!\n\n"
    "Recibe alertas meteorológicas de la AEMET directamente en Telegram.\n\n"
    "Comandos disponibles:\n"
    "/suscribir — Elegir comunidad autónoma\n"
    "/desuscribir — Eliminar suscripción\n"
    "/mis_avisos — Ver suscripciones activas\n"
    "/ayuda — Ayuda"
)

HELP_TEXT = (
    "ℹ️ <b>Ayuda</b>\n\n"  # noqa: RUF001 (user-facing Spanish text, not code)
    "Este bot consulta periódicamente los avisos meteorológicos de la AEMET "
    "y te notifica si hay alertas en las comunidades autónomas a las que "
    "estés suscrito.\n\n"
    "<b>Comandos:</b>\n"
    "/suscribir — Suscribirse a una comunidad\n"
    "/desuscribir — Eliminar una suscripción\n"
    "/mis_avisos — Ver tus suscripciones\n"
    "/ayuda — Este mensaje\n\n"
    "Los niveles de aviso son:\n"
    "🟡 Amarillo — Riesgo bajo\n"
    "🟠 Naranja — Riesgo importante\n"
    "🔴 Rojo — Riesgo extremo"
)

# Callback data prefixes
CB_SUBSCRIBE = "sub:"
CB_UNSUBSCRIBE = "unsub:"
CB_UNSUB_ALL = "unsub_all"

COLUMNS = 2


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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_subs = set(get_user_subscriptions(update.effective_user.id))
    keyboard = _build_region_keyboard(CB_SUBSCRIBE, exclude=user_subs)
    if not keyboard.inline_keyboard:
        await update.message.reply_text("Ya estás suscrito a todas las comunidades.")
        return
    await update.message.reply_text(
        "Elige una comunidad autónoma para suscribirte:", reply_markup=keyboard
    )


async def unsubscribe_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    subs = get_user_subscriptions(user_id)
    if not subs:
        await update.message.reply_text("No tienes suscripciones activas.")
        return

    buttons = [
        InlineKeyboardButton(
            REGIONS.get(code, code), callback_data=f"{CB_UNSUBSCRIBE}{code}"
        )
        for code in subs
    ]
    rows = [buttons[i : i + COLUMNS] for i in range(0, len(buttons), COLUMNS)]
    rows.append([InlineKeyboardButton("❌ Eliminar todas", callback_data=CB_UNSUB_ALL)])
    await update.message.reply_text(
        "Elige la suscripción a eliminar:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def my_subscriptions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    subs = get_user_subscriptions(update.effective_user.id)
    if not subs:
        await update.message.reply_text(
            "No tienes suscripciones. Usa /suscribir para añadir."
        )
        return
    names = [f"• {REGIONS.get(c, c)}" for c in subs]
    await update.message.reply_text(
        "📋 <b>Tus suscripciones:</b>\n" + "\n".join(names), parse_mode="HTML"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith(CB_SUBSCRIBE):
        region_code = data[len(CB_SUBSCRIBE) :]
        region_name = REGIONS.get(region_code, region_code)
        added = add_subscription(user_id, region_code)
        if added:
            text = f"✅ Suscrito a <b>{region_name}</b>."
        else:
            text = f"Ya estabas suscrito a <b>{region_name}</b>."
        await query.edit_message_text(text, parse_mode="HTML")

    elif data.startswith(CB_UNSUBSCRIBE):
        region_code = data[len(CB_UNSUBSCRIBE) :]
        region_name = REGIONS.get(region_code, region_code)
        removed = remove_subscription(user_id, region_code)
        if removed:
            text = f"🗑 Suscripción a <b>{region_name}</b> eliminada."
        else:
            text = f"No estabas suscrito a <b>{region_name}</b>."
        await query.edit_message_text(text, parse_mode="HTML")

    elif data == CB_UNSUB_ALL:
        count = remove_all_subscriptions(user_id)
        await query.edit_message_text(f"🗑 Se han eliminado {count} suscripción(es).")
