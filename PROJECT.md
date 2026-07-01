# Project Handoff: World Cup 2026 Prediction Bot

## What it is
A **Telegram bot for a World Cup 2026 score-prediction game** among friends. Players
predict match scores via the bot; the bot writes ONLY the two prediction cells into a
shared **Google Sheet**. **All scoring is done by the sheet's own formulas** (the bot
never writes formula columns) — totals recalc live and the bot just reads them.
Telegram bots can't list group members, so each player is manually linked (`/assign`)
to a sheet column.

## Two live instances (same codebase, two Telegram bots + two sheets)
| | MAKA (مکا) | Katan Mempof (کتان ممپف) |
|---|---|---|
| Git branch | `aria` | `mostajeran` |
| Server dir | `/opt/wc2026` | `/opt/ketan` |
| systemd service | `wc2026` | `ketan` |
| Telegram bot | MAKA's | @katanmempof_bot |
| Sheet ID | `16NshT8ko9zdyeGeRXbABnHdhROHau2c6_5sOvSBOfNA` | `1mi1x9Bwq6zALFziPG7gjbuzdqYOXRFK81rJreBNyLw4` |
| Worksheet tab | `AFC Asian Cup Qatar 2022` (legacy name) | `FIFA World Cup 2026` |
| Text style | clean Farsi, dates Shamsi+Gregorian, Europe/Amsterdam | **Isfahani dialect**, Shamsi-only, Asia/Tehran |
| Players | ~20 | 33 |
| `.env` extras | `MATCH_LAST_ROW=74`, `SPECIAL_BASE_ROW=107` | `MATCH_LAST_ROW=74`, `SPECIAL_BASE_ROW=107`, `KICKOFF_COL=114` |

The two branches differ ONLY in user-facing TEXT (dialect) and a few display choices.
`config.py`, `sheet.py`, `store.py`, `main.py`, `teams_fa.py` are kept IDENTICAL on both
branches — when changing them, edit one branch then `git checkout <otherbranch> -- <file>`.
`bot.py` diverges (dialect strings) so it's edited per-branch with the same logic.

## Infra / deploy
- **Server:** Hetzner Ubuntu 24.04 VPS, IP `167.235.240.242`, SSH as root with key
  `~/.ssh/wc2026_deploy` (no passphrase; same key authorizes GitHub-pull and the
  operator's login).
- **Repo (public):** github.com/Yoosofbidardel/worldcup-2026-sheets-prediction-bot —
  branches `aria` and `mostajeran`.
- **Auto-deploy:** each server dir is a git clone; a systemd timer
  (`wc2026-deploy.timer` / `ketan-deploy.timer`) runs every 2 min, does `git fetch` +
  `git reset --hard origin/<branch>` + `pip install -r requirements.txt` +
  `systemctl restart` only when the branch HEAD changed. So: **push to a branch → it
  self-deploys within ~2 min.** `.env`/`service_account.json`/state files are gitignored
  so the reset preserves them. (Instant deploy: run `/usr/local/bin/wc2026-deploy.sh` or
  `ketan-deploy.sh` over SSH; an `.env`-only change needs a manual `systemctl restart`.)
- **Secrets:** `.env` (bot token, sheet id, admin ids, football api key, tz, layout
  overrides) and `service_account.json` (same Google service account shared as Editor on
  both sheets, email `worldcup-service-acc@second-hexagon-480010-u3.iam.gserviceaccount.com`)
  live only on the server + dev machine, never in git.
- **Dev machine:** Windows, repo at `d:\code\worldcup\wc2026_sheets_bot\wc2026_sheets_bot`,
  venv at `.venv`. gspread works locally with the local `service_account.json`, so sheet
  edits/inspection are done with one-off python scripts.

## Code layout
- `main.py` — entry; builds the PTB Application, instantiates `SheetClient`, registers
  handlers, schedules jobs (kickoff sync, results sync, reminders, group-announce +
  exact-kickoff timers, daily 9am fixtures).
- `bot.py` — all Telegram handlers + jobs + Farsi/RTL text. Big file.
- `sheet.py` — `SheetClient` (gspread): `slots`, `matches`, `open_matches`, `specials`,
  `standings`/`leaderboard`, `user_predictions`, `user_points`, `match_all_predictions`,
  `set_match_prediction`, `set_special_prediction`, `sync_kickoffs`, `sync_results`,
  `predicted_match_rows`.
- `store.py` — JSON files: `assignments.json` (uid→{name,col}), `reminders_sent.json`,
  `seen_intro.json`, `announce.json` (group_id, thread_id, snapshot, announced, started,
  auto_leaderboard), `predictions_log.jsonl` (append-only audit log of every saved
  prediction).
- `config.py` — all settings, several env-overridable: `MATCH_LAST_ROW` (default 69),
  `SPECIAL_BASE_ROW` (default 70), `KICKOFF_COL` (default 74), `DISPLAY_TZ`,
  `PREDICTIONS_ALWAYS_OPEN` (default false → matches lock at kickoff), football API, etc.
- `api_client.py` — football-data.org client (competition code WC): `fetch_kickoffs`,
  `fetch_results`, plus `canon()` name normalization + alias map.
- `teams_fa.py` — English→Farsi team names, keyed by `canon()` token; `fa(name)` falls
  back to the original. Long names deliberately shortened (e.g. Bosnia→بوسنی) for RTL.
- Tech stack: `python-telegram-bot[job-queue]==21.*`, `gspread==6.*`, `google-auth`,
  `python-dotenv`, `requests`, `jdatetime` (Shamsi dates).

## Sheet layout (1-based)
- Row 1: participant names starting at **col E (5)**, every **3 columns**
  (`[predicted_home, predicted_away, points_formula]` per player). A `ROBOT` column and
  `Kickoff (UTC)` headers are in `EXCLUDED_NAMES`.
- Row 2: each player's `=SUM(...)` total (the leaderboard reads these, at the player's
  FIRST/home column).
