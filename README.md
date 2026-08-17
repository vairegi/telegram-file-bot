# Telegram File Bot

An **aiogram 3** (Python) Telegram bot that mirrors a private **Database Channel**
into one or more **Main Channels**, splitting content into **cover posts** and their
attached **PDFs**. Users tap **📥 Get File** on a Main-channel cover post to receive
the cover + its PDFs in a DM, where each PDF has **❤️ Save / 🗑 Remove** buttons.

## ⚠️ Critical: historical posts must be back-filled

The Telegram **Bot API cannot read old channel messages**. It only sees `channel_post`
updates for messages posted **after** the bot became admin. This means:

- `/setcursor <chan> <link>` only tells the bot *from which future message-id to
  start capturing*. It does NOT retroactively pull 1000 old posts.
- To ingest old posts, use **`/import <from> <to>`** in the bot's DM. The bot forwards
  each old message to your DM briefly, classifies it (cover vs PDF), stores it, then
  deletes the forwarded copy from your DM.
- For very large history (>500), split into ranges: `/import 1 500`, `/import 501 1000`, etc.
- Bot must be **admin** in the Database Channel for forward/copy to work.

## Stack
- Python 3.12 / 3.13 · aiogram 3.30 · aiohttp 3.14 · libsql (Turso SQLite)
- Webhook mode (Render) with an in-process IST scheduler

## What the bot does
1. Watches registered **database** channels.
2. **Cover** (photo/video/text/non-PDF doc) starts a group and gets `#N` (line 2 below title). Numbering is **permanent**.
3. **PDFs** between one cover and the next belong to that cover.
4. Main-channel cover caption:
   ```
   <title>
   #N
   <rest>
   <postcaption extra>   (if /postcaption is set)
   ```
   with a single **📥 Get File #N** button.
5. DM delivery = cover + each PDF (PDFs get ❤️ Save / 🗑 Remove + `filecaption` extra).
6. `/protect 1` forces `protect_content` on ALL sends.

## Commands

**Users:** `/start /help /whoami /favs /rfavs <n> /mystats /streak /random /recent /leaderboard`

**Admin:**
- Channels: `/addchannel <chat_id> <role>`, `/removechannel`, `/listchannels`, `/setlog`, **`/import <from> <to> [chan]`**, `/importone <link>`
- Posting: `/setcaption`, `/postcaption <text>`, `/filecaption <text>`, `/pauseposting`, `/resumeposting`, `/repost`, `/mpost <link…>`, `/deletepost`
- Queue/drip: `/queue`, `/queueinfo`, `/setschedule 07:00,19:00 15`, `/scheduleoff`, `/dripnow [N]`, `/setcursor <chan_id> <t.me link>`
- Content: `/protect 1|0`, `/spoiler 1|0`, `/autodelete <s|off>`
- Moderation: `/ban`, `/unban`, `/banlist`, `/stats`, `/broadcast`, `/warn`, `/warns`, `/unwarn`

## Deployment (Render free tier)

### 1. Create Turso database
```bash
turso db create telegram-bot
turso db tokens create telegram-bot        # -> TURSO_AUTH_TOKEN
turso db show telegram-bot --url           # -> TURSO_DATABASE_URL (libsql://…)
```

### 2. Push to GitHub (private)
Do NOT commit `.env`.

### 3. Deploy on Render
- New → **Web Service** → connect the repo
- Build: `pip install -r requirements.txt`
- Start: `python -m app.main`
- Health: `/health`
- Plan: **Free**

### 4. Env vars
`BOT_TOKEN`, `BASE_WEBHOOK_URL` (https://your-bot.onrender.com), `WEBHOOK_SECRET`,
`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `START_MESSAGE_ID` (0), `SUPER_ADMIN_ID`.

### 5. Keep alive
Ping `/health` every ~10 min (cron-job.org / UptimeRobot).

### 6. First-run workflow (in bot DM)
```
/start                                              # become super-admin
/addchannel -1002298797194 database
/addchannel -1003796521529 main
/import 1 200                                       # backfill FIRST 200 old posts
/queueinfo                                          # should now show pending covers
/setschedule 07:00,19:00 15                         # IST slots
/dripnow                                            # test one immediate post
```

Then, for ongoing new posts sent to the DB channel, the webhook auto-captures them.
Use `/setcursor <db_chat_id> <link>` if you want to skip forward.

## Local run
```bash
pip install -r requirements.txt
cp .env.example .env      # fill it in
python -m app.main        # polling if BASE_WEBHOOK_URL empty
```
