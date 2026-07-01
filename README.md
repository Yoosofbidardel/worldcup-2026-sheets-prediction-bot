# World Cup 2026 Prediction Bot ⚽️🏆

A Telegram bot that runs a **score-prediction game** for a group of friends over
the whole World Cup. Players predict match scores from friendly tap-buttons in
Telegram; the bot writes **only the two prediction cells** into a shared Google
Sheet, and **every point is computed by the sheet's own formulas**. The bot never
writes a score — so the scoring logic is fully transparent, lives in one place,
and recalculates live for everyone.

The same codebase runs **two independent leagues** at once (two Telegram bots,
two Google Sheets) that differ only by their `.env` — same code, different data.

---

## Why it's built this way

- **The Google Sheet is the source of truth and the scoring engine.** The bot is
  a thin, stateless front-end: it maps a Telegram user to a sheet column and
  writes their prediction. All totals, tie-breaks, bonuses and knockout rules are
  `=IF(...)` formulas in the sheet. Anyone can open the sheet and audit exactly
  how a score was produced.
- **Tap, don't type.** Everything is done with buttons — a persistent bottom
  menu plus inline keyboards (a ±/Save score stepper, paginated match lists,
  penalty-winner buttons). Users never have to remember a command.
- **Config-driven, single codebase.** Sheet layout (row ranges, columns,
  deadlines, bonus rows) is read from environment variables, so one code path
  serves differently-shaped sheets.

---

## How scoring works (all in the sheet)

Each match row stores the two teams (cols `A`/`B`) and the actual result
(cols `C`/`D`). Each participant owns a 3-column block: `[predicted_home,
predicted_away, points]`. The **points** cell is a formula comparing the player's
prediction to the actual result. Row 2 holds each player's `=SUM(...)` total,
which drives the leaderboard.

### Group stage (per match)

| Outcome | Points |
|---|---|
| Exact score | **10** |
| Correct draw, close (within 1 goal of the real draw) | 7.5 |
| Correct draw, further off | 5.5 |
| Correct winner | 7.5 minus a goal-difference-error penalty |
| One team's goals correct | 1 |
| Otherwise | 0 |

A cell counts as "empty" (→ 0) only when it contains a literal space `" "`, not
when it is truly blank — a deliberate convention the formulas rely on.

### Knockout stage (paired rows + penalties)

Every knockout match uses **two rows**: the *score row* (regulation result) and a
*penalty row* below it that encodes who advanced (`1-0` = home, `0-1` = away).
Predicting a **draw** in the bot triggers a follow-up: two buttons asking which
team you think goes through on penalties.

Scoring (before the per-round multiplier):

**If the match went to penalties**
- Exact draw score **and** correct qualifier → 10
- Inexact draw, correct qualifier → `7 + 1.5 / |predicted − actual|` (8.5 → 7)
- Predicted a winner whose team then advanced on penalties → configurable (e.g. 4)
- Exact draw score, wrong qualifier → 8.5
- Inexact draw, wrong qualifier → `5.5 + 1.5 / |predicted − actual|` (7 → 5.5)
- One team's goals correct → 1

**If it was decided in regulation** — the group-stage-style winner/score scoring,
plus a small reward for predicting a draw but naming the eventual winner.

Each round then multiplies the result: **Round-of-32 ×1.2, Round-of-16 ×1.4,
Quarter ×1.6, Semi ×1.8, Third-place ×1.9, Final ×2.0** (per-league configurable).

### Special predictions

Champion, best player and top scorer are text predictions with their own points
and **time-based deadlines**:
- Best player & top scorer **hard-close** at a deadline.
- The **champion** is a two-cell prediction: lock it in early and leave it
  unchanged for a **+bonus** if correct; you may still change it later (up to the
  knockout stage) but forfeit the bonus (the pre-deadline pick lives in cell 1,
  later changes in cell 2).
- One-off **challenges** (e.g. "who will make the boldest predictions") can be
  added per-league via an env variable, each with its own label, points and
  deadline.

