# World Cup 2026 Prediction Bot (Google Sheets edition)

A Telegram bot that writes each player's predictions straight into your shared
Google Sheet. **All scoring stays in the sheet** — the bot only fills the two
prediction cells of each player's column block; the existing `IF(...)` formulas
calculate points and totals live.

## How it maps to your sheet

The workbook `مکا worldcup 2026.xlsx` has, on the `AFC Asian Cup Qatar 2022` tab:

| Sheet area | Meaning |
|---|---|
| Row 1, from col **E**, every 3 cols | Player names (20 players + a `ROBOT` column the bot ignores) |
| Row 2 | `=SUM(...)` total for each player |
| Rows 3–69, cols **A/B** | Home / away team |
| Rows 3–69, cols **C/D** | Actual result (admin fills these) |
| Each player = 3 cols | `[predicted_home, predicted_away, points_formula]` |
| Rows 70–72 | Special predictions: Champion (7 pts), Man of the Cup (6), Top Scorer (6) |

A match is **open for predictions until its actual result (C/D) is entered**.
Run `python3 verify_layout.py "../مکا worldcup 2026.xlsx"` any time to print how
the bot reads the file — no credentials needed.

## One-time setup

### 1. Put the workbook on Google Sheets
Upload `مکا worldcup 2026.xlsx` to Google Drive and **Open with → Google Sheets**
(or File → Import). Formulas are preserved and recalculate live. Copy the sheet
ID from the URL: `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit`.

### 2. Create a Google service account (free)
1. Go to <https://console.cloud.google.com/> → create/select a project.
2. **APIs & Services → Library** → enable **Google Sheets API** and **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
4. Open the new service account → **Keys → Add key → JSON**. Save the file as
   `service_account.json` in this folder.
5. Copy the service account's email (looks like `...@...iam.gserviceaccount.com`)
   and **Share** your Google Sheet with that email as **Editor**.

### 3. Create the Telegram bot (free)
1. In Telegram, open **@BotFather** (blue verified check) and tap **Start**.
2. Send `/newbot`, give it a **name** (e.g. *World Cup Predictions*), then a unique
   **username** ending in `bot` (e.g. `wc2026_predictions_bot`).
3. BotFather replies with a token like `123456789:AAExample-FakeToken...` — that's
   your `TELEGRAM_BOT_TOKEN`. Keep it secret (use `/revoke` if it ever leaks).

### 4. Get a football API key (free) — for kickoff deadlines
1. Register at <https://www.football-data.org/client/register> (name + email, free tier).
2. They **email you the API token** in ~1 minute (check spam) — that's your
   `FOOTBALL_API_KEY`. The free tier covers the World Cup.
3. Optional: leave it blank to disable deadlines (matches then lock only when a
   result is entered).

### 5. Configure
```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, FOOTBALL_API_KEY, GOOGLE_SHEET_ID, ADMIN_IDS
```
Get your own Telegram id by running the bot and sending `/whoami`; put it in
`ADMIN_IDS`.

### 6. Install & run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

## Linking players to names (admin)

Telegram bots **cannot list the members of a channel/group** (the Bot API
forbids it), so players are linked manually — once each:

1. Each player opens the bot and sends `/whoami`; they send you their `id`.
2. You run `/slots` to see the numbered list of names, then
   `/assign <idx> <telegram_id>` (e.g. `/assign 11 123456789` links that user to
   *Yoosof*). Slots are keyed by number, so the two players named *Sina* are
   never confused.
3. `/assignments` lists everyone linked; `/unassign <telegram_id>` removes one.

## Player commands

| Command | Action |
|---|---|
| `/predict` | Pick an open match, set the score with ➖/➕ buttons, tap **Save** |
| `/special` | Predict Champion / Man of the Cup / Top Scorer |
| `/mypredictions` | Review your picks (and actual results) |
| `/leaderboard` | Live standings (totals straight from the sheet) |
| `/matches` | Fixtures and results |
| `/whoami` | Your Telegram id and linked name |

### Score entry UI

`/predict` lists the open matches; tapping one opens an in-place **score stepper**:

```
⚽ England – Croatia
Set your score, then Save.
┌──────────────────────────────┐
│  ➖   England:  2    ➕       │
│  ➖   Croatia:  1    ➕       │
│  ✅ Save        ✖️ Cancel    │
└──────────────────────────────┘
```

The ➖/➕ taps update the same message instantly (no network), and only **Save**
writes to the sheet. If you already predicted that match, it opens pre-filled so
you can adjust it.

## Prediction deadlines (lock at kickoff)

A match stops accepting predictions **at kickoff**, not when you enter the result.
Kickoff times come from the football API (the same `FOOTBALL_API_KEY` the original
bot used). A background job fetches them every few hours and writes each match's
kickoff into column **BV** of the sheet; the bot then enforces the lock by reading
that column.

- Set `FOOTBALL_API_KEY` in `.env` to enable this. Leave it blank and matches lock
  only when you enter a result (no time deadline).
- The API only ever **fills empty** BV cells, so you can **hand-edit any BV cell to
  override** a wrong/missing time — your value is never overwritten.
- Admin command `/synckickoffs` runs the sync on demand and reports any fixtures it
  couldn't name-match (set those kickoff cells manually).
- Lock logic always compares in **UTC**; `DISPLAY_TZ` only affects how times are
  shown to players (e.g. `Asia/Tehran`).

## Admin commands

| Command | Action |
|---|---|
| `/slots` | Numbered list of the 20 names and who's linked |
| `/assign <idx> <id>` | Link a Telegram id to a slot |
| `/unassign <id>` | Remove a link |
| `/assignments` | List all links |
| `/synckickoffs` | Fetch kickoff times now; report unmatched fixtures |

## Notes
- The bot **never** writes a formula/points column, so your scoring can't be
  corrupted. It writes only `predicted_home` / `predicted_away` (or one text cell
  for specials), plus the kickoff column BV.
- Predictions can be changed until kickoff (or until a result is entered).
- New fixtures appear automatically once you fill columns A/B for more rows.
- `assignments.json`, `service_account.json`, and `.env` are git-ignored.
