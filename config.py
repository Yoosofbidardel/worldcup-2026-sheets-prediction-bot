import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "AFC Asian Cup Qatar 2022")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# ── Football API (kickoff times → prediction deadlines) ──────────────────
# Reuses the same provider/key as the original bot (football-data.org).
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
FOOTBALL_API_BASE = os.getenv("FOOTBALL_API_BASE", "https://api.football-data.org/v4")
COMPETITION_CODE = os.getenv("COMPETITION_CODE", "WC")
# How often the background job refreshes kickoff times from the API (hours).
KICKOFF_REFRESH_HOURS = float(os.getenv("KICKOFF_REFRESH_HOURS", "6"))
# Timezone used only for displaying kickoff times to users (lock logic uses UTC).
DISPLAY_TZ = os.getenv("DISPLAY_TZ", "UTC")

# If true, predictions can be added/changed at ANY time. Default is FALSE so a
# match LOCKS at kickoff — you can edit your pick freely until the match starts,
# but never predict a past/started game.
PREDICTIONS_ALWAYS_OPEN = os.getenv("PREDICTIONS_ALWAYS_OPEN", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# How often the background job pulls finished-match results from the API and
# writes them into the sheet (hours). Needs FOOTBALL_API_KEY.
RESULTS_REFRESH_HOURS = float(os.getenv("RESULTS_REFRESH_HOURS", "1"))

# ── Sheet layout (1-based row/column numbers) ────────────────────────────
# Row 1 holds participant names, row 2 holds their =SUM() totals.
NAME_ROW = 1
TOTAL_ROW = 2

# Participant prediction blocks start at column E (5) and repeat every 3 columns:
#   [predicted_home, predicted_away, points_formula]
FIRST_SLOT_COL = 5
SLOT_STRIDE = 3

# Real match fixtures live in this row range; A=home B=away C=actual_home D=actual_away.
# MATCH_LAST_ROW is env-configurable: a 72-match group stage needs rows 3..74.
MATCH_FIRST_ROW = 3
MATCH_LAST_ROW = int(os.getenv("MATCH_LAST_ROW", "69"))

# Column that stores each match's kickoff time (ISO-8601 UTC). It MUST sit past
# every prediction block (and the ROBOT block), otherwise it collides with a
# player's column. Default 74 (col BV) suits ~20 players; override per-deployment
# via env for bigger leagues (e.g. 33 players -> set KICKOFF_COL=114).
KICKOFF_COL = int(os.getenv("KICKOFF_COL", "74"))
KICKOFF_HEADER = "Kickoff (UTC)"

# Names that are not real participants and must never be assigned to a person.
EXCLUDED_NAMES = {"ROBOT", "Kickoff (UTC)"}

# Special predictions: row -> (label, points). The prediction text goes in the
# participant's FIRST block column; the actual answer is entered by the admin in C.
# SPECIAL_BASE_ROW is env-configurable (these rows shift down if group rows grow).
SPECIAL_BASE_ROW = int(os.getenv("SPECIAL_BASE_ROW", "70"))
SPECIAL_ROWS = {
    SPECIAL_BASE_ROW: ("قهرمان جام جهانی", 7),
    SPECIAL_BASE_ROW + 1: ("بهترین بازیکن تورنمنت", 6),
    SPECIAL_BASE_ROW + 2: ("آقای گل", 6),
}

# Local file mapping telegram users -> sheet slots.
ASSIGNMENTS_FILE = os.path.join(os.path.dirname(__file__), "assignments.json")

# ── Reminders ────────────────────────────────────────────────────────────
# Nudge each linked user who hasn't predicted a match yet, this many hours
# before its kickoff. One reminder per (user, match, window). Needs the match's
# kickoff time to be known (from the API or hand-filled in column BV).
REMINDER_WINDOWS_HOURS = [24, 3, 1]
# How often the background job checks for due reminders (minutes).
REMINDER_CHECK_MINUTES = float(os.getenv("REMINDER_CHECK_MINUTES", "30"))
# Tracks which reminders were already sent, so none is sent twice.
REMINDERS_FILE = os.path.join(os.path.dirname(__file__), "reminders_sent.json")

# Tracks which users have already seen the first-time guide (so it shows once).
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen_intro.json")

# Append-only audit log of every prediction a user saves (kept forever, even
# when they change a pick), so the admin can review who predicted what.
PREDLOG_FILE = os.path.join(os.path.dirname(__file__), "predictions_log.jsonl")

# ── Group announcements (daily analysis call + post-match leaderboard) ────
# Stores the main-group chat id, the daily totals snapshot, and which finished
# matches were already announced.
ANNOUNCE_FILE = os.path.join(os.path.dirname(__file__), "announce.json")
# Daily "yesterday's top scorers" call fires at this hour, Tehran time.
ANALYSIS_HOUR_TEHRAN = int(os.getenv("ANALYSIS_HOUR_TEHRAN", "9"))
# How often to check for newly-finished matches to post the leaderboard (minutes).
ANNOUNCE_CHECK_MINUTES = float(os.getenv("ANNOUNCE_CHECK_MINUTES", "15"))