- Cols A/B = home/away team; C/D = actual result (admin or API fills).
- Match rows: `MATCH_FIRST_ROW=3` .. `MATCH_LAST_ROW` (72 group matches → rows 3-74 on
  both sheets now).
- Below group: knockout rows (different scoring), then special-prediction rows at
  `SPECIAL_BASE_ROW`..+2 (Champion 7 / Man of the Cup 6 / Top Scorer 6), then a
  "Challenges" section. (Katan also has a one-off "کوییز" row at 110, answer in C, 3 pts.)
- Column `KICKOFF_COL` (BV=74 for MAKA, DJ=114 for Katan because 33 players push columns
  further right) stores each match's kickoff ISO time; auto-filled from the API into
  EMPTY cells only (hand-edits respected). **KICKOFF_COL must sit past ALL player columns
  + ROBOT, or it collides with a player.**

## CRITICAL GOTCHAS
1. **Scoring formulas return 0 only when checked cells contain a literal SPACE `" "`, not
   when truly empty.** The formula is `=IF(OR(E=" ",F=" ",$C=" ",$D=" "),0, …)`. If you
   CLEAR a knockout/special row's result cell to truly-empty, `empty=empty` → it awards
   FULL points → every total inflates (once hit ~300 on Katan). Fix: put `" "` in column
   C of empty knockout rows and in C of the special rows. MAKA's empty rows all have `" "`
   in C.
2. **Assignments are keyed by absolute column number.** If someone inserts/deletes a sheet
   column, names shift but stored cols don't → predictions land in the wrong person's
   column. Happened once (Katan, off-by-3). Don't insert/delete columns; edit names in
   place. (A name-based self-heal is a known TODO.)
3. **RTL/bidi:** Farsi is RTL; Latin names/numbers/dates must be wrapped in Unicode
   isolates (`_iso` uses U+2068/2069) and each line forced RTL (`_rtl` prefixes U+200F).
   Scores must be SPACED (`0 - 2`) not contiguous, or bidi reverses them next to Farsi
   team names. `_pred_line` isolates name + the whole "team score team" unit.
4. **One process per bot token** — a second instance on the same token gives Telegram
   `409 Conflict`.
5. Leftover backup tabs exist in the Katan sheet (`Backup-*`, `Backup2-*`, `Backup3-*`,
   `BROKEN-by-claude-*`) — safe to delete.

## Features (current)
- Predict via persistent reply-keyboard buttons; inline ±/Save score stepper; predictions
  editable until kickoff (matches lock at kickoff via the kickoff column).
- Special predictions (champion/top-scorer/man-of-cup) as free text.
- `/mypredictions` shows each pick + actual result + points earned.
- Reminders: DM players 24h/3h/1h before a match they haven't predicted.
- Auto kickoff-time sync (every 6h) and auto results sync from the API (the group-announce
  job also pulls results before posting).
- Group announcements (after `/setgroup` run by admin inside the target group — and inside
  a forum TOPIC to lock posts to that topic): at each kickoff it posts everyone's
  predictions, and after each finished match it posts the leaderboard. Exact-kickoff
  timers + a 15-min fallback poll.
- Daily 09:00 Asia/Tehran post of the next-24h fixtures to the group.
- Admin controls (reply-keyboard + commands): `/sendtable`, `/sendpreds`, `/autoresults`
  (toggle auto leaderboard), `/predlog` (download full prediction audit log + last 20),
  `/broadcast`, `/slots`, `/assign`, `/unassign`, `/assignments`, `/synckickoffs`,
  `/syncresults`, `/setgroup`.

## How to work on it
- Make code changes on a branch, push → auto-deploys in ~2 min. For shared files, mirror
  to the other branch (`git checkout <branch> -- file`); for `bot.py` apply the same logic
  per branch (dialect differs).
- Sheet structure changes are done with one-off local gspread scripts; **always duplicate
  the tab as a backup first** and verify with read-only inspection before writing.
- Per-sheet differences (row counts, kickoff col, timezone) go in each `.env`, NOT
  hardcoded in `config.py`.
