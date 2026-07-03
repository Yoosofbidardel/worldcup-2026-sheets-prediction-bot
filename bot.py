"""Telegram handlers for the World Cup 2026 prediction bot (Farsi, RTL UI).

Flow:
  • Admin assigns each Telegram user to a sheet slot (/slots, /assign).
  • Assigned users send predictions via friendly buttons; the bot writes the
    two prediction cells.
  • Scoring & totals are computed live by the Google Sheet's own formulas.
  • A background job reminds users who haven't predicted a match yet, 24h / 3h /
    1h before kickoff.

Text is right-to-left Farsi. Latin/number runs (team names, scores, dates) are
wrapped in Unicode isolates so they never shuffle the surrounding Farsi line,
and dates are shown in both the Jalali (Shamsi) and Gregorian calendars.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jdatetime
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import api_client
import store
from config import (
    ADMIN_IDS,
    BESTPLAYER_ROW,
    BONUS_SPECIAL_ROWS,
    CHAMPION_FINAL_DEADLINE_DT,
    CHAMPION_ROW,
    DISPLAY_TZ,
    PREDLOG_FILE,
    REMINDER_WINDOWS_HOURS,
    SPECIAL_BONUS,
    SPECIAL_DEADLINE_DT,
    TOP_GAINERS_COUNT,
    TOPSCORER_ROW,
)
from teams_fa import fa as _team_fa

MAX_SCORE = 20  # cap for the +/- stepper

# ── Friendly menu buttons (tap instead of typing commands) ────────────────
BTN_PREDICT = "🎯 پیش‌بینی بازی"
BTN_SPECIAL = "🏆 پیش‌بینی ویژه"
BTN_MINE = "📋 پیش‌بینی‌های من"
BTN_MATCHES = "📅 برنامه بازی‌ها"
BTN_LEADERBOARD = "📊 جدول امتیازات"  # admin only
BTN_BROADCAST = "📢 پیام به همه"  # admin only
BTN_WHOAMI = "🆔 من کی‌ام؟"
BTN_HELP = "❓ راهنما"
# admin-only
BTN_SEND_TABLE = "📤 ارسال جدول"
BTN_SEND_PREDS = "📤 ارسال پیش‌بینی‌ها"
BTN_TOGGLE_AUTO = "⚙️ ارسال خودکار نتایج"
BTN_PREDLOG = "📜 لاگ پیش‌بینی‌ها"
BTN_SEND_GROUP = "📨 پیام تو گروه"  # admin only — post in the group as the bot
BTN_TOPGAINERS = "🏅 برترین‌های دیروز"  # admin only — post yesterday's top gainers

# ── Bidi helpers (keep the layout stable when Latin text appears) ─────────
RLM = "‏"  # right-to-left mark — forces RTL base direction on a line
_LRI, _PDI = "⁨", "⁩"  # first-strong isolate / pop directional isolate
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

try:
    _TZ = ZoneInfo(DISPLAY_TZ)
except Exception:
    _TZ = ZoneInfo("UTC")

_FA_MONTHS = jdatetime.date.j_months_fa


def _fa_num(s) -> str:
    return str(s).translate(_FA_DIGITS)


def _iso(s) -> str:
    """Isolate a run so it doesn't reorder the surrounding RTL text."""
    return f"{_LRI}{s}{_PDI}"


def _team(name: str) -> str:
    """Farsi team name, isolated so a Latin fallback can't break the line."""
    return _iso(_team_fa(name))


def _score(home, away) -> str:
    """A 'home - away' score that reads correctly in an RTL line.

    Must be SPACED and NOT isolated: a contiguous '0-2' gets bidi-reversed to
    '2-0' next to Farsi team names, but spaced digits flow right-to-left so each
    number lines up with its own team.
    """
    return _fa_num(f"{home} - {away}")


def _pred_line(name, home_fa, away_fa, h, a) -> str:
    """One tidy prediction row: the name and the whole 'team score team' result
    are each isolated, so a Latin name never shuffles the line's layout."""
    unit = _iso(f"{home_fa} {_fa_num(f'{h} - {a}')} {away_fa}")
    return f"• {_iso(name)}: {unit}"


def _rtl(text: str) -> str:
    """Force every line to RTL base direction."""
    return "\n".join(RLM + ln for ln in text.split("\n"))


def _fmt_kickoff(dt) -> str:
    """Kickoff shown in Shamsi (Jalali) only, Tehran time, isolated as one unit."""
    if not dt:
        return ""
    local = dt.astimezone(_TZ)
    jd = jdatetime.datetime.fromgregorian(datetime=local)
    shamsi = _fa_num(f"{jd.day} {_FA_MONTHS[jd.month - 1]} {jd.year}")
    clock = _fa_num(local.strftime("%H:%M"))
    return _iso(f"{shamsi} ⏰ {clock}")


# ── Special-prediction deadline helpers ───────────────────────────────────
def _deadline_passed() -> bool:
    """True once the special-prediction deadline (start of group round 3) has passed."""
    return SPECIAL_DEADLINE_DT is not None and datetime.now(timezone.utc) >= SPECIAL_DEADLINE_DT


def _deadline_str() -> str:
    """The deadline formatted for display (Shamsi / Tehran)."""
    return _fmt_kickoff(SPECIAL_DEADLINE_DT) if SPECIAL_DEADLINE_DT else ""


def _champion_locked() -> bool:
    """True once the champion's FINAL deadline (start of knockouts) has passed."""
    return CHAMPION_FINAL_DEADLINE_DT is not None and datetime.now(timezone.utc) >= CHAMPION_FINAL_DEADLINE_DT


def _final_deadline_str() -> str:
    return _fmt_kickoff(CHAMPION_FINAL_DEADLINE_DT) if CHAMPION_FINAL_DEADLINE_DT else ""


def _special_is_open(s: dict) -> bool:
    """Whether a special prediction can currently be set/changed. Two-cell bonus
    rows (champion always; Katan also best-player & top-scorer) stay editable
    until the knockout stage starts — a change after the first deadline just
    forfeits the bonus. Any other special hard-closes at the first deadline."""
    if s["row"] in BONUS_SPECIAL_ROWS:
        return s["open"] and not _champion_locked()
    return s["open"] and not _deadline_passed()


_MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


def _fmt_total(t) -> str:
    """Points like 47.5 / 47 (two decimals, trailing zeros trimmed), Persian digits."""
    return _fa_num(f"{float(t):.2f}".rstrip("0").rstrip("."))


def _leaderboard_lines(standings, header: str) -> str:
    lines = [header]
    for i, e in enumerate(standings):
        rank = _MEDALS.get(i, f"{_fa_num(i + 1)}.")
        lines.append(f"{rank} {e['name']} — *{_iso(_fmt_total(e['total']))}*")
    return "\n".join(lines)


def _col_uid_map() -> dict:
    """slot column -> linked telegram user id (for tagging in the group)."""
    return {info["col"]: uid for uid, info in store.all_assignments().items()}


def _mention(name: str, uid) -> str:
    """A clickable Telegram mention by id (works without a username)."""
    return f"[{name}](tg://user?id={uid})" if uid else name


def _thread_kw() -> dict:
    """Post into the saved forum topic (if /setgroup was run inside one)."""
    tid = store.get_thread_id()
    return {"message_thread_id": tid} if tid else {}


