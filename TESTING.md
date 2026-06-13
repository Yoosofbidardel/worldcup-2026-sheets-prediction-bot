# Testing the bot locally (step by step)

Follow this top to bottom the first time. Test against a **copy** of the sheet so
you can't disturb the real game. All terminal commands assume you start here:

```bash
cd /Users/yoosefbidardel/Downloads/worldcup_bot/wc2026_sheets_bot
```

---

## Phase 0 — Offline layout check (no accounts needed)

Proves the bot reads your real spreadsheet layout correctly.

```bash
python3 verify_layout.py "../مکا worldcup 2026.xlsx"
```

✅ Expect: `20 assignable slots`, the fixture list, and `formula=OK` on the three
specials. If this looks right, the cell-mapping is right.

---

## Phase 1 — Make a TEST Google Sheet

1. Upload `مکا worldcup 2026.xlsx` to Google Drive → right-click → **Open with →
   Google Sheets**.
2. **File → Make a copy**, name it e.g. *WC2026 TEST*. Use this copy for testing.
3. Copy the sheet ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`<THIS_IS_THE_ID>`**`/edit`

---

## Phase 2 — Google service account + share the sheet

1. <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **Google Sheets API** *and* **Google Drive API**.
3. **Credentials → Create credentials → Service account** → create.
4. Open it → **Keys → Add key → JSON** → download. Save it as:
   `service_account.json` in this folder.
5. Copy the service-account email (ends in `…iam.gserviceaccount.com`), then open
   your **TEST sheet → Share → add that email as Editor**.

> ⚠️ Must be **Editor**, not Viewer — otherwise saving predictions silently fails.

---

## Phase 3 — Get the two tokens

You need two free credentials: a **Telegram bot token** and a **football API key**.

### 3a. Telegram bot token (from BotFather)

1. Open Telegram and search for **@BotFather** (it has a blue verified check). Open
   the chat and tap **Start**.
2. Send `/newbot`.
3. BotFather asks for a **name** (what people see, e.g. *World Cup Predictions*).
4. Then it asks for a **username** — it must be unique and end in `bot`
   (e.g. `wc2026_predictions_bot`).
5. BotFather replies with a line like:
   `Use this token to access the HTTP API:` followed by
   `123456789:AAExample-FakeToken...` — **that whole string is your `TELEGRAM_BOT_TOKEN`.** Copy it.

> Keep the token secret — anyone with it can control the bot. If it leaks, send
> `/revoke` to BotFather to get a new one.

### 3b. Football API key (from football-data.org)

Used for kickoff times so predictions lock at kickoff.

1. Go to <https://www.football-data.org/client/register>.
2. Enter your name + email, accept the terms, and submit (the **free** tier is fine —
   it already includes the World Cup).
3. They **email you the API token** within a minute — check inbox/spam.
4. Copy that token — **it's your `FOOTBALL_API_KEY`.**

> Free tier allows 10 requests/min, which is plenty here (the bot only syncs a few
> times a day). Leave `FOOTBALL_API_KEY` blank if you don't want deadlines — matches
> then lock only when you enter the result.

---

## Phase 4 — Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `GOOGLE_SHEET_ID` | the TEST sheet ID from Phase 1 |
| `WORKSHEET_NAME` | leave as `AFC Asian Cup Qatar 2022` (tab name is preserved on upload) |
| `GOOGLE_CREDENTIALS_FILE` | leave as `service_account.json` |
| `ADMIN_IDS` | leave blank for now (filled in Phase 6) |
| `FOOTBALL_API_KEY` | your football-data.org key (same one the old bot used). Blank = no kickoff deadline |
| `DISPLAY_TZ` | e.g. `Asia/Tehran` to show kickoff times in your timezone |

---

## Phase 5 — Install dependencies and launch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

- ✅ Expect the log line: `Bot started. Polling…`
- ❌ If it exits immediately: wrong `service_account.json` path, wrong sheet ID, or
  the sheet isn't shared with the service-account email. Fix, then rerun.

Leave this running. Open a **second terminal** for later commands, or just use
Ctrl-C to stop when you need to edit `.env`.

---

## Phase 6 — Make yourself the admin

1. In Telegram, open your bot → send `/whoami`. It replies with
   `Your Telegram id: 123456789`.
