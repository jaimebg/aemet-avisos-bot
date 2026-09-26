from __future__ import annotations

import logging
import time
from enum import Enum

from telegram import Bot, BotCommand
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from config import (
    ADMIN_CHAT_ID,
    CLEANUP_INTERVAL_SECONDS,
    FEED_FAILURE_ALERT_CYCLES,
    LEVEL_RANK,
    POLL_INTERVAL_SECONDS,
    SEEN_RETENTION_DAYS,
    TELEGRAM_TOKEN,
    ConfigError,
)
from database import (
    cleanup_old_alerts,
    clear_failed_deliveries,
    clear_failed_delivery,
    forget_user,
    get_failed_deliveries,
    get_seen_levels,
    get_subscribed_regions,
    get_subscribers_with_min_rank,
    init_db,
    mark_alert_seen,
    record_failed_delivery,
    update_alert_level,
)
from handlers import (
    callback_handler,
    current_alerts_command,
    help_command,
    level_command,
    my_subscriptions_command,
    private_only_command,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from rss_parser import (
    Alert,
    close_shared_client,
    fetch_alerts_for_regions,
    shared_client,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# bot_data key: consecutive poll cycles in which no subscribed region could be
# read at all.
FEED_FAILURE_STREAK_KEY = "feed_failure_streak"


class SendResult(Enum):
    DELIVERED = "delivered"
    # Permanent for this message: the user blocked the bot, or Telegram
    # refused the message itself. Retrying would fail the same way.
    REJECTED = "rejected"
    # Transient (flood control, network): worth retrying on a later cycle.
    FAILED = "failed"


def _stored_rank(level: str | None) -> int:
    """Rank of a previously stored level; unknown/legacy rows rank 0.

    Deliberately the opposite bias to Alert.level_rank, which ranks an
    unparseable *incoming* level as maximally urgent. Ranking a stored
    unknown at 0 lets any known level escalate over a legacy NULL row.
    """
    return LEVEL_RANK.get(level or "", 0)


async def _send_alert(bot: Bot, user_id: int, message: str) -> SendResult:
    """Send one alert to one user and classify the outcome.

    Never raises: every Telegram failure mode is caught and reported here so
    one bad recipient can never abort delivery to the rest.
    """
    try:
        await bot.send_message(chat_id=user_id, text=message, parse_mode="HTML")
    except Forbidden:
        logger.info(
            "User %d blocked the bot or the chat is gone; forgetting the user",
            user_id,
        )
        forget_user(user_id)
        return SendResult.REJECTED
    except BadRequest as exc:
        # Include the message text: this is how a formatting bug gets diagnosed.
        logger.error(
            "Telegram rejected the alert for user %d: %s. Message was: %r",
            user_id,
            exc,
            message,
        )
        return SendResult.REJECTED
    except RetryAfter as exc:
        # The rate limiter normally absorbs these; if one still surfaces the
        # alert is retried on a later cycle (the D3 rule, or the retry queue).
        logger.warning("Flood control for user %d: %s", user_id, exc)
        return SendResult.FAILED
    except TelegramError as exc:
        logger.warning("Could not send alert to user %d: %s", user_id, exc)
        return SendResult.FAILED
    return SendResult.DELIVERED


async def _deliver_region_alerts(
    bot: Bot, region_code: str, alerts: list[Alert]
) -> None:
    """Deliver the new and escalated alerts of one region and record them.

    Also retries queued deliveries of alerts already recorded: when an alert
    reached some subscribers but not others, the ones it missed transiently
    are queued in failed_deliveries and retried here while the alert lasts.
    """
    seen = get_seen_levels([a.canonical_id for a in alerts])

    # (alert, previous_level, is_new) for every alert worth delivering.
    pending: list[tuple[Alert, str | None, bool]] = []
    # Recorded alerts that are not being re-sent; candidates for retries.
    settled: list[Alert] = []
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
        else:
            settled.append(alert)

    retries = get_failed_deliveries([a.canonical_id for a in settled])
    if not pending and not retries:
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

            failed: list[int] = []
            delivered = 0
            if eligible:
                message = alert.format_message(
                    region_code, previous_level=previous_level
                )
                for user_id in eligible:
                    result = await _send_alert(bot, user_id, message)
                    if result is SendResult.DELIVERED:
                        delivered += 1
                    elif result is SendResult.FAILED:
                        failed.append(user_id)

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
                # Retries of the lower-level message are superseded: everyone
                # eligible for it was just sent the escalation instead.
                clear_failed_deliveries(alert.canonical_id)
            for user_id in failed:
                record_failed_delivery(alert.canonical_id, user_id, previous_level)
        except Exception:
            logger.exception(
                "Error delivering alert %s for region %s",
                alert.canonical_id,
                region_code,
            )

    if retries:
        await _retry_failed_deliveries(bot, region_code, settled, retries, recipients)


async def _retry_failed_deliveries(
    bot: Bot,
    region_code: str,
    alerts: list[Alert],
    retries: dict[str, list[tuple[int, str | None]]],
    recipients: list[tuple[int, int]],
) -> None:
    """Resend recorded alerts to the users a previous delivery missed."""
    min_rank_by_user = dict(recipients)
    for alert in alerts:
        queued = retries.get(alert.canonical_id)
        if not queued:
            continue
        try:
            for user_id, previous_level in queued:
                min_rank = min_rank_by_user.get(user_id)
                if min_rank is None or min_rank > alert.level_rank:
                    # Unsubscribed from the region, or raised their minimum
                    # level since: they no longer want this alert.
                    clear_failed_delivery(alert.canonical_id, user_id)
                    continue
                message = alert.format_message(
                    region_code, previous_level=previous_level
                )
                result = await _send_alert(bot, user_id, message)
                if result is not SendResult.FAILED:
                    clear_failed_delivery(alert.canonical_id, user_id)
        except Exception:
            logger.exception(
                "Error retrying alert %s for region %s",
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

    alerts_by_region = await fetch_alerts_for_regions(
        regions, shared_client(context.bot_data)
    )
    await _track_feed_health(context, alerts_by_region)

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


async def _notify_admin(bot: Bot, text: str) -> None:
    """Best-effort message to ADMIN_CHAT_ID; a no-op when it is not set."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except TelegramError as exc:
        logger.warning("Could not notify the admin chat: %s", exc)


async def _track_feed_health(
    context: ContextTypes.DEFAULT_TYPE, alerts_by_region: dict[str, list[Alert] | None]
) -> None:
    """Make a total AEMET outage loud instead of indistinguishable from calm.

    A cycle in which no subscribed region could be read (AEMET down, or its
    markup changed under _RSS_LINK_RE) would otherwise look exactly like a
    cycle with no alerts. After FEED_FAILURE_ALERT_CYCLES such cycles in a row
    this logs an error and tells the admin chat, once; the first cycle that
    reads anything again reports the recovery.
    """
    streak = int(context.bot_data.get(FEED_FAILURE_STREAK_KEY, 0))
    total_failure = bool(alerts_by_region) and all(
        alerts is None for alerts in alerts_by_region.values()
    )

    if not total_failure:
        if streak >= FEED_FAILURE_ALERT_CYCLES:
            logger.info("AEMET feeds readable again after %d failed cycle(s)", streak)
            await _notify_admin(
                context.bot,
                f"✅ AEMET vuelve a responder tras {streak} ciclo(s) fallidos.",
            )
        context.bot_data[FEED_FAILURE_STREAK_KEY] = 0
        return

    streak += 1
    context.bot_data[FEED_FAILURE_STREAK_KEY] = streak
    if streak == FEED_FAILURE_ALERT_CYCLES:
        logger.error(
            "No subscribed region could be read for %d consecutive cycle(s); "
            "no alerts are being delivered",
            streak,
        )
        await _notify_admin(
            context.bot,
            f"⚠️ No se ha podido leer ningún feed de AEMET en {streak} ciclos "
            "seguidos. No se están enviando avisos. Revisa los logs.",
        )
    else:
        logger.warning("No subscribed region could be read (cycle %d)", streak)


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


async def post_shutdown(app: Application) -> None:
    """Close the long-lived HTTP client opened by the polling job or /avisos."""
    await close_shared_client(app.bot_data)


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
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands. Private chats only: subscriptions are per user and alerts go
    # to the user's private chat, so a subscription made from a group would
    # target a chat the bot may not be allowed to open -- the first alert
    # would fail with Forbidden and silently erase it.
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start_command, filters=private))
    app.add_handler(CommandHandler("ayuda", help_command, filters=private))
    app.add_handler(CommandHandler("suscribir", subscribe_command, filters=private))
    app.add_handler(CommandHandler("desuscribir", unsubscribe_command, filters=private))
    app.add_handler(
        CommandHandler("mis_avisos", my_subscriptions_command, filters=private)
    )
    app.add_handler(CommandHandler("avisos", current_alerts_command, filters=private))
    app.add_handler(CommandHandler("nivel", level_command, filters=private))
    app.add_handler(MessageHandler(filters.COMMAND & ~private, private_only_command))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_error_handler(error_handler)

    # Periodic RSS polling
    app.job_queue.run_repeating(poll_alerts, interval=POLL_INTERVAL_SECONDS, first=10)

    logger.info("Bot started. Polling AEMET every %d seconds.", POLL_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