def _sheet(context):
    return context.application.bot_data["sheet"]


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _main_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Persistent bottom menu so users tap instead of typing commands."""
    rows = [
        [KeyboardButton(BTN_PREDICT), KeyboardButton(BTN_SPECIAL)],
        [KeyboardButton(BTN_MINE), KeyboardButton(BTN_MATCHES)],
        [KeyboardButton(BTN_WHOAMI), KeyboardButton(BTN_HELP)],
    ]
    if is_admin:  # leaderboard (others' scores) + broadcast are admin-only
        rows.append([KeyboardButton(BTN_LEADERBOARD), KeyboardButton(BTN_BROADCAST)])
        rows.append([KeyboardButton(BTN_SEND_TABLE), KeyboardButton(BTN_SEND_PREDS)])
        rows.append([KeyboardButton(BTN_TOGGLE_AUTO), KeyboardButton(BTN_PREDLOG)])
        rows.append([KeyboardButton(BTN_SEND_GROUP), KeyboardButton(BTN_TOPGAINERS)])
    # is_persistent keeps the menu always shown (esp. on Telegram Desktop, where a
    # non-persistent reply keyboard collapses and its toggle is easy to miss).
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


# ── The first-time guide ──────────────────────────────────────────────────
GUIDE = (
    "📖 *راهنمای مسابقه‌ی پیش‌بینی دوره نهم کتان ممپف* ⚽️🎉\n\n"
    "سلام رفیق جون! 😎 ایجا قراره نتیجه‌ی بازیا رو حدس بزنی و با بقیه کل‌کل کنی. "
    "بیو ببینیم چی‌کار باید بکنی 👇\n\n"
    "🎯 *پیش‌بینی بازی*\n"
    "دکمه‌ی «🎯 پیش‌بینی بازی» رو بزن، یه بازی وردار، با ➖ و ➕ نتیجه رو بچین "
    "و «✅ ذخیره» رو بزن. تموم شد رفت!\n\n"
    "🔄 *عوض کردنش*\n"
    "تا قبل شروع بازی هرچقد دلت خواست عوضش کن. ولی همین که سوت شروع خورد، قفل می‌شه و دیگه نمی‌شه‌ها! ⏰🔒\n\n"
    "🏁 *نتیجه‌ها*\n"
    "بعد هر بازی نتیجه‌ش خودکار ثبت می‌شه و امتیازتم حساب می‌شه، غصه نخور.\n\n"
    "🏆 *پیش‌بینی ویژه*\n"
    "قهرمان، آقای گل و بهترین بازیکن تورنمنت رو هم حدس بزن (امتیازش چرب‌تره‌ها! 🤑).\n\n"
    "📋 *پیش‌بینی‌های من*\n"
    "ببین تا حالا چی زدی و نتیجه‌ی واقعی بازیا چی شده.\n\n"
    "📅 *برنامه بازی‌ها*\n"
    "لیست بازیا، زمان شروع (به تاریخ شمسی و میلادی) و نتیجه‌ها.\n\n"
    "⏰ *یادآوری*\n"
    "اگه یه بازی رو پیش‌بینی نکرده باشی، ۲۴ و ۳ و ۱ ساعت قبلش بهت تذکر می‌دم که یادت نره‌ها! 😉\n\n"
    "🏅 *امتیازا چه‌جوری حساب می‌شه؟*\n"
    "همه‌چی خودکار حساب می‌شه، پس تقلب بی‌تقلب! 😄\n\n"
    "🆔 *هنوز وصل نیستی؟*\n"
    "دکمه‌ی «🆔 من کی‌ام؟» رو بزن و آیدیتو وردار بفرست برا ادمین تا وصلت کنه.\n\n"
    "موفق باشی، ایشالا که ببری (ولی نه بیشتر از من 😏)!"
)

ADMIN_GUIDE = (
    "\n\n— — — — —\n"
    "👑 *مخصوص ادمین*\n"
    "• «📢 پیام به همه» — یه پیام بده به همه‌ی بچه‌ها (تو پیویِ هرکی).\n"
    "• «📨 پیام تو گروه» — یه پیام از طرفِ ربات بذار تو گروه.\n"
    "• `/topgainers` — نفراتِ برترِ امتیازگیریِ دیروز رو همین الان تو گروه اعلام کن.\n"
    "• «📊 جدول امتیازات» — جدولو ببین (فقط خودت می‌بینی).\n"
    "• «📤 ارسال جدول» — جدولو همین‌جا (گروه یا پیوی) بفرست.\n"
    "• «📤 ارسال پیش‌بینی‌ها» — پیش‌بینیِ یه بازی رو همین‌جا بفرست.\n"
    "• «⚙️ ارسال خودکار نتایج» — ارسالِ خودکارِ جدولِ بعد از بازی رو روشن/خاموش کن.\n"
    "• «📜 لاگ پیش‌بینی‌ها» — کلِ تاریخچه‌ی پیش‌بینی‌های همه رو بگیر.\n"
    "• `/slots` و `/assign <idx> <id>` — وصل کردن آدما به اسما.\n"
    "• `/assignments` و `/unassign <id>` — مدیریت اتصالا.\n"
    "• `/synckickoffs` — گرفتن زمان شروع بازیا از API.\n"
    "• `/syncresults` — ثبت نتیجه‌ی بازیای تموم‌شده (خودکارم می‌شه)."
)


def _guide_for(is_admin: bool) -> str:
    return GUIDE + (ADMIN_GUIDE if is_admin else "")


async def _run(func, *args):
    """Run a blocking gspread call off the event loop."""
    return await asyncio.to_thread(func, *args)


# ── Small reply helpers that always go out RTL + Markdown ────────────────
def _split_for_telegram(text: str, limit: int = 4000) -> list:
    """Split text into <=limit-char chunks on line boundaries (Telegram caps a
    message at 4096 chars). Splitting on whole lines keeps each line's Markdown
    balanced, so no entity is broken across a chunk."""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + 1 + len(ln) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = ln if not cur else cur + "\n" + ln
    if cur:
        chunks.append(cur)
    return chunks or [""]


async def _say(update: Update, text: str, **kw):
    kw.setdefault("parse_mode", ParseMode.MARKDOWN)
    chunks = _split_for_telegram(_rtl(text))
    for i, ch in enumerate(chunks):
        k = dict(kw)
        if i < len(chunks) - 1:  # attach the keyboard only to the final chunk
            k.pop("reply_markup", None)
        await update.message.reply_text(ch, **k)


async def _edit(query, text: str, **kw):
    kw.setdefault("parse_mode", ParseMode.MARKDOWN)
    parts = _split_for_telegram(_rtl(text))
    try:
        await query.edit_message_text(parts[0], **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    # If it was too long for one message, send the rest as follow-up messages.
    for ch in parts[1:]:
        k = dict(kw)
        k.pop("reply_markup", None)
        await query.message.reply_text(ch, **k)


def _stepper_kb(row, home_name, away_name, home, away):
    """Inline score stepper. State (the two scores) is carried in callback_data
    so +/- taps never touch the network — only Save does."""
    def cb(act):
        return f"sp:{row}:{home}:{away}:{act}"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data=cb("hm")),
                InlineKeyboardButton(f"{_team(home_name)}:  {_fa_num(home)}", callback_data=cb("noop")),
                InlineKeyboardButton("➕", callback_data=cb("hp")),
            ],
            [
                InlineKeyboardButton("➖", callback_data=cb("am")),
                InlineKeyboardButton(f"{_team(away_name)}:  {_fa_num(away)}", callback_data=cb("noop")),
                InlineKeyboardButton("➕", callback_data=cb("ap")),
            ],
            [
                InlineKeyboardButton("✅ ذخیره", callback_data=cb("save")),
                InlineKeyboardButton("✖️ ولش کن", callback_data=cb("cancel")),
            ],
        ]
    )


def _remember_names(context, row, home_name, away_name):
    context.user_data.setdefault("names", {})[row] = (home_name, away_name)


async def _match_names(context, sheet, row):
    """Team names for a match row, from cache or (rarely) a fresh fetch."""
    cached = context.user_data.get("names", {}).get(row)
    if cached:
        return cached
    m = next((x for x in await _run(sheet.matches) if x["row"] == row), None)
    if m:
        _remember_names(context, row, m["home"], m["away"])
        return m["home"], m["away"]
    return "Home", "Away"


_NOT_LINKED = "😅 هنوز به هیچ اسمی وصل نیستی که! دکمه‌ی «🆔 من کی‌ام؟» رو بزن، آیدیتو وردار بفرست برا ادمین تا وصلت کنه."


# ── Basic commands ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    a = store.get_assignment(user.id)
    if a:
        msg = (
            f"⚽️🎉 بَه‌بَه سلام {a['name']} جون! چطوری؟ همه‌چی رو‌به‌راهه، بیو بریم بترکونیم! 🔥\n\n"
            "از این دکمه‌های پایین استفاده کن، خیلی راحته‌ها 👇😎\n\n"
            "🎯 پیش‌بینی بازی – بزن بریم!\n"
            "🏆 پیش‌بینی ویژه – قهرمان / آقای گل / ستاره‌ی تورنمنت\n"
            "📋 پیش‌بینی‌های من – ببین چی زدی (شایدم خجالت بکشی 😅)\n"
            "📅 برنامه بازی‌ها – بازیا و نتیجه‌ها\n\n"
            "✨ تا قبل شروع هر بازی می‌تونی پیش‌بینیتو عوض کنی! همین که سوت خورد قفل می‌شه ⏰🔒"
        )
    else:
        msg = (
            "👋🌍 خوش اومدی به مسابقه‌ی پیش‌بینیِ دوره نهم کتان ممپف! 🎉⚽️\n\n"
            "فعلاً به هیچ اسمی وصل نیستی، یه غریبه‌ی نازنین 😎\n\n"
            "👈 برای ثبت‌نام، دکمه‌ی «🆔 من کی‌ام؟» رو بزن و آیدیت رو برای *آریا (کمیته آنالیز)* بفرست تا وصلت کنه.\n\n"
            f"`id={user.id}`  name=`{user.full_name}`"
            f"{'  `@' + user.username + '`' if user.username else ''}"
        )
    is_admin = _is_admin(user.id)
    await _say(update, msg, reply_markup=_main_kb(is_admin))
    # First time someone opens the bot, hand them the full guide.
    if not store.has_seen_intro(user.id):
        store.mark_seen_intro(user.id)
        await _say(update, _guide_for(is_admin))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = _is_admin(update.effective_user.id)
    await _say(update, _guide_for(is_admin), reply_markup=_main_kb(is_admin))


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    a = store.get_assignment(user.id)
    lines = [
        f"🆔 آیدی تلگرامت: `{user.id}`",
        f"🙋 اسم: {user.full_name}"
        + (f" (`@{user.username}`)" if user.username else ""),
    ]
    lines.append(f"🔗 وصل به اسم: *{a['name']}*" if a else "🔗 وصل به اسم: _هنوز هیشکی واللا! 🤷_")
    if not a:
        lines.append("\n👉 همین عدد بالا رو برای *آریا (کمیته آنالیز)* بفرست، یا همین پیامو براش فوروارد کن تا ثبت‌نامت کنه. 🙏")
    if _is_admin(user.id):
        lines.append("👑 نقش: *ادمین* (آقا بالاسر! 😎)")
    await _say(update, "\n".join(lines), reply_markup=_main_kb(_is_admin(user.id)))


# ── Predicting matches ───────────────────────────────────────────────────
PAGE_SIZE = 4  # open matches shown per page in /predict


def _sorted_open(matches):
    """Open matches in chronological order (soonest kickoff first)."""
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(matches, key=lambda m: (m["kickoff"] or far, m["row"]))


def _predict_page(matches, page):
    """Build the (text, keyboard) for one page of the open-match list."""
    pages = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = matches[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
    rows = []
    for m in chunk:
        label = f"{_team(m['home'])} 🆚 {_team(m['away'])}"
        if m["kickoff"]:
            label += f"  ⏰{_fmt_kickoff(m['kickoff'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{m['row']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"ppage:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"ppage:{page + 1}"))
    if nav:
        rows.append(nav)
    text = (
        f"🎯 یه بازی وردار (صفحه‌ی {_fa_num(page + 1)} از {_fa_num(pages)}) — "
        "تا قبل سوت شروع هرچی خواستی عوضش کن ⏰:"
    )
    return text, InlineKeyboardMarkup(rows)


async def cmd_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await _say(update, _NOT_LINKED)
        return
    matches = _sorted_open(await _run(_sheet(context).open_matches))
    if not matches:
        await _say(update, "🎉 الان هیچ بازی‌ای برا پیش‌بینی نی جونم. یه چایی بریز بخور دیگه ☕️😌")
        return
    for m in matches:
        _remember_names(context, m["row"], m["home"], m["away"])
    text, kb = _predict_page(matches, 0)
    await _say(update, text, reply_markup=kb)


async def predict_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip to another page of the open-match list (edits the same message)."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    matches = _sorted_open(await _run(_sheet(context).open_matches))
    if not matches:
        await _edit(query, "🎉 الان هیچ بازی‌ای برا پیش‌بینی نی جونم.")
        return
    for m in matches:
        _remember_names(context, m["row"], m["home"], m["away"])
    text, kb = _predict_page(matches, page)
    await _edit(query, text, reply_markup=kb)


async def cmd_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await _say(update, _NOT_LINKED)
        return
    specials = await _run(_sheet(context).specials)
    opens = [s for s in specials if _special_is_open(s)]
    if not opens:
        await _say(update, "🤷 الان هیچ پیش‌بینی ویژه‌ای باز نی واللا (مهلتشون تموم شدِس).")
        return
    buttons = [
        [InlineKeyboardButton(f"{s['label']} ({_fa_num(s['points'])} امتیاز)", callback_data=f"s:{s['row']}")]
        for s in opens
    ]
    note = ""
    if SPECIAL_DEADLINE_DT:
        note = f"\n\n⏰ مهلتِ پیش‌بینیِ ویژه تا *شروعِ دور سومِ گروهی*: {_deadline_str()}"
    await _say(update, "🏆 یه پیش‌بینی ویژه وردار:" + note, reply_markup=InlineKeyboardMarkup(buttons))


async def open_stepper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Player tapped a match -> show the score stepper, pre-filled if they
    already predicted it."""
    query = update.callback_query
    await query.answer()
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await _edit(query, _NOT_LINKED)
        return
    row = int(query.data.split(":")[1])
    sheet = _sheet(context)
    match = next((m for m in await _run(sheet.matches) if m["row"] == row), None)
    if not match or not match["open"]:
        await _edit(query, "🔒 این بازی دیگه بسته‌ست جونم، دیر رسیدی!")
        return
    _remember_names(context, row, match["home"], match["away"])
    home, away = await _run(sheet.match_prediction, a["col"], row)
    home, away = home or 0, away or 0
    when = f"\n🕐 سوت شروع: {_fmt_kickoff(match['kickoff'])}" if match["kickoff"] else ""
    await _edit(
        query,
        f"⚽️ *{_team(match['home'])}* 🆚 *{_team(match['away'])}*\n"
        f"👇 نتیجه رو بچین و ذخیره کن (تا قبل سوت شروع هرچقد خواستی عوضش کن 🔄){when}",
        reply_markup=_stepper_kb(row, match["home"], match["away"], home, away),
    )


