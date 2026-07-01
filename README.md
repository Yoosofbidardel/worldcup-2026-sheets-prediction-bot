# World Cup 2026 Prediction Bot ⚽️🏆

A Telegram bot that runs a **score-prediction game** for a group of friends over
the whole World Cup. Players predict from tap-buttons; the bot writes only the
prediction cells into a shared Google Sheet, and **every point is computed by the
sheet's own formulas**. One codebase runs two independent leagues that differ
only by their `.env`.

> ℹ️ **`main` is just a landing page.** The full, up-to-date project lives on the
> branches below — pick your language:

## 📂 Branches

| Branch | What it is |
|---|---|
| [**`english`**](../../tree/english) | 🇬🇧 **English** version — full English UI. Start here if you want to read/use it in English. |
| [**`aria`**](../../tree/aria) | 🇮🇷 **فارسی (Persian)** version — the primary league (MAKA). README and bot text in Farsi. |
| [**`mostajeran`**](../../tree/mostajeran) | 🇮🇷 A second Persian league (Isfahani-dialect variant), same code driven by a different `.env`. |

**For English → go to the [`english`](../../tree/english) branch.**
**برای فارسی → به برنچِ [`aria`](../../tree/aria) برو (README و رباتش فارسیه).**

Each branch's own `README.md` explains exactly how the scoring formulas and the
bot's buttons work, and how to deploy it.

## What it does (in one paragraph)

Each match row holds the two teams and the actual result; each player owns a
3-column block `[predicted_home, predicted_away, points]` whose points cell is an
`=IF(...)` formula. The bot maps a Telegram user to a column and writes only their
prediction — group games, knockout ties (paired rows + a penalty-winner
sub-prediction), champion/top-scorer/best-player specials with their own
deadlines, and one-off challenges. Results and kickoff times auto-fill from
football-data.org. Everything is done with buttons (a ±/Save score stepper,
paginated lists, reminders, a live leaderboard).

## Tech

python-telegram-bot 21 · gspread + Google service account · football-data.org.
