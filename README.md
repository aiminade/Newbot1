# Telegram File Share Bot

Telegram bot for file sharing with **expiry links, private links, admin panel, download analytics, and multi-file album support**.

## Features

- Upload document/file and get a share URL.
- Share URL format:
  - `https://t.me/<your_bot_username>?start=share_<code>`
- Expiry links using `/expiry <minutes>`.
- Private links to a specific Telegram user ID using `/private <user_id>`.
- Multi-file albums (send multiple documents in one media group).
- Download analytics per user (`/analytics`).
- Admin panel (`/admin`) for global bot stats.

## Setup

1. Create bot with `@BotFather` and copy token.
2. Install deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Set env:

```bash
export BOT_TOKEN="123456:ABC-..."
export BOT_USERNAME="your_bot_username_without_at"
export ADMIN_IDS="123456789,999888777"   # optional
export DB_PATH="files.db"                 # optional
```

4. Run bot:

```bash
python bot.py
```

## Commands

- `/start` – intro/help and deep-link entry (`/start share_<code>`).
- `/help` – show help.
- `/list` – list your created links.
- `/delete <code>` – delete link.
- `/private <user_id|off>` – set/unset private target for upcoming uploads.
- `/expiry <minutes|off>` – set/unset expiry for upcoming uploads.
- `/analytics` – your link download statistics.
- `/admin` – global stats (admins only, controlled by `ADMIN_IDS`).

## Notes

- Bot stores Telegram `file_id` metadata in SQLite; file bytes are not stored locally.
- Private links validate downloader user ID.
- Expired links are blocked automatically.


## Deploy on Railway (Fix Railpack build-plan error)

If Railway shows **"Error creating build plan with Railpack"**, use the included build files in this repo:

- `nixpacks.toml` (explicit build plan for Python + pip)
- `Procfile` (`worker: python bot.py`)

### Steps

1. Push this repo to GitHub.
2. In Railway, create a new project from the repo.
3. Set variables:
   - `BOT_TOKEN`
   - `BOT_USERNAME`
   - `ADMIN_IDS` (optional)
   - `DB_PATH=files.db` (optional)
4. Redeploy.

### Why this fixes it

Railpack sometimes fails auto-detection when a repo has minimal files. The explicit `nixpacks.toml` gives Railway a deterministic setup/install/start plan.


### If Railpack still fails instantly

If Railway still shows `Error creating build plan with Railpack`, force a Docker deploy:

1. Keep `Dockerfile` in repo root (already added).
2. In Railway service settings, redeploy from latest commit.
3. Railway will build via Dockerfile instead of Railpack auto-planning.

This bypasses build-plan detection issues entirely.


### Force Railway to use Docker builder (important)

If you still get the same error, Railway may still be trying Railpack auto-planning.
This repo now includes `railway.toml` to force Docker builds:

- `builder = "DOCKERFILE"`
- `dockerfilePath = "Dockerfile"`

After pushing latest commit:
1. Open Railway service.
2. Go to **Settings → Source/Build**.
3. Confirm builder is Dockerfile (not Railpack/Nixpacks).
4. Trigger **Redeploy**.

If the service was created with old settings, create a fresh Railway service from the same repo so it re-reads `railway.toml`.