async def stepper_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle +/- / Save / Cancel taps on the stepper (state lives in callback_data)."""
    query = update.callback_query
    _, row, home, away, act = query.data.split(":")
    row, home, away = int(row), int(home), int(away)
    home_name, away_name = await _match_names(context, _sheet(context), row)

    if act in ("hm", "hp", "am", "ap"):
        if act == "hm":
            home = max(0, home - 1)
        elif act == "hp":
            home = min(MAX_SCORE, home + 1)
        elif act == "am":
            away = max(0, away - 1)
        else:
            away = min(MAX_SCORE, away + 1)
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=_stepper_kb(row, home_name, away_name, home, away)
        )
        return

    if act == "noop":
        await query.answer()
        return

    if act == "cancel":
        await query.answer("ولش کردیم 😎")
        await _edit(query, "✖️ ولش کردیم. شاید بعداً پشیمون شی‌ها 🤭")
        return

    # save
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await query.answer()
        await _edit(query, _NOT_LINKED)
        return
    sheet = _sheet(context)
    match = next((m for m in await _run(sheet.matches) if m["row"] == row), None)
    if not match or not match["open"]:
        await query.answer()
        await _edit(query, "⏰ ای داد! همین الان بسته شد، پیش‌بینیت ثبت نشد 😬")
        return
    await _run(sheet.set_match_prediction, a["col"], row, home, away)
    store.log_prediction({
        "type": "match", "uid": update.effective_user.id, "name": a["name"],
        "row": row, "home": home_name, "away": away_name, "ph": home, "pa": away,
    })
    # knockout draw → بپرس کی تو پنالتی صعود می‌کنه
    if match.get("knockout") and home == away:
        pen_row = match["pen_row"]
        await query.answer("نتیجه ثبت شد ✅")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🏆 {_team(home_name)}", callback_data=f"pen:{pen_row}:h")],
            [InlineKeyboardButton(f"🏆 {_team(away_name)}", callback_data=f"pen:{pen_row}:a")],
        ])
        await _edit(
            query,
            f"✅ ثبت شد: {_team(home_name)} *{_score(home, away)}* {_team(away_name)}\n\n"
            "⚖️ مساوی زدی! کدوم تیم تو پنالتی صعود می‌کنه جونم؟ 👇",
            reply_markup=kb,
        )
        return
    if match.get("knockout"):
        await _run(sheet.set_penalty_prediction, a["col"], match["pen_row"], None)
    await query.answer("ثبت شد ✅🔥")
    await _edit(
        query,
        f"✅ ثبت شد: {_team(home_name)} *{_score(home, away)}* {_team(away_name)} 🎯\n"
        "ایشالا که درست از آب در بیاد! 🤞",
    )


async def penalty_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """knockout draw: ذخیرهٔ تیمی که از نظر بازیکن تو پنالتی صعود می‌کنه."""
    query = update.callback_query
    _, pen_row, side = query.data.split(":")
    pen_row = int(pen_row)
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await query.answer()
        await _edit(query, _NOT_LINKED)
        return
    sheet = _sheet(context)
    score_row = pen_row - 1
    match = next((m for m in await _run(sheet.matches) if m["row"] == score_row), None)
    if not match or not match["open"]:
        await query.answer()
        await _edit(query, "⏰ این بازی همین الان بستِس — پنالتی ثبت نشد. 😬")
        return
    home_adv = side == "h"
    await _run(sheet.set_penalty_prediction, a["col"], pen_row, home_adv)
    winner = match["home"] if home_adv else match["away"]
    store.log_prediction({
        "type": "penalty", "uid": update.effective_user.id, "name": a["name"],
        "row": score_row, "winner": winner,
    })
    await query.answer("پنالتی ثبت شد ✅")
    await _edit(query, f"✅ ثبت شد: *{_team(winner)}* تو پنالتی صعود می‌کنه. 🎯\nموفق باشی جونم! 🤞")


async def special_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Player tapped a special prediction -> ask for a text answer."""
    query = update.callback_query
    await query.answer()
    if not store.get_assignment(update.effective_user.id):
        await _edit(query, _NOT_LINKED)
        return
    row = int(query.data.split(":")[1])
    special = next((s for s in await _run(_sheet(context).specials) if s["row"] == row), None)
    if not special:
        await _edit(query, "🔒 این یکی پیدا نشد جونم.")
        return
    if not _special_is_open(special):
        when = _final_deadline_str() if row in BONUS_SPECIAL_ROWS else _deadline_str()
        await _edit(query, f"⏰ مهلتِ این پیش‌بینی تموم شدِس ({when}). دیگه نمی‌شه ثبتش کرد جونم. 🔒")
        return
    context.user_data["await"] = ("special", row)
    context.user_data["label"] = special["label"]
    bonus = _fa_num(SPECIAL_BONUS.get(row, 0))
    if row in BONUS_SPECIAL_ROWS and _deadline_passed():
        msg = (
            f"🏆 *{special['label']}*\n\n"
            f"⚠️ مهلتِ قفلِ پیش‌بینی گذشتِس. می‌تونی عوضش کنی، ولی دیگه اون *{bonus} امتیازِ بونوسِ* قفل‌شدن بهت نمی‌رسه جونم.\n"
            "✍️ جوابتو بنویس بفرست."
        )
    elif row in BONUS_SPECIAL_ROWS:
        msg = (
            f"🏆 *{special['label']}*\n\n"
            f"💡 اگه تا ددلاین ({_deadline_str()}) ثبتش کنی و دیگه عوضش نکنی، اگه درست دربیاد *{bonus} امتیازِ اضافه* می‌گیری‌ها! 🤑\n"
            "✍️ جوابتو بنویس بفرست."
        )
    else:
        msg = f"🏆 *{special['label']}*\n\n✍️ جوابتو بنویس بفرست (مثلاً اسم یه تیم یا یه بازیکن)."
    await _edit(query, msg)