Results are auto-filled from the [football-data.org](https://football-data.org)
API after each match (kickoff times too, so predictions lock at kickoff). For a
penalty shootout the bot writes the **regulation draw** to the score row and the
**qualifier** to the penalty row — never the shootout aggregate.

---

## The bot, button by button

Tap **/start** once to get the persistent menu.

**Everyone**
- **🎯 Predict a match** — opens a paginated list of open matches (4 per page,
  Next/Prev). Tapping a match opens the **score stepper**: `➖ / ➕` under each
  team and **✅ Save** / **✖️ Cancel**. The running score lives in the button
  data, so `+/-` taps never hit the network — only Save writes to the sheet.
  For a knockout match you predict as a draw, Save is followed by two buttons to
  pick the **penalty winner**.
- **🏆 Special prediction** — lists the currently-open specials (champion, best
  player, top scorer, any active challenge), each showing **its own deadline**.
  Pick one and send your answer as text.
- **📋 My predictions** — your picks with the real result and points earned. Shows
  the **last 10** matches by default, with a **📄 Show all** button that opens a
  paginated view (10/page, Prev/Next) and a **🔙 Back** button. Knockout draws
  also show your penalty pick ("Argentina 1 - 1 Brazil — Argentina on penalties").
- **📅 Fixtures** — the schedule with kickoff times (Shamsi + Gregorian) and
  results.
- **🆔 Who am I?** — shows your Telegram id so the admin can link you.
- **❓ Help** — the first-time guide.

**Admin-only** (extra rows appear for admins)
- **📊 Leaderboard** — private standings (others' scores are hidden from players).
- **📢 Broadcast** — send a message to every linked player's private chat.
- **📨 Post to group** — post a message into the registered group as the bot.
- **📤 Send table / 📤 Send predictions** — push the leaderboard or a match's
  predictions to wherever you run it.
- **⚙️ Auto results** — toggle the automatic post-match leaderboard.
- **📜 Prediction log** — download the full append-only audit log of every pick.
- **🏅 Yesterday's top gainers** — post (and re-post on demand) the daily
  "who scored the most yesterday" call-out that tags the top N and asks them to
  analyse today's games.
- Commands: `/slots`, `/assign <idx> <id>`, `/unassign <id>`, `/assignments`,
  `/setgroup`, `/synckickoffs`, `/syncresults`, `/sendgroup`, `/topgainers`.

### Automatic jobs

- Kickoff-time sync and result sync from the API (results auto-fill the sheet).
- **Reminders** to each player who hasn't predicted a match yet — 24h / 3h / 1h
  before kickoff — plus a special-deadline reminder (24h / 1h). All predictions
  are read in **one bulk call** to stay under the Sheets read-quota.
- Group posts: everyone's predictions when a match kicks off, and the leaderboard
  when it finishes (optionally into a specific forum topic).
- A daily 9am fixtures post + the top-gainers call-out.

---

## Architecture

| File | Role |
|---|---|
| `main.py` | Builds the Telegram app, registers handlers, schedules the jobs. |
| `bot.py` | All handlers, buttons, jobs and the (RTL-aware) message text. |
| `sheet.py` | `SheetClient` (gspread): matches, specials, predictions, results/kickoff sync, standings. Wraps calls with **retry-on-429**. |
| `store.py` | Local JSON: user→column assignments, reminder de-dup, the group/announce state, and an append-only prediction log. |
| `config.py` | All settings, most env-overridable (row ranges, kickoff column, deadlines, bonus rows, extra specials). |
| `api_client.py` | football-data.org client (kickoffs + results, incl. penalty shootouts) with team-name normalisation. |
| `teams_fa.py` | Team-name display map. |

**Two leagues, one codebase.** Two git branches deploy to two service instances;
`config.py` is identical and every per-league difference (sheet id, token,
timezone, row layout, bonus rows, extra specials) lives in each instance's
`.env`. Secrets (`.env`, `service_account.json`) never enter git.

Right-to-left text is kept stable next to Latin names/numbers with Unicode
isolates, and dates are shown in both the Jalali (Shamsi) and Gregorian calendars.

---

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/botfather) and get its token.
2. Create a Google Cloud service account, download `service_account.json`, and
   share your scoring Google Sheet with the service-account email as **Editor**.
3. Copy `.env.example` to `.env` and fill in: `TELEGRAM_BOT_TOKEN`,
   `GOOGLE_SHEET_ID`, `ADMIN_IDS`, `FOOTBALL_API_KEY`, and any layout overrides.
4. Install and run:
   ```bash
   pip install -r requirements.txt
   python main.py
   ```
5. In your group, run `/setgroup` (inside the target forum topic if you use one),
   then `/slots` and `/assign <idx> <telegram_id>` to link people to sheet columns.

Only **one** process may run per bot token (a second one causes a 409 conflict).

---

## Tech

python-telegram-bot 21 (long polling, JobQueue, inline + reply keyboards) ·
gspread + Google service account · football-data.org · jdatetime (Jalali dates).
