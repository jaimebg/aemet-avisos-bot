from __future__ import annotations

import logging
import time

from telegram import Bot, BotCommand
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from config import (
    CLEANUP_INTERVAL_SECONDS,
    LEVEL_RANK,
    POLL_INTERVAL_SECONDS,
    SEEN_RETENTION_DAYS,
    TELEGRAM_TOKEN,
    ConfigError,
)
from database import (
    cleanup_old_alerts,
    get_seen_levels,
    get_subscribed_regions,
    get_subscribers_with_min_rank,
    init_db,
    mark_alert_seen,
    remove_all_subscriptions,
    update_alert_level,
)
from handlers import (
    callback_handler,
    current_alerts_command,
    help_command,
    level_command,
    my_subscriptions_command,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from rss_parser import Alert, build_client, fetch_alerts_for_regions

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _stored_rank(level: str | None) -> int:
    """Rank of a previously stored level; unknown/legacy rows rank 0.

    Deliberately the opposite bias to Alert.level_rank, which ranks an
    unparseable *incoming* level as maximally urgent. Ranking a stored
    unknown at 0 lets any known level escalate over a legacy NULL row.
    """
    return LEVEL_RANK.get(level or "", 0)


async def _send_alert(bot: Bot, user_id: int, message: str) -> bool:
    """Send one alert to one user. Returns True when it was delivered.

    Never raises: every Telegram failure mode is caught and reported here so
    one bad recipient can never abort delivery to the rest.
    """
    try:
        await bot.send_message(chat_id=user_id, text=message, parse_mode="HTML")
    except Forbidden:
        logger.info(
            "User %d blocked the bot or the chat is gone; removing subscriptions",
            user_id,
        )
        remove_all_subscriptions(user_id)
        return False
    except BadRequest as exc:
        # Include the message text: this is how a formatting bug gets diagnosed.
        logger.error(
            "Telegram rejected the alert for user %d: %s. Message was: %r",
            user_id,
            exc,
            message,
        )
        return False
    except RetryAfter as exc:
        # The rate limiter normally absorbs these; if one still surfaces the
        # alert is simply retried on the next cycle (see the D3 rule).
        logger.warning("Flood control for user %d: %s", user_id, exc)
        return False
    except TelegramError as exc:
        logger.warning("Could not send alert to user %d: %s", user_id, exc)
        return False
    return True


async def _deliver_region_alerts(
    bot: Bot, region_code: str, alerts: list[Alert]
) -> None:
    """Deliver the new and escalated alerts of one region and record them."""
    seen = get_seen_levels([a.canonical_id for a in alerts])

    # (alert, previous_level, is_new) for every alert worth delivering.
    pending: list[tuple[Alert, str | None, bool]] = []
    for alert in alerts:
        if alert.canonical_id not in seen:
            pending.append((alert, None, True))
            continue
        stored_level = seen[alert.canonical_id]
        # An unparseable level ranks 3 so the severity filter never discards
        # it, but that rank is a guess, not evidence of an escalation. Without
        # the `is not None` guard such an alert would out-rank its own stored
        # NULL row on every single cycle (and would downgrade a known stored
        # level to NULL), re-notifying every subscriber until AEMET drops it.
        if alert.level is not None and alert.level_rank > _stored_rank(stored_level):
            # AEMET republished the alert at a higher level: notify again.
            pending.append((alert, stored_level, False))

    if not pending:
        return

    recipients = get_subscribers_with_min_rank(region_code)

    for alert, previous_level, is_new in pending:
        # Scoped to one alert on purpose. A failure after a successful send
        # (mark_alert_seen hitting a full disk or a lock that outlives
        # busy_timeout, say) must cost exactly that alert: a guard around the
        # whole loop would leave the region's remaining alerts neither sent
        # nor recorded. poll_alerts keeps a region-level guard as well, so a
        # failure in the per-region setup above still cannot abort the cycle.
        try:
            eligible = [
                user_id
                for user_id, min_rank in recipients
                if min_rank <= alert.level_rank
            ]

            delivered = 0
            if eligible:
                message = alert.format_message(
                    region_code, previous_level=previous_level
                )
                for user_id in eligible:
                    if await _send_alert(bot, user_id, message):
                        delivered += 1

                if delivered == 0:
                    logger.warning(
                        "Alert %s (%s) reached none of its %d recipient(s); "
                        "not recording it so the next cycle retries",
                        alert.canonical_id,
                        region_code,
                        len(eligible),
                    )
                    continue

            if is_new:
                mark_alert_seen(alert.canonical_id, alert.level)
            else:
                update_alert_level(alert.canonical_id, alert.level)
        except Exception:
            logger.exception(
                "Error delivering alert %s for region %s",
                alert.canonical_id,
                region_code,
            )


def _maybe_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prune old seen alerts at most once per CLEANUP_INTERVAL_SECONDS."""
    now = time.monotonic()
    last_run = context.bot_data.get("last_cleanup_ts")
    if last_run is not None and now - last_run < CLEANUP_INTERVAL_SECONDS:
        return

    context.bot_data["last_cleanup_ts"] = now
    removed = cleanup_old_alerts(SEEN_RETENTION_DAYS)
    if removed:
        logger.info(
            "Removed %d seen alert(s) older than %d day(s)",
            removed,
            SEEN_RETENTION_DAYS,
        )


async def poll_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: fetch AEMET feeds and deliver new or escalated alerts.

    Two cycles never overlap: PTB's JobQueue runs on an APScheduler
    AsyncIOScheduler and does not override APScheduler's default
    max_instances=1, so a cycle that overruns POLL_INTERVAL_SECONDS is skipped
    rather than started alongside the running one. That is what keeps the
    read-then-write in _deliver_region_alerts (get_seen_levels ... await ...
    mark_alert_seen) from ever classifying the same alert as new twice.
    """
    # Pruning runs on its own schedule and must keep running even after the
    # last user unsubscribes, so it happens before the early return below.
    _maybe_cleanup(context)

    regions = get_subscribed_regions()
    if not regions:
        return

    async with build_client() as client:
        alerts_by_region = await fetch_alerts_for_regions(regions, client)

    for region_code, alerts in alerts_by_region.items():
        if not alerts:
            continue
        try:
            await _deliver_region_alerts(context.bot, region_code, alerts)
        except Exception:
            # Narrower failures are already handled per alert inside
            # _deliver_region_alerts; this catches the per-region setup (the
            # seen-levels and subscriber lookups) so one misbehaving region
            # still cannot abort the whole cycle.
            logger.exception("Error processing alerts for region %s", region_code)


async def post_init(app: Application) -> None:
    """Publish the command list so Telegram clients can autocomplete it."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Empezar"),
            BotCommand("suscribir", "Suscribirse a una comunidad"),
            BotCommand("desuscribir", "Eliminar una suscripción"),
            BotCommand("mis_avisos", "Ver tus suscripciones"),
            BotCommand("avisos", "Ver avisos activos ahora"),
            BotCommand("nivel", "Elegir nivel mínimo de aviso"),
            BotCommand("ayuda", "Ayuda"),
        ]
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception raised inside a handler instead of losing it."""
    logger.error(
        "Unhandled exception while processing update %r",
        update,
        exc_info=context.error,
    )


def main() -> None:
    try:
        config.validate()
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(1) from exc

    init_db()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ayuda", help_command))
    app.add_handler(CommandHandler("suscribir", subscribe_command))
    app.add_handler(CommandHandler("desuscribir", unsubscribe_command))
    app.add_handler(CommandHandler("mis_avisos", my_subscriptions_command))
    app.add_handler(CommandHandler("avisos", current_alerts_command))
    app.add_handler(CommandHandler("nivel", level_command))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_error_handler(error_handler)

    # Periodic RSS polling
    app.job_queue.run_repeating(poll_alerts, interval=POLL_INTERVAL_SECONDS, first=10)

    logger.info("Bot started. Polling AEMET every %d seconds.", POLL_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