_MYP_PAGE = 10  # matches shown per page


async def _build_mypreds(context, a) -> dict:
    """Read & render the user's predictions once, cache them on user_data, and
    return {col, name, matches:[lines], specials:[lines]}. Pagination/back then
    slice the cache without re-hitting the sheet."""
    sheet = _sheet(context)
    preds = await _run(sheet.user_predictions, a["col"])
    pts = await _run(sheet.user_points, a["col"])
    matches = {m["row"]: m for m in await _run(sheet.matches)}
    specials = {s["row"]: s for s in await _run(sheet.specials)}

    mlines = []
    pens = preds.get("penalties", {})
    for row in sorted(preds["matches"]):
        m = matches.get(row)
        if not m:
            continue
        h, aw = preds["matches"][row]
        res = f"  (نتیجه‌ش شد: {_score(m['actual_home'], m['actual_away'])})" if m["actual_home"] is not None else ""
        pt = f"  🏅 {_fmt_total(pts[row])} امتیاز" if m["actual_home"] is not None and row in pts else ""
        pen = ""
        if m.get("knockout") and row in pens:
            winner = m["home"] if pens[row] == "home" else m["away"]
            pen = f" — {_team(winner)} تو پنالتی"
        mlines.append(f"⚽️ {_team(m['home'])} {_score(h, aw)} {_team(m['away'])}{pen}{res}{pt}")

    slines = []
    simple_specials = {r: v for r, v in preds["specials"].items() if r not in BONUS_SPECIAL_ROWS}
    for row in sorted(simple_specials):
        s = specials.get(row)
        pt = f"  🏅 {_fmt_total(pts[row])} امتیاز" if s and not s["open"] and row in pts else ""
        slines.append(f"🏆 {s['label'] if s else row}: {_iso(simple_specials[row])}{pt}")
    for row in sorted(BONUS_SPECIAL_ROWS):
        locked = preds["specials"].get(row)
        live = preds.get("specials_live", {}).get(row)
        eff = live or locked
        if not eff:
            continue
        s = specials.get(row)
        label = s["label"] if s else row
        bonus = _fa_num(SPECIAL_BONUS.get(row, 0))
        if live:
            status = "  (بعدِ ددلاین عوض شدِس — بدونِ بونوس)"
        elif _deadline_passed():
            status = f"  🔒 (قفل شدِس — واجدِ {bonus} امتیاز بونوس ✅)"
        else:
            status = f"  💡 (تا ددلاین عوضش نکنی، {bonus} امتیاز بونوس می‌گیری)"
        pt = f"  🏅 {_fmt_total(pts[row])} امتیاز" if s and not s["open"] and row in pts else ""
        slines.append(f"🏆 {label}: {_iso(eff)}{status}{pt}")

    data = {"col": a["col"], "name": a["name"], "matches": mlines, "specials": slines}
    context.user_data["myp"] = data
    return data


def _mypreds_default_view(data) -> tuple:
    """Default view: the last 10 matches + all specials, with a 'show all' button."""
    mlines, slines = data["matches"], data["specials"]
    out = [f"📋 *{data['name']}* جون، اینا پیش‌بینیاته 👇"]
    if mlines:
        shown = mlines[-_MYP_PAGE:]
        if len(mlines) > _MYP_PAGE:
            out.append(f"_آخرین {_fa_num(len(shown))} بازی (اِز کلِ {_fa_num(len(mlines))} بازی):_")
        out.append("")
        out += shown
    else:
        out.append("\n• هنوز هیچی پیش‌بینی نکردی که! 😴 دِ پاشو یه چیزی بزن دیگه تنبل‌خان 😏")
    if slines:
        out.append("")
        out += slines
    buttons = []
    if len(mlines) > _MYP_PAGE:
        buttons.append([InlineKeyboardButton("📄 نمایشِ همهٔ بازیا", callback_data="myp:p:0")])
    return "\n".join(out), (InlineKeyboardMarkup(buttons) if buttons else None)


def _mypreds_page_view(data, page) -> tuple:
    """All matches, paginated, with prev/next + a back button."""
    mlines = data["matches"]
    pages = max(1, (len(mlines) + _MYP_PAGE - 1) // _MYP_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _MYP_PAGE
    out = [f"📋 *{data['name']}* جون — همهٔ بازیا (صفحهٔ {_fa_num(page + 1)} اِز {_fa_num(pages)}) 👇", ""]
    out += mlines[start:start + _MYP_PAGE]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"myp:p:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"myp:p:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="myp:back")])
    return "\n".join(out), InlineKeyboardMarkup(rows)


async def cmd_mypredictions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await _say(update, _NOT_LINKED)
        return
    data = await _build_mypreds(context, a)
    text, kb = _mypreds_default_view(data)
    await _say(update, text, reply_markup=kb)


async def mypreds_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pagination + back for /mypredictions (slices the cached list)."""
    query = update.callback_query
    await query.answer()
    a = store.get_assignment(update.effective_user.id)
    if not a:
        await _edit(query, _NOT_LINKED)
        return
    data = context.user_data.get("myp")
    if not data or data.get("col") != a["col"]:
        data = await _build_mypreds(context, a)
    parts = query.data.split(":")
    if parts[1] == "back":
        text, kb = _mypreds_default_view(data)
    else:
        text, kb = _mypreds_page_view(data, int(parts[2]))
    await _edit(query, text, reply_markup=kb)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Others' scores are private — admin only (but posted publicly in the group).
    if not _is_admin(update.effective_user.id):
        await _say(update, "🔒 جدول فقط مال ادمینه جونم! فضولی نکن 😜")
        return
    standings = await _run(_sheet(context).standings)
    await _say(update, _leaderboard_lines(standings, "🏆📊 *جدول امتیازات* (زنده و بی‌رحم‌ها! 😈)\n"))


async def cmd_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = await _run(_sheet(context).matches)
    if not matches:
        await _say(update, "🤷 هنوز هیچ بازی‌ای ثبت نشده واللا.")
        return
    lines = ["📅⚽️ *برنامه‌ی بازیا*\n"]
    for m in matches:
        if m["actual_home"] is not None:
            score = _score(m["actual_home"], m["actual_away"])
            lines.append(f"✅ {_team(m['home'])} *{score}* {_team(m['away'])}")
        elif m["open"]:
            when = f"  ⏰{_fmt_kickoff(m['kickoff'])}" if m["kickoff"] else ""
            lines.append(f"🔵 {_team(m['home'])} 🆚 {_team(m['away'])}  _(باز)_{when}")
        else:  # started but no result yet (only when locking is enabled)
            lines.append(f"🔒 {_team(m['home'])} 🆚 {_team(m['away'])}  _(قفل)_")
    await _say(update, "\n".join(lines))


# ── Free text: menu buttons + special-prediction answers ─────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route menu-button taps to the right handler; otherwise treat the text as
    an awaited special-prediction answer."""
    text = (update.message.text or "").strip()

    # Menu buttons (work even mid-flow, so a stray button never gets eaten).
    if text == BTN_PREDICT:
        return await cmd_predict(update, context)
    if text == BTN_SPECIAL:
        return await cmd_special(update, context)
    if text == BTN_MINE:
        return await cmd_mypredictions(update, context)
    if text == BTN_MATCHES:
        return await cmd_matches(update, context)
    if text == BTN_LEADERBOARD:
        return await cmd_leaderboard(update, context)
    if text == BTN_WHOAMI:
        return await cmd_whoami(update, context)
    if text == BTN_HELP:
        return await cmd_help(update, context)
    if text == BTN_BROADCAST:
        return await cmd_broadcast(update, context)
    if text == BTN_SEND_TABLE:
        return await cmd_send_table(update, context)
    if text == BTN_SEND_PREDS:
        return await cmd_send_preds(update, context)
    if text == BTN_TOGGLE_AUTO:
        return await cmd_toggle_auto(update, context)
    if text == BTN_PREDLOG:
        return await cmd_predlog(update, context)
    if text == BTN_SEND_GROUP:
        return await cmd_sendgroup(update, context)
    if text == BTN_TOPGAINERS:
        return await cmd_topgainers(update, context)

    pending = context.user_data.get("await")
    # Admin is composing a broadcast: this text is the message to send to everyone.
    if pending and pending[0] == "broadcast":
        context.user_data.clear()
        if not _is_admin(update.effective_user.id):
            return
        await _broadcast(update, context, text)
        return
    # Admin is composing a group message: post it in the group as the bot.
    if pending and pending[0] == "groupmsg":
        context.user_data.clear()
        if not _is_admin(update.effective_user.id):
            return
        await _post_to_group(update, context, text)
        return
    if not pending or pending[0] != "special":
        return  # nothing awaited; ignore stray text
    row = pending[1]
    a = store.get_assignment(update.effective_user.id)
    if not a:
        context.user_data.clear()
        await _say(update, "😕 دیگه به هیچ اسمی وصل نیستی واللا. برو با ادمین حرف بزن.")
        return
    label = context.user_data.get("label", "")
    bonus = _fa_num(SPECIAL_BONUS.get(row, 0))
    # non-bonus specials hard-close at the first deadline (guard if it passed mid-typing)
    if row not in BONUS_SPECIAL_ROWS and _deadline_passed():
        context.user_data.clear()
        await _say(update, f"⏰ مهلتِ این پیش‌بینی تموم شدِس ({_deadline_str()}). دیگه نمی‌شه ثبتش کرد جونم. 🔒")
        return
    # bonus specials fully lock once the knockout stage starts
    if row in BONUS_SPECIAL_ROWS and _champion_locked():
        context.user_data.clear()
        await _say(update, f"⏰ مهلتِ تغییرِ این پیش‌بینی تموم شدِس ({_final_deadline_str()}). دیگه قفله جونم. 🔒")
        return
    # a bonus special changed after the first deadline -> SECOND cell, no bonus
    after = row in BONUS_SPECIAL_ROWS and _deadline_passed()
    await _run(_sheet(context).set_special_prediction, a["col"], row, text, after)
    store.log_prediction({
        "type": "special", "uid": update.effective_user.id, "name": a["name"],
        "row": row, "label": label, "text": text, "after_deadline": after,
    })
    context.user_data.clear()
    if after:
        await _say(
            update,
            f"✅ ثبت شد: {label}  →  *{_iso(text)}*\n"
            f"⚠️ چون بعدِ ددلاین بود، اون {bonus} امتیازِ بونوس بهت نمی‌رسه جونم.",
        )
    elif row in BONUS_SPECIAL_ROWS:
        await _say(
            update,
            f"✅ ثبت شد: {label}  →  *{_iso(text)}* 🏆\n"
            f"🔒 اگه تا ددلاین دیگه عوضش نکنی و درست دربیاد، {bonus} امتیازِ اضافه می‌گیری‌ها! 🤑",
        )
    else:
        await _say(
            update,
            f"✅ ثبت شد: {label}  →  *{_iso(text)}* 🎯\nبه‌به، چه با اعتماد‌به‌نفس! 😎",
        )


