import logging

from telegram.ext import Application

import bot
from config import (
    FOOTBALL_API_KEY,
    GOOGLE_SHEET_ID,
    KICKOFF_REFRESH_HOURS,
    REMINDER_CHECK_MINUTES,
    RESULTS_REFRESH_HOURS,
    TELEGRAM_BOT_TOKEN,
)
from sheet import SheetClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s", level=logging.INFO
)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env).")
    if not GOOGLE_SHEET_ID:
        raise SystemExit("GOOGLE_SHEET_ID is not set (see .env).")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.bot_data["sheet"] = SheetClient()  # fails fast if creds/sheet are wrong
    bot.register(app)

    # Keep kickoff times fresh so predictions lock at kickoff.
    if FOOTBALL_API_KEY and app.job_queue:
        app.job_queue.run_repeating(
            bot.refresh_kickoffs_job,
            interval=KICKOFF_REFRESH_HOURS * 3600,
            first=5,  # also run shortly after startup
        )
        logging.info("Kickoff sync scheduled every %.1f h.", KICKOFF_REFRESH_HOURS)

        # Auto-fill match results into the sheet after games finish.
        app.job_queue.run_repeating(
            bot.refresh_results_job,
            interval=RESULTS_REFRESH_HOURS * 3600,
            first=20,
        )
        logging.info("Results sync scheduled every %.1f h.", RESULTS_REFRESH_HOURS)
    else:
        logging.warning("FOOTBALL_API_KEY not set — kickoff deadlines & result sync disabled.")

    # Nudge users who haven't predicted upcoming matches (needs kickoff times).
    if app.job_queue:
        app.job_queue.run_repeating(
            bot.reminder_job,
            interval=REMINDER_CHECK_MINUTES * 60,
            first=15,
        )
        logging.info("Reminder check scheduled every %.0f min.", REMINDER_CHECK_MINUTES)

    logging.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
