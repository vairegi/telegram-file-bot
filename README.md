# Telegram File Bot (Python + aiogram + Turso)

A from-scratch, light rewrite of the Lovable AI TypeScript bot. It keeps the same
core behaviour — Database Channel → Main Channel → user DM delivery via a
"Get File" button — with **full command parity** (favorites, ratings, streaks,
referrals, notifications, force-sub, backups, audit, warnings, scheduling,
broadcasts) in a single small Python service that idles well under Render's
512 MB free tier.

---

## Architecture summary

```
Telegram ──(webhook POST)→ aiohttp server ──→ aiogram Dispatcher
                                                  │
                          ┌───────────────────────┼───────────────────────┐
                          │ channel_post handler  │ command/callback      │
                          ▼                       ▼                       ▼
                    sync engine (cursor)      handlers/*.py          scheduler loop
                          │                                                │
                          ▼                                                ▼
                     Turso (SQLite) ◄────── repo.py (data access)
```

* **Webhook mode** (not polling) so there's no long-poll connection to babysit.
* **In-process scheduler** replaces Supabase `pg_cron` (drip posting, scheduled
  posts, autodelete).
* **Turso** (hosted SQLite) replaces Supabase — free, tiny, survives Render sleeps.

---

## 1. Prerequisites (do these once)

1. **Turso database** — sign up at <https://turso.tech>, then:
   ```bash
   turso db create telegram-bot
   turso db show telegram-bot --url        # → TURSO_DATABASE_URL
   turso db tokens create telegram-bot     # → TURSO_AUTH_TOKEN
   ```
   (Tables are created automatically on first run.)
2. **BotFather token** — you've already rotated it. Copy the new token.
3. **GitHub repo** — push this folder to a new GitHub repo (Render deploys from
   GitHub). **Do NOT commit `.env`.**
4. **Bot must be admin in the Database Channel** (required for `copyMessage`).

---

## 2. Configure environment variables

Set these in **Render → your service → Environment** (or a local `.env` for dev):

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | New BotFather token |
| `BASE_WEBHOOK_URL` | ✅ | `https://<your-service>.onrender.com` (no trailing slash) |
| `WEBHOOK_PATH` | optional | defaults to `/webhook` |
| `WEBHOOK_SECRET` | ✅ | A long random string (verify Telegram secret header) |
| `TURSO_DATABASE_URL` | ✅ | `libsql://...` from Turso |
| `TURSO_AUTH_TOKEN` | ✅ | Turso auth token |
| `START_MESSAGE_ID` | ✅ | Resume cursor (see §4) |
| `SUPER_ADMIN_ID` | optional | Force a specific user → super-admin |
| `PORT` | auto | Render injects `PORT` automatically |
| `DATABASE_PATH` | dev only | Local SQLite fallback when no Turso URL |

> Render also injects `PORT`, which the app auto-detects.

---

## 3. Deploy to Render (step by step)

**Option A — Render Blueprints (fastest):**
1. Push this folder to GitHub.
2. Render Dashboard → **New → Blueprint** → select the repo.
3. Render reads `render.yaml` and creates the web service (free/512 MB).
4. Add the missing env vars (Render's blueprint marks them as `sync: false`).
5. Done — Render runs `pip install -r requirements.txt` then `python -m app.main`.

**Option B — Manual web service:**
1. Push to GitHub.
2. Render Dashboard → **New → Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python -m app.main`
   - **Health Check Path:** `/health`
   - **Instance type:** Free (512 MB)
4. Add the env vars from §2.
5. Click **Create Web Service**, then **Deploy**.

### Keep the free tier awake
Render free services spin down after ~15 min idle. Add a free external ping to
`https://<your-service>.onrender.com/health` every ~10 min using
[cron-job.org](https://cron-job.org) or [UptimeRobot](https://uptimerobot.com).

---

## 4. Resume logic — how NOT to re-post your ~1000 old files

Telegram's Bot API delivers **only forward-going `channel_post` updates** to the
bot — it cannot re-read history. So your old ~1000 posts are inherently skipped.
The **cursor** (`last_processed_message_id`) then ensures anything Telegram
re-delivers is also ignored:

- On first boot, if the cursor is empty, it is seeded from `START_MESSAGE_ID`.
- Every `channel_post` with `message_id <= cursor` is **ignored**.
- Every new post increments the cursor and is queued for the Main channel.

### Setting the starting point (~1001)
1. In the **Database Channel**, find the message id of the **last post the old
   bot already handled** (Telegram channel message ids are monotonic integers).
   Forward that message to [@userinfobot](https://t.me/userinfobot) or check its
   `t.me/c/<channel>/<msg_id>` link.
2. Set `START_MESSAGE_ID=<that id>` in the Render env vars **before** first
   deploy, then the bot resumes from the next message.
3. You can also adjust later with the `/setcursor <id>` command (admin) or check
   the current value with `/cursor`.

> ⚠️ **Rotated token note:** file_ids are scoped to the bot that fetched them.
> The new bot delivers files via `copyMessage` from the Database Channel, so it
> must be an **admin** there and the source messages must still exist.

---

## 5. Post-deploy checklist

1. `/start` the bot → you become super-admin (first user).
2. Register the **Database Channel**: `/addchannel -100xxx database`
3. Register the **Main Channel**: `/addchannel -100yyy main`
4. (Optional) register **forcesub**, **log**, **backup** channels.
5. Set cursor: `/setcursor 1001` (or set `START_MESSAGE_ID` then redeploy).
6. Post a test file in the Database Channel → it should appear in Main.
7. Tap the **Get File** button → bot DMs you the file.

---

## 6. Local development (polling mode)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN, TURSO_* (or DATABASE_PATH=bot.db)
python -m app.main     # no BASE_WEBHOOK_URL → polling mode
```

---

## 7. Command reference

**Users:** `/start`, `/help`, `/whoami`, `/favs`, `/rfavs`, `/random`, `/recent`,
`/trending`, `/similar #tag`, `/mystats`, `/streak`, `/referral`, `/notify #tag`,
`/unnotify`, `/leaderboard`

**Admins:** `/addchannel`, `/removechannel`, `/listchannels`, `/cursor`,
`/setcursor`, `/queue`, `/publish <code>`, `/ban`, `/unban`, `/warn`, `/warns`,
`/unwarn`, `/users`, `/stats`, `/broadcast`

**Super-admins:** `/addadmin`, `/removeadmin`, `/listadmins`

---

## Security note

The old repo committed `.env` with your Supabase keys. Treat those as leaked and
rotate everything you can (Supabase project, Lovable account). The new repo ships
with `.gitignore` excluding `.env`, and `render.yaml` marks secrets `sync: false`.