# ── Admin commands ───────────────────────────────────────────────────────
async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    slots = await _run(_sheet(context).slots, True)
    assigned = {info["col"]: (uid, info) for uid, info in store.all_assignments().items()}
    lines = ["📝 *اسلات‌ها* (شماره — اسم — وصل به کی)\n"]
    for s in slots:
        who = assigned.get(s["col"])
        tag = f"{who[1]['name']} (آیدی {_iso(who[0])})" if who else "—"
        lines.append(f"{_fa_num(s['idx'])}. {s['name']}  →  {tag}")
    lines.append("\n🔗 برای وصل کردن: `/assign <idx> <telegram_id>`")
    await _say(update, "\n".join(lines))


async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await _say(update, "📌 طرز استفاده: `/assign <idx> <telegram_id>`")
        return
    idx, target = int(args[0]), int(args[1])
    slot = await _run(_sheet(context).slot_by_idx, idx)
    if not slot:
        await _say(update, f"🤔 اسلاتی با شماره‌ی {_iso(idx)} نداریم. /slots رو ببین.")
        return
    taken = store.col_is_taken(slot["col"])
    if taken and taken != target:
        await _say(
            update,
            f"⚠️ {slot['name']} قبلاً به آیدی {_iso(taken)} وصله. "
            "اگه می‌خوای عوضش کنی، اول /unassign رو بزن.",
        )
        return
    store.set_assignment(target, slot["name"], slot["col"])
    # Tell the newly linked person they're in (only works if they've /start-ed the bot).
    notified = True
    try:
        await context.bot.send_message(
            chat_id=target,
            text=_rtl(
                f"🎉🎊 تبریک *{slot['name']}* جون! ادمین تو رو به مسابقه‌ی پیش‌بینی دوره نهم کتان ممپف اضافه کرد! ⚽️🔥\n\n"
                "از الان می‌تونی پیش‌بینی کنی 😍\n"
                "👇 دکمه‌ی «🎯 پیش‌بینی بازی» رو بزن، یا «❓ راهنما» رو واسه آموزش."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_kb(_is_admin(target)),
        )
    except Exception as e:
        notified = False
        logging.warning("Assign notify to %s failed: %s", target, e)
    msg = f"✅🔗 آیدی {_iso(target)} به *{slot['name']}* وصل شد! خوش اومدی به جمعمون 🎉"
    if not notified:
        msg += "\n⚠️ ولی نتونستم بهش خبر بدم — باید اول خودش ربات رو /start کنه، بعد دوباره assign کن (یا بهش بگو استارت کنه)."
    await _say(update, msg)


async def cmd_unassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await _say(update, "📌 طرز استفاده: `/unassign <telegram_id>`")
        return
    target = int(context.args[0])
    ok = store.remove_assignment(target)
    await _say(update, "✅ حذف شد. خداحافظ! 👋" if ok else "🤷 این آیدی اصلاً وصل نبود.")


async def cmd_assignments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    data = store.all_assignments()
    if not data:
        await _say(update, "🤷 هنوز هیچ‌کس وصل نشده. /slots رو ببین.")
        return
    lines = ["🔗 *وصل‌شده‌های فعلی*\n"]
    for uid, info in sorted(data.items(), key=lambda x: x[1]["name"]):
        lines.append(f"• {info['name']} — آیدی {_iso(uid)}")
    await _say(update, "\n".join(lines))


# ── Broadcast: admin sends one message to every linked participant ───────
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    # /broadcast <text> sends immediately; bare button/command asks for the text.
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        context.user_data["await"] = ("broadcast", None)
        await _say(update, "✍️ متن پیامی که می‌خوای برای *همه‌ی شرکت‌کننده‌ها* بره رو بفرست:")
        return
    await _broadcast(update, context, text)


async def _broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    data = store.all_assignments()
    if not data:
        await _say(update, "🤷 هنوز هیچ شرکت‌کننده‌ای وصل نشده که بهش پیام بدم.")
        return
    # Sent without Markdown parsing so the admin's text can't break formatting,
    # and with no prefix/header — exactly the admin's words, nothing added.
    out = _rtl(text)
    sent = failed = 0
    for uid in data:
        try:
            await context.bot.send_message(chat_id=uid, text=out)
            sent += 1
        except Exception as e:
            failed += 1
            logging.warning("Broadcast to %s failed: %s", uid, e)
    msg = f"✅ پیام به *{_fa_num(sent)}* نفر فرستاده شد."
    if failed:
        msg += f"\n⚠️ *{_fa_num(failed)}* نفر ناموفق (شاید ربات رو استارت نکردن یا بلاک کردن)."
    await _say(update, msg)


# ── Send to group: admin posts a message in the group, as the bot ────────
async def cmd_sendgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not store.get_group_id():
        await _say(update, "⚠️ هنوز هیچ گروهی ثبت نشده‌ها. اول تو خودِ گروه `/setgroup` رو بزن.")
        return
    # /sendgroup <text> posts immediately; bare button/command asks for the text.
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        context.user_data["await"] = ("groupmsg", None)
        await _say(update, "✍️ متنی که می‌خوای ربات از طرفِ خودش تو *گروه* بذاره رو بفرست:")
        return
    await _post_to_group(update, context, text)


async def _post_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    gid = store.get_group_id()
    if not gid:
        await _say(update, "⚠️ هنوز هیچ گروهی ثبت نشده. اول تو گروه `/setgroup` رو بزن.")
        return
    try:
        # No prefix, no Markdown parsing — exactly what the admin wrote, as the bot.
        await context.bot.send_message(chat_id=gid, text=_rtl(text), **_thread_kw())
        await _say(update, "✅ گذاشتمش تو گروه. 📨")
    except Exception as e:
        logging.warning("Group post failed: %s", e)
        await _say(update, "❌ نشد بذارم تو گروه. مطمئن شو ربات تو گروه هست و دسترسیِ ارسال داره.")


# ── Kickoff-time sync (API → sheet) ──────────────────────────────────────
async def _sync_kickoffs(context) -> dict:
    sheet = _sheet(context)
    kmap = await asyncio.to_thread(api_client.fetch_kickoffs)
    if not kmap:
        return {"written": 0, "unmatched": [], "fetched": 0}
    report = await asyncio.to_thread(sheet.sync_kickoffs, kmap)
    report["fetched"] = len(kmap)
    return report


async def refresh_kickoffs_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic background refresh of kickoff times from the football API."""
    report = await _sync_kickoffs(context)
    logging.info(
        "Kickoff sync: fetched %d, wrote %d new, %d unmatched.",
        report.get("fetched", 0), report["written"], len(report["unmatched"]),
    )
    if report["unmatched"]:
        logging.warning(
            "Unmatched fixtures — set their kickoff manually in the sheet: %s",
            "; ".join(report["unmatched"]),
        )


async def cmd_synckickoffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    await _say(update, "⏳🔄 دارم زمان شروع بازی‌ها رو از API می‌گیرم…")
    report = await _sync_kickoffs(context)
    msg = f"✅ *{_fa_num(report.get('fetched', 0))}* بازی گرفته شد، *{_fa_num(report['written'])}* زمان شروع جدید نوشته شد. 🕐"
    if report["unmatched"]:
        shown = "\n".join(f"  • {_team(x.split(' – ')[0])} 🆚 {_team(x.split(' – ')[-1])}" for x in report["unmatched"][:25])
        msg += (
            f"\n\n⚠️ *{_fa_num(len(report['unmatched']))}* بازی با اسم جور در نیومد — "
            f"زمان شروعشونو دستی تو ستون *BV* شیت بذار:\n{shown}"
        )
    await _say(update, msg)


# ── Knockout fixtures sync (API → sheet, as each round's teams are decided) ─
async def _sync_knockout(context) -> dict:
    km = await asyncio.to_thread(api_client.fetch_knockout)
    if not km:
        return {"teams": 0, "kickoffs": 0, "added": []}
    return await asyncio.to_thread(_sheet(context).sync_knockout_teams, km)


async def refresh_knockout_job(context: ContextTypes.DEFAULT_TYPE):
    """Once-a-day background fill of newly-decided knockout fixtures into the
    sheet (teams + kickoff), so a fresh round appears in the bot on its own."""
    report = await _sync_knockout(context)
    if report["teams"] or report["kickoffs"]:
        logging.info(
            "Knockout sync: added %d matchup(s), %d kickoff(s): %s",
            report["teams"], report["kickoffs"], "; ".join(report["added"]),
        )


async def cmd_syncknockout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    await _say(update, "⏳🔄 د وایسا بازی‌های تازه‌ی مرحله‌ی حذفی رو از API بیارم…")
    report = await _sync_knockout(context)
    if not report["teams"] and not report["kickoffs"]:
        await _say(update, "🤷 د بازی تازه‌ای نبود که اضاف کنم (تیمای دور بعد هنوز معلوم نشده‌ن).")
        return
    shown = "\n".join(
        f"  • {_team(x.split(' – ')[0])} 🆚 {_team(x.split(' – ')[-1])}" for x in report["added"][:25]
    )
    await _say(
        update,
        f"✅ د *{_fa_num(report['teams'])}* بازی تازه اضاف شد و *{_fa_num(report['kickoffs'])}* تا زمون شروع نوشته شد ها. ⚽️"
        + (f"\n{shown}" if shown else ""),
    )


# ── Results sync (API → sheet, after matches finish) ─────────────────────
async def _sync_results(context) -> dict:
    results = await asyncio.to_thread(api_client.fetch_results)
    if not results:
        return {"written": 0, "scored": []}
    return await asyncio.to_thread(_sheet(context).sync_results, results)


async def refresh_results_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic background pull of finished-match results into the sheet."""
    report = await _sync_results(context)
    if report["written"]:
        logging.info("Results sync: wrote %d new result(s): %s",
                     report["written"], "; ".join(report["scored"]))


async def cmd_syncresults(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    await _say(update, "⏳🔄 دارم نتیجه‌ی بازی‌های تموم‌شده رو از API می‌گیرم…")
    report = await _sync_results(context)
    if not report["written"]:
        await _say(update, "🤷 نتیجه‌ی جدیدی برای نوشتن نبود (یا بازیا هنوز تموم نشدن).")
        return
    shown = "\n".join(f"  • {_iso(s)}" for s in report["scored"][:25])
    await _say(update, f"✅ *{_fa_num(report['written'])}* نتیجه تو شیت ثبت شد:\n{shown}")


# ── Reminders: nudge users who haven't predicted upcoming matches ────────
def _window_for(hours_left: float):
    """Which reminder window (in hours) the time-to-kickoff falls into, or None.
    Bands are (next_smaller, window]: e.g. for [24,3,1] → (3,24], (1,3], (0,1]."""
    windows = REMINDER_WINDOWS_HOURS
    for i, w in enumerate(windows):
        lower = windows[i + 1] if i + 1 < len(windows) else 0
        if lower < hours_left <= w:
            return w
    return None


def _reminder_text(window: int, name: str, home: str, away: str, kickoff) -> str:
    match = f"*{_team(home)}* 🆚 *{_team(away)}*"
    when = _fmt_kickoff(kickoff)
    if window == 24:
        return _rtl(
            f"⏳😴 {name} جون، ۲۴ ساعت مونده به {match} و تو هنوز پیش‌بینی نکردی که!\n"
            f"🕐 سوت شروع: {when}\n"
            "نکنه می‌خوای غافلگیرمون کنی؟ 🤨 دِ بیو بزن 👉 «🎯 پیش‌بینی بازی»"
        )
    if window == 3:
        return _rtl(
            f"🚨🔥 آژیر قرمز {name}! فقط ۳ ساعت مونده به {match} و هنوز خبری از پیش‌بینیت نی! 😱\n"
            f"🕐 سوت شروع: {when}\n"
            "دِ بجمب دیگه قهرمان 🏃‍♂️💨 «🎯 پیش‌بینی بازی»"
        )
    return _rtl(
        f"⏰😭 آخرین هشدار {name}! یه ساعتم نمونده به {match}!\n"
        f"🕐 سوت شروع: {when}\n"
        "الان نزنی بعداً غصه نخوری‌ها 🐊 «🎯 پیش‌بینی بازی»"
    )


# Synthetic dedup key (a non-match "row") for the special-deadline reminder.
_SPECIAL_REM_ROW = -1


def _special_reminder_text(window: int, name: str, missing: list) -> str:
    items = "\n".join(f"• {m}" for m in missing)
    when = _deadline_str()
    head = (
        f"⏳ {name} جون، ۲۴ ساعت تا بسته‌شدنِ پیش‌بینی‌های ویژه موندِس! 😱"
        if window == 24 else
        f"🚨🔥 {name}! فقط ۱ ساعت تا بسته‌شدنِ پیش‌بینی‌های ویژه موندِس!"
    )
    return _rtl(
        f"{head}\n\nاین‌هارو هنوز ثبت نکردی:\n{items}\n\n"
        f"🕐 مهلت تا شروعِ دور سومِ گروهی: {when}\n"
        "👇 دکمه‌ی «🏆 پیش‌بینی ویژه» رو بزن.\n"
        "🤑 یادت باشه قهرمانو زود قفل کنی تا ۵ امتیازِ بونوس بگیری!"
    )


async def _special_deadline_reminders(context, assignments, now) -> int:
    """24h & 1h before the special deadline, DM users who haven't locked the
    champion / best-player / top-scorer yet. One bulk read; deduped per window."""
    if not SPECIAL_DEADLINE_DT:
        return 0
    hrs = (SPECIAL_DEADLINE_DT - now).total_seconds() / 3600.0
    window = 24 if 1 < hrs <= 24 else (1 if 0 < hrs <= 1 else None)
    if window is None:
        return 0
    cols = [info["col"] for info in assignments.values()]
    try:
        locked = await _run(_sheet(context).special_locked_by_col, cols)
    except Exception as e:
        logging.warning("Special reminder: read failed: %s", e)
        return 0
    targets = [(CHAMPION_ROW, "🏆 قهرمان جام"), (BESTPLAYER_ROW, "🌟 بهترین بازیکن تورنمنت"), (TOPSCORER_ROW, "⚽️ آقای گل")]
    sent = 0
    for uid, info in assignments.items():
        if store.was_reminded(uid, _SPECIAL_REM_ROW, window):
            continue
        have = locked.get(info["col"], {})
        missing = [lbl for r, lbl in targets if r not in have]
        if not missing:
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=_special_reminder_text(window, info["name"], missing),
                parse_mode=ParseMode.MARKDOWN,
            )
            store.mark_reminded(uid, _SPECIAL_REM_ROW, window)
            sent += 1
        except Exception as e:
            logging.warning("Special reminder: send to %s failed: %s", uid, e)
    return sent


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Every REMINDER_CHECK_MINUTES: DM each linked user who hasn't predicted a
    match yet, when it's 24h / 3h / 1h away. One reminder per (user, match,
    window). Also fires the special-prediction deadline reminders (24h & 1h)."""
    sheet = _sheet(context)
    matches = await _run(sheet.matches)
    now = datetime.now(timezone.utc)
    upcoming = [
        (m, (m["kickoff"] - now).total_seconds() / 3600.0)
        for m in matches
        if m["kickoff"] and m["kickoff"] > now
    ]
    due = [(m, _window_for(hrs)) for m, hrs in upcoming]
    due = [(m, w) for m, w in due if w is not None]

    assignments = store.all_assignments()
    if not assignments:
        return

    sent = 0
    # ── Match reminders ──────────────────────────────────────────────────
    # Read EVERYONE's predicted rows in ONE bulk call. Reading per-user (2 reads
    # each) blew the Sheets read-quota (60/min) with many players, and the old
    # fallback treated a failed read as "predicted nothing" — so people who HAD
    # predicted got false "you didn't predict" nudges. Now: if the bulk read
    # fails, skip just the match nudges rather than risk a wrong reminder.
    if due:
        cols = [info["col"] for info in assignments.values()]
        try:
            pred_by_col = await _run(sheet.predicted_rows_for_cols, cols)
        except Exception as e:
            logging.warning("Reminder: bulk prediction read failed, skipping match nudges: %s", e)
            pred_by_col = None
        if pred_by_col is not None:
            predicted = {uid: pred_by_col.get(info["col"], set()) for uid, info in assignments.items()}
            for m, window in due:
                for uid, info in assignments.items():
                    if m["row"] in predicted.get(uid, set()):
                        continue  # already predicted — no nudge
                    if store.was_reminded(uid, m["row"], window):
                        continue  # already nudged for this window
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=_reminder_text(window, info["name"], m["home"], m["away"], m["kickoff"]),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        store.mark_reminded(uid, m["row"], window)
                        sent += 1
                    except Exception as e:
                        logging.warning("Reminder: send to %s failed: %s", uid, e)

    # ── Special-prediction deadline reminders (24h & 1h before) ──────────
    sent += await _special_deadline_reminders(context, assignments, now)

    if sent:
        logging.info("Sent %d reminder(s).", sent)


# ── Group announcements: daily analysis call + post-match leaderboard ─────
async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin runs this INSIDE the main group to register it for announcements."""
    if not _is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ این دستورو باید داخل خودِ گروهِ اصلی بزنی.")
        return
    sheet = _sheet(context)
    standings = await _run(sheet.standings)
    matches = await _run(sheet.matches)
    finished = [m["row"] for m in matches if m["actual_home"] is not None]
    started = [m["row"] for m in matches if m["started"]]
    tid = update.effective_message.message_thread_id  # forum topic, or None (General)
    # baseline now so we don't dump a backlog of already started/finished matches.
    store.init_announce(chat.id, {str(e["col"]): e["total"] for e in standings}, finished, started, thread_id=tid)
    where = "همین تاپیک" if tid else "تاپیکِ General"
    await update.message.reply_text(
        f"✅ این گروه ثبت شد! از این به بعد اعلام‌ها تو *{where}* میاد.\n"
        "• بعد از شروعِ هر بازی، پیش‌بینیِ همه اعلام می‌شه. 🔒\n"
        "• بعد از پایانِ هر بازی هم جدول منتشر می‌شه. 🏁",
        parse_mode=ParseMode.MARKDOWN,
    )