2. Stop the bot (Ctrl-C), set `ADMIN_IDS=123456789` in `.env`, then start again:
   ```bash
   python3 main.py
   ```
3. Send `/whoami` again → it should now show `Role: admin`.

---

## Phase 7 — Test the player flow

**Link yourself to a name (admin commands):**

```
/slots                 → numbered list of the 20 names
/assign 11 <your_id>   → 11 = Yoosof; expect "✅ Linked id … → Yoosof"
/assignments           → shows the link
```

**Predict a match with the stepper:**

```
/predict               → tap a match, e.g. England – Croatia
```
- Tap ➕ / ➖ — the message updates instantly.
- Tap **✅ Save** → confirmation.
- **Check the TEST sheet**: that match's row, your two prediction columns
  (AI/AJ for Yoosof) show the numbers; the points column (AK) computes itself.
- `/predict` the same match again → the stepper opens **pre-filled**. Edit, Save,
  and watch the sheet update.

**Special prediction:**

```
/special               → tap "Top Scorer" → type a name
```
Check it lands in your column at row 72.

**Read-backs:** `/mypredictions`, `/matches`, `/leaderboard` — confirm they match
the sheet.

---

## Phase 8 — Confirm scoring actually fires

1. In the TEST sheet, type the **actual result** of a match you predicted into
   columns **C** and **D** of that row.
2. The player's points cell for that row fills in (10 / 7 / 5 / 1 / 0) and their
   **row-2 total** rises — all by the sheet's own formulas.
3. In Telegram: `/leaderboard` reflects the new total; `/matches` shows the result
   instead of *(open)*; `/predict` no longer lists that match (it's closed once a
   result exists).

---

## Phase 9 — Test kickoff deadlines (needs `FOOTBALL_API_KEY`)

1. With the bot running, send `/synckickoffs` (admin). Expect a reply like
   *"Fetched N fixtures, wrote M new kickoff times"* — and a list of any fixtures
   it couldn't name-match.
2. Open the TEST sheet → column **BV** now holds kickoff times (UTC) next to each
   matched fixture. The header `Kickoff (UTC)` appears in BV1.
3. `/predict` now shows each match with a ⏰ kickoff time; `/matches` shows ⏰ for
   open games and 🔒 _locked_ for ones that have kicked off.
4. **Simulate a deadline:** in the sheet, set one match's BV cell to a time in the
   **past** (e.g. yesterday). Then in Telegram `/predict` — that match should no
   longer be listed, and if you were mid-stepper, **Save** is refused with
   "that match just closed". This proves the lock works.
5. **Override test:** type your own time into a BV cell that the API left blank;
   re-run `/synckickoffs` and confirm your value is **not** overwritten.

> If `/synckickoffs` reports many unmatched fixtures, the API's team spellings
> differ from your sheet. Either set those BV cells manually, or tell me the names
> and I'll add them to the alias list in `api_client.py`.

## Stopping / restarting during testing

```bash
# stop:    Ctrl-C in the terminal running the bot
# restart: python3 main.py   (with the venv active: source .venv/bin/activate)
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot exits on start | Wrong `service_account.json` path / sheet not shared with the service email / wrong sheet ID |
| `409 Conflict` in logs | The bot is already running somewhere else (another terminal, or the server). Run it in only one place. |
| `/predict` says "not linked" | You didn't `/assign` your id |
| Save seems to do nothing in the sheet | Service account shared as **Viewer**, not **Editor** |
| Points stay 0 after entering a result | Result typed somewhere other than columns **C/D**, or the cell has a stray space |
| `/slots` is empty | `WORKSHEET_NAME` doesn't match the tab name in your copy |
| Match has no ⏰ / never locks | `FOOTBALL_API_KEY` blank, or API couldn't name-match it — run `/synckickoffs` and fill column **BV** manually |
| `/synckickoffs` does nothing | `FOOTBALL_API_KEY` missing/invalid, or the API has no fixtures for `COMPETITION_CODE=WC` yet |

---

## Files created while testing (git-ignored, safe to delete to reset)

- `.env` — your tokens/IDs
- `service_account.json` — Google key
- `assignments.json` — the telegram-id → name mapping (created on first `/assign`)
- `.venv/` — the Python virtual environment
