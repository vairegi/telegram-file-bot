# Telegram File Bot

An **aiogram 3** (Python) Telegram bot that mirrors a private **Database Channel**
into one or more **Main Channels**, splitting content into **cover posts** and their
attached **PDFs**. Users tap **📥 Get File** on a Main-channel cover post to receive
the cover + its PDFs in a DM, where each PDF has **❤️ Save / 🗑 Remove** buttons
(surfaced again via `/favs`).

## Stack
- Python 3.12 / 3.13 · aiogram 3.30 · aiohttp 3.14 · libsql (Turso SQLite)
- Webhook mode (Render) with an in-process IST scheduler (no pg_cron needed)

## What the bot does
1. Watches registered **database** channels (`channel_post` updates).
2. A **cover** (photo/video/text/non-PDF document) starts a group and gets the next
   auto number `#N` (line 2, right below the title). Numbering is **permanent** and
   never resets; old posts keep their number.
3. **PDFs** between one cover and the next belong to that cover (1..N PDFs supported).
   Only covers are published to Main channels — PDFs are never posted there.
4. Main-channel cover caption:
   ```
   <title>
   #N
   <rest of caption>
   <postcaption extra>      (if set with /postcaption)
   ```
   with a single **📥 Get File #N** button.
5. Tapping it delivers the cover + every attached PDF to the user's DM. PDFs get a
   file caption:
   ```
   #N · file i/total
   <original PDF caption>
   <filecaption extra>      (if set with /filecaption)
   ```
   plus **❤️ Save / 🗑 Remove**.
6. `/protect 1` forces `protect_content=True` on ALL sends (Main + DM).

## Key commands (role-scoped)
**Everyone:** `/start` `/help` `/whoami` `/favs` `/rfavs <n>` `/mystats` `/streak`
`/random` `/recent` `/leaderboard`

**Admin / super-admin** (summary):
- Channels: `/addchannel <chat_id> <role>` (database|main|log|backup|forcesub),
  `/removechannel`, `/listchannels`, `/setlog`
- Posting: `/setcaption`, `/postcaption <text>`, `/filecaption <text>`,
  `/pauseposting`, `/resumeposting`, `/repost`, `/mpost <link…>`, `/deletepost`
- Queue/drip: `/queue`, `/queueinfo`, `/setschedule 07:00,19:00 15`,
  `/scheduleoff`, `/dripnow [N]`, `/setcursor <db_chat_id> <t.me link>`
- Content: `/protect 1|0`, `/spoiler 1|0`, `/autodelete <s|off>`
- Moderation: `/ban`, `/unban`, `/banlist`, `/warn`, `/warns`, `/unwarn`, `/stats`,
  `/broadcast <text>`

## Deployment (Render free tier)

### 1. Create the database (Turso)
```bash
turso db create telegram-bot
turso db tokens create telegram-bot          # -> TURSO_AUTH_TOKEN
turso db show telegram-bot --url             # -> TURSO_DATABASE_URL (libsql://…)
```

### 2. Push to GitHub (private)
Do **NOT** commit `.env`. The repo already has a `.gitignore`.

### 3. Deploy on Render
- New → **Web Service** → connect the GitHub repo.
- Build: `pip install -r requirements.txt`
- Start: `python -m app.main`
- Health check path: `/health`
- Plan: **Free** (512 MB)

### 4. Environment variables (Render)
| Var | Example |
|-----|---------|
| `BOT_TOKEN` | `123456789:AAF…` (from @BotFather) |
| `BASE_WEBHOOK_URL` | `https://your-bot.onrender.com` |
| `WEBHOOK_SECRET` | long random string |
| `TURSO_DATABASE_URL` | `libsql://your-db-org.turso.io` |
| `TURSO_AUTH_TOKEN` | `eyJ…` |
| `START_MESSAGE_ID` | `0` (or an initial cursor value) |
| `SUPER_ADMIN_ID` | your numeric Telegram id |

### 5. Keep the free service awake
Render free services sleep after ~15 min. Ping `https://your-bot.onrender.com/health`
every ~10 min (cron-job.org or UptimeRobot).

### 6. First-run setup (bot chat)
```text
/start
/addchannel -1002298797194 database      # your Database Channel
/addchannel -1003796521529 main          # your Main Channel
/setcursor -1002298797194 https://t.me/c/2298797194/3
/setschedule 07:00,19:00 15
/dripnow                                # test one immediate post
/queueinfo
```
- The bot must be an **admin** in the Database Channel (so `copyMessage` works).
- `/setcursor <db_chat_id> <link>` resumes posting **from** the linked message. If the
  link points at a PDF, the bot rewinds to that PDF's cover.

## Local run
```bash
pip install -r requirements.txt
cp .env.example .env      # fill it in
python -m app.main        # polling mode when BASE_WEBHOOK_URL is empty
```