_GAINER_MEDALS = ["🥇", "🥈", "🥉", "🏅", "🏅"]


def _gainers_message(items: list) -> str:
    """Build the 'yesterday's top gainers' group post from stored items
    (list of {col,name,points}). Re-resolves the tag each time it's posted."""
    col_uid = _col_uid_map()
    lines = [
        "🌅 صبح‌تون بخیر و شادی، رفقای گُل! 👋",
        "🏅 دیشب این عزیزان بیشترین امتیازو وردشتِس:\n",
    ]
    for i, it in enumerate(items):
        medal = _GAINER_MEDALS[i] if i < len(_GAINER_MEDALS) else "🏅"
        lines.append(
            f"{medal} {_mention(it['name'], col_uid.get(it['col']))} — "
            f"دیروز *{_iso(_fmt_total(it['points']))}* امتیاز گرفتِس 📈"
        )
    lines.append(
        "\nدمتون گرم، تبریک می‌گم بهتون! 🎉 د حالا که اینقده خوش‌فکرید، "
        "بی‌زحمت بازی‌های امروزو یه تحلیل کنید واسه‌مون ببینیم نظرتون چیه 👀⚽️"
    )
    return _rtl("\n".join(lines))


async def _post_daily_gainers(context: ContextTypes.DEFAULT_TYPE):
    """Daily morning: compute the last 24h point-gainers (current totals vs the
    previous morning's snapshot), re-baseline the snapshot, store the result so
    the admin can re-post it on demand, then post it. TOP_GAINERS_COUNT people."""
    gid = store.get_group_id()
    if not gid:
        return
    standings = await _run(_sheet(context).standings)
    prev = store.get_snapshot()
    store.set_snapshot({str(e["col"]): e["total"] for e in standings})  # new daily baseline
    if not prev:
        return  # first run after /setgroup — baseline set, nothing to compare yet
    deltas = []
    for e in standings:
        p = prev.get(str(e["col"]))
        if p is None:
            continue
        d = e["total"] - float(p)
        if d > 0.0001:
            deltas.append((e, d))
    if not deltas:
        return  # nobody gained points yesterday
    deltas.sort(key=lambda x: x[1], reverse=True)
    items = [{"col": e["col"], "name": e["name"], "points": d} for e, d in deltas[:TOP_GAINERS_COUNT]]
    store.set_last_gainers(items)  # so /topgainers can re-post this exact report
    try:
        await context.bot.send_message(
            chat_id=gid, text=_gainers_message(items),
            parse_mode=ParseMode.MARKDOWN, **_thread_kw(),
        )
    except Exception as e:
        logging.warning("Daily gainers post failed: %s", e)


