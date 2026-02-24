import logging

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

from config import POLL_INTERVAL_SECONDS, TELEGRAM_TOKEN
from database import (
    cleanup_old_alerts,
    get_subscribers_for_region,
    get_subscribed_regions,
    init_db,
    is_alert_seen,
    mark_alert_seen,
)
from handlers import (
    callback_handler,
    help_command,
    my_subscriptions_command,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from rss_parser import fetch_alerts

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def poll_alerts(context) -> None:
    """Periodic job: fetch RSS feeds and send new alerts to subscribers."""
    regions = get_subscribed_regions()
    if not regions:
        return

    for region_code in regions:
        alerts = fetch_alerts(region_code)
        new_alerts = [a for a in alerts if not is_alert_seen(a.canonical_id)]
        if not new_alerts:
            continue

        subscribers = get_subscribers_for_region(region_code)
        for alert in new_alerts:
            message = alert.format_message(region_code)
            for user_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=user_id, text=message, parse_mode="HTML"
                    )
                except Exception:
                    logger.exception(
                        "Failed to send alert to user %d", user_id
                    )
            mark_alert_seen(alert.canonical_id)

    # Cleanup old seen alerts once per cycle
    cleanup_old_alerts(days=7)


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ayuda", help_command))
    app.add_handler(CommandHandler("suscribir", subscribe_command))
    app.add_handler(CommandHandler("desuscribir", unsubscribe_command))
    app.add_handler(CommandHandler("mis_avisos", my_subscriptions_command))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Periodic RSS polling
    app.job_queue.run_repeating(
        poll_alerts, interval=POLL_INTERVAL_SECONDS, first=10
    )

    logger.info(
        "Bot started. Polling AEMET every %d seconds.", POLL_INTERVAL_SECONDS
    )
    app.run_polling()


if __name__ == "__main__":
    main()
