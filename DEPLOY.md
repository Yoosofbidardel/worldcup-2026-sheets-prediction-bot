# Deploy & CI/CD

Push to `aria` → GitHub Actions copies the code to your server and restarts the
bot. Secrets (`.env`, `service_account.json`) live **only** on the server and
your PC — never in git.

```
your PC ── git push (aria) ──► GitHub ── Actions (rsync + restart) ──► server runs the bot 24/7
```

## One-time setup (≈10 minutes)

### 1. Get a server
Any cheap Ubuntu VPS works (Hetzner / DigitalOcean / Vultr, ~$4–5/mo). Note its
IP. SSH in as `root`.

### 2. Make an SSH deploy key
This key lets GitHub log into your server. Generate it **on your PC**:

```bash
ssh-keygen -t ed25519 -f wc2026_deploy -N "" -C "github-deploy"
# creates two files: wc2026_deploy (PRIVATE) and wc2026_deploy.pub (public)
```

- Put the **public** half on the server:
  ```bash
  ssh-copy-id -i wc2026_deploy.pub root@<SERVER_IP>
  # or paste wc2026_deploy.pub into the server's ~/.ssh/authorized_keys
  ```
- Keep the **private** half (`wc2026_deploy`) for step 4. Never commit it.

### 3. Bootstrap the server (run once, as root)
```bash
curl -fsSL https://raw.githubusercontent.com/Yoosofbidardel/worldcup-2026-sheets-prediction-bot/aria/deploy/setup-server.sh | bash
```
Then add your secrets on the server:
```bash
nano /opt/wc2026/.env                  # TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_ID, ADMIN_IDS, …
nano /opt/wc2026/service_account.json  # paste the Google service-account JSON
```

### 4. Add the GitHub repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `SSH_HOST` | your server IP |
| `SSH_USER` | `root` |
| `SSH_KEY` | contents of the **private** `wc2026_deploy` file (whole thing) |
| `DEPLOY_DIR` | `/opt/wc2026` |
| `SERVICE_NAME` | `wc2026` |
| `SSH_PORT` | `22` *(optional; omit if 22)* |

> These are encrypted and write-only — masked as `***` in all logs, unreadable
> after saving, even by you.

### 5. Deploy
Push to `aria` (or **Actions → Deploy to server → Run workflow**). It will:
rsync the code → install deps → restart the service. Verify on the server:
```bash
systemctl status wc2026
journalctl -u wc2026 -f
```

## Everyday use
Just push. That's it.
```bash
git push        # → auto-deploys to the server
```

## Two leagues on one server
Bootstrap a second instance with its own dir/service, then give it its own
`.env` (different bot token + sheet):
```bash
DEPLOY_DIR=/opt/wc2026-leagueB SERVICE_NAME=wc2026-b bash setup-server.sh
nano /opt/wc2026-leagueB/.env
```
To auto-deploy both, duplicate the deploy steps in
`.github/workflows/deploy.yml` for the second dir/service (or add a matrix).

## Notes & gotchas
- **Only one process per bot token** — two instances = `409 Conflict`. Stop any
  local `python main.py` before/while the server runs.
- The deploy uses `rsync --delete` but **excludes** `.env`,
  `service_account.json`, `assignments.json`, `reminders_sent.json`, `.venv/` —
  so your secrets and saved state on the server are never overwritten or deleted.
- `sudo systemctl restart` over SSH assumes `SSH_USER` is `root` (or has
  passwordless sudo).
- The bot **must stay running** for the 24h/3h/1h prediction reminders to fire.