async def cmd_topgainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: re-post the latest daily 'yesterday's top gainers' report to the
    group on demand. Re-posts the stored morning result (not a fresh cumulative
    calc), so the number is always the real per-day gain — never a long total."""
    if not _is_admin(update.effective_user.id):
        return
    if not store.get_group_id():
        await _say(update, "⚠️ هنوز هیچ گروهی ثبت نشده‌ها. اول تو خودِ گروه `/setgroup` رو بزن.")
        return
    items = store.get_last_gainers()
    if not items:
        await _say(update, "🤔 هنوز گزارشِ روزانه‌ای ثبت نشده‌ها. اولین گزارش فردا ساعت ۹ صبح خودش میاد، بعدش می‌تونی همین‌جا دوباره بفرستیش.")
        return
    try:
        await context.bot.send_message(
            chat_id=store.get_group_id(), text=_gainers_message(items),
            parse_mode=ParseMode.MARKDOWN, **_thread_kw(),
        )
        await _say(update, "✅ نفراتِ برترِ دیروز تو گروه اعلام شدِس. 🏅")
    except Exception as e:
        logging.warning("Top-gainers repost failed: %s", e)
        await _say(update, "❌ نشد بذارم تو گروه. مطمئن شو ربات تو گروهه و اجازه‌ی ارسال داره.")


async def _match_predictions_text(context, m) -> str:
    """The 'predictions are locked' message for one match, with team names so each
    score is unambiguous."""
    preds = await _run(_sheet(context).match_all_predictions, m["row"], m.get("pen_row"))
    head = (
        f"⏰🔒 مهلتِ ارسالِ پیش‌بینیِ بازیِ *{_team(m['home'])}* 🆚 *{_team(m['away'])}* به پایان رسید!\n\n"
        "📋 پیش‌بینیِ همه:"
    )
    if preds:
        home_fa, away_fa = _team_fa(m["home"]), _team_fa(m["away"])
        rows = []
        for p in preds:
            line = _pred_line(p["name"], home_fa, away_fa, p["home"], p["away"])
            if m.get("knockout") and p.get("pen") and p["home"] == p["away"]:
                winner_fa = home_fa if p["pen"] == "home" else away_fa
                line += f" — {_iso(winner_fa)} تو پنالتی"
            rows.append(line)
    else:
        rows = ["• هیچکس برای این بازی پیش‌بینی نکرده بود! 😅"]
    return head + "\n" + "\n".join(rows)


async def _post_match_predictions(context, m) -> bool:
    """Auto-post predictions to the group (deduped via the 'started' set)."""
    gid = store.get_group_id()
    if not gid or m["row"] in store.get_started():
        return False
    try:
        await context.bot.send_message(
            chat_id=gid, text=_rtl(await _match_predictions_text(context, m)),
            parse_mode=ParseMode.MARKDOWN, **_thread_kw(),
        )
        store.set_started(list(store.get_started() | {m["row"]}))
        return True
    except Exception as e:
        logging.warning("Predictions post failed (row %s): %s", m["row"], e)
        return False


async def kickoff_fire_job(context: ContextTypes.DEFAULT_TYPE):
    """Fires exactly at a match's kickoff -> post that match's predictions."""
    row = context.job.data
    m = next((x for x in await _run(_sheet(context).matches) if x["row"] == row), None)
    if m and m["started"]:
        await _post_match_predictions(context, m)


async def reschedule_kickoffs_job(context: ContextTypes.DEFAULT_TYPE):
    """Keep a precise run-once timer set for every upcoming match at its exact
    kickoff. Re-runs periodically so new/updated kickoffs (and restarts) are covered."""
    if not store.get_group_id() or context.job_queue is None:
        return
    now = datetime.now(timezone.utc)
    started = store.get_started()
    for m in await _run(_sheet(context).matches):
        ko = m["kickoff"]
        if not ko or ko <= now or m["row"] in started:
            continue
        name = f"ko:{m['row']}"
        if context.job_queue.get_jobs_by_name(name):
            continue
        context.job_queue.run_once(kickoff_fire_job, when=ko, name=name, data=m["row"])


async def group_announce_job(context: ContextTypes.DEFAULT_TYPE):
    """Every 15 min: pull just-finished results from the API into the sheet, post
    predictions for any started match the exact-kickoff timer missed, and post the
    leaderboard for newly finished matches."""
    gid = store.get_group_id()
    if not gid:
        return
    # First, write any newly-finished results into the sheet, so the leaderboard
    # below already reflects games that just ended (no waiting for the hourly job).
    try:
        await _sync_results(context)
    except Exception as e:
        logging.warning("Announce results sync failed: %s", e)
    sheet = _sheet(context)
    matches = await _run(sheet.matches)

    # 1) Started matches not yet announced -> reveal everyone's predictions.
    for m in sorted([x for x in matches if x["started"]], key=lambda x: x["row"]):
        if m["row"] not in store.get_started():
            await _post_match_predictions(context, m)

    # 2) Newly FINISHED (result entered) -> post the leaderboard (if auto is on).
    finished = {m["row"]: m for m in matches if m["actual_home"] is not None}
    announced = store.get_announced()
    new_finished = [r for r in finished if r not in announced]
    if new_finished:
        posted = True
        if store.get_auto_leaderboard():
            standings = await _run(sheet.standings)
            fin = "\n".join(
                f"✅ {_team(finished[r]['home'])} "
                f"*{_score(finished[r]['actual_home'], finished[r]['actual_away'])}* "
                f"{_team(finished[r]['away'])}"
                for r in sorted(new_finished)
            )
            body = _leaderboard_lines(standings, "📊 *جدول به‌روز شد:*\n")
            try:
                await context.bot.send_message(
                    chat_id=gid, text=_rtl(f"🏁 بازی تموم شد!\n{fin}\n\n{body}"),
                    parse_mode=ParseMode.MARKDOWN, **_thread_kw(),
                )
            except Exception as e:
                logging.warning("Match-end leaderboard post failed: %s", e)
                posted = False
        if posted:  # mark either way (silently when auto is off) to avoid a backlog
            store.set_announced(list(announced | set(finished.keys())))


# ── Daily 9am: the next 24 hours' fixtures ────────────────────────────────
async def daily_fixtures_job(context: ContextTypes.DEFAULT_TYPE):
    """Each morning: post the next-24h fixtures, then congratulate & tag
    yesterday's top point-gainers and ask them to analyze today's matches."""
    gid = store.get_group_id()
    if not gid:
        return
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    upcoming = [
        m for m in await _run(_sheet(context).matches)
        if m["kickoff"] and now < m["kickoff"] <= horizon and m["actual_home"] is None
    ]
    if upcoming:
        upcoming.sort(key=lambda m: m["kickoff"])
        lines = ["☀️ *بازی‌های ۲۴ ساعتِ آینده* ⚽️\n"]
        for m in upcoming:
            lines.append(f"⚽️ {_team(m['home'])} 🆚 {_team(m['away'])}  ⏰ {_fmt_kickoff(m['kickoff'])}")
        lines.append("\n⏳ یادتون نره پیش‌بینی‌هاتونو ثبت کنید! 🎯")
        try:
            await context.bot.send_message(chat_id=gid, text=_rtl("\n".join(lines)), parse_mode=ParseMode.MARKDOWN, **_thread_kw())
        except Exception as e:
            logging.warning("Daily fixtures post failed: %s", e)
    # Then: yesterday's top gainers (congratulate, tag, ask for today's analysis).
    await _post_daily_gainers(context)


# ── Admin on-demand controls ──────────────────────────────────────────────
async def cmd_send_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the leaderboard to wherever the admin ran it (group or private)."""
    if not _is_admin(update.effective_user.id):
        return
    standings = await _run(_sheet(context).standings)
    await _say(update, _leaderboard_lines(standings, "🏆📊 *جدول امتیازات*\n"))


