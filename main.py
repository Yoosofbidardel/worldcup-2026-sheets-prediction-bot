import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram.ext import Application

import bot
from config import (
    ANALYSIS_HOUR_TEHRAN,
    ANNOUNCE_CHECK_MINUTES,
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

        # Group posts: predictions when a match starts, leaderboard when it ends.
        # (Needs /setgroup run in the main group first.)
        app.job_queue.run_repeating(
            bot.group_announce_job,
            interval=ANNOUNCE_CHECK_MINUTES * 60,
            first=45,
        )
        # Precise: fire each match's prediction post exactly at its kickoff.
        app.job_queue.run_repeating(
            bot.reschedule_kickoffs_job,
            interval=1800,  # re-scan every 30 min for new/updated kickoffs + restarts
            first=12,
        )
        logging.info("Group announce: 15-min fallback + exact-kickoff timers.")

        # Each morning at 09:00 Tehran: post the next 24h fixtures to the group.
        app.job_queue.run_daily(
            bot.daily_fixtures_job,
            time=dtime(hour=ANALYSIS_HOUR_TEHRAN, minute=0, tzinfo=ZoneInfo("Asia/Tehran")),
        )
        logging.info("Daily fixtures post at %02d:00 Tehran.", ANALYSIS_HOUR_TEHRAN)

    logging.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