async def cmd_send_preds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin picks a started match; its predictions are posted to this same chat."""
    if not _is_admin(update.effective_user.id):
        return
    started = [m for m in await _run(_sheet(context).matches) if m["started"]]
    if not started:
        await _say(update, "هنوز هیچ بازی‌ای شروع نشده که پیش‌بینی‌هاش رو بفرستم.")
        return
    started.sort(key=lambda m: m["kickoff"], reverse=True)
    buttons = [
        [InlineKeyboardButton(f"{_team(m['home'])} 🆚 {_team(m['away'])}", callback_data=f"spsend:{m['row']}")]
        for m in started[:10]
    ]
    await _say(update, "کدوم بازی؟ پیش‌بینی‌هاش رو همین‌جا می‌فرستم 👇", reply_markup=InlineKeyboardMarkup(buttons))


async def send_preds_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(update.effective_user.id):
        await query.answer()
        return
    await query.answer()
    row = int(query.data.split(":")[1])
    m = next((x for x in await _run(_sheet(context).matches) if x["row"] == row), None)
    if not m:
        await _edit(query, "بازی پیدا نشد.")
        return
    text = await _match_predictions_text(context, m)
    await context.bot.send_message(chat_id=query.message.chat_id, text=_rtl(text), parse_mode=ParseMode.MARKDOWN)
    await _edit(query, f"✅ پیش‌بینی‌های بازیِ *{_team(m['home'])}* 🆚 *{_team(m['away'])}* فرستاده شد.")


async def cmd_toggle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle the automatic post-match leaderboard on/off."""
    if not _is_admin(update.effective_user.id):
        return
    new = not store.get_auto_leaderboard()
    store.set_auto_leaderboard(new)
    if new:
        await _say(update, "✅ ارسالِ خودکارِ جدول بعد از هر بازی *روشن* شد.")
    else:
        await _say(
            update,
            "⏸️ ارسالِ خودکارِ جدول *خاموش* شد.\n"
            "از این به بعد هر وقت خواستی، دستی با «📤 ارسال جدول» بفرست.",
        )


async def cmd_predlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: get the full prediction history as a file + a preview of the last 20."""
    if not _is_admin(update.effective_user.id):
        return
    recs = store.read_prediction_log()
    if not recs:
        await _say(update, "هنوز هیچ پیش‌بینی‌ای ثبت نشده.")
        return
    try:
        with open(PREDLOG_FILE, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename="predictions_log.jsonl",
                caption=f"📜 کلِ تاریخچه‌ی پیش‌بینی‌ها — {_fa_num(len(recs))} رکورد",
            )
    except Exception as e:
        logging.warning("predlog document send failed: %s", e)
    lines = ["📜 *۲۰ پیش‌بینیِ آخر* (کاملش تو فایلِ بالاست):\n"]
    for r in recs[-20:]:
        try:
            when = _fmt_kickoff(datetime.fromisoformat(r["ts"]))
        except Exception:
            when = ""
        if r.get("type") == "special":
            body = f"• {_iso(r.get('name', '?'))}: {r.get('label', '')} → {_iso(str(r.get('text', '')))}"
        else:
            home_fa, away_fa = _team_fa(r.get("home", "")), _team_fa(r.get("away", ""))
            body = _pred_line(r.get("name", "?"), home_fa, away_fa, r.get("ph", 0), r.get("pa", 0))
        lines.append(f"{body}  🕐 {when}")
    await _say(update, "\n".join(lines))


def register(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("special", cmd_special))
    app.add_handler(CommandHandler("mypredictions", cmd_mypredictions))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("matches", cmd_matches))
    # admin
    app.add_handler(CommandHandler("slots", cmd_slots))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("unassign", cmd_unassign))
    app.add_handler(CommandHandler("assignments", cmd_assignments))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("sendgroup", cmd_sendgroup))
    app.add_handler(CommandHandler("topgainers", cmd_topgainers))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("sendtable", cmd_send_table))
    app.add_handler(CommandHandler("sendpreds", cmd_send_preds))
    app.add_handler(CommandHandler("autoresults", cmd_toggle_auto))
    app.add_handler(CommandHandler("predlog", cmd_predlog))
    app.add_handler(CommandHandler("synckickoffs", cmd_synckickoffs))
    app.add_handler(CommandHandler("syncknockout", cmd_syncknockout))
    app.add_handler(CommandHandler("syncresults", cmd_syncresults))
    # interactive
    app.add_handler(CallbackQueryHandler(send_preds_choice, pattern=r"^spsend:\d+$"))
    app.add_handler(CallbackQueryHandler(predict_page, pattern=r"^ppage:\d+$"))
    app.add_handler(CallbackQueryHandler(open_stepper, pattern=r"^pick:\d+$"))
    app.add_handler(CallbackQueryHandler(stepper_action, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(penalty_choice, pattern=r"^pen:\d+:[ha]$"))
    app.add_handler(CallbackQueryHandler(special_choice, pattern=r"^s:\d+$"))
    app.add_handler(CallbackQueryHandler(mypreds_nav, pattern=r"^myp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
