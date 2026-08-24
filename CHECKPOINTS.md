# Checkpoint ledger — telegram-file-bot v2 (clean redesign)

| %   | Milestone                                                                 | Link |
|-----|---------------------------------------------------------------------------|------|
| 10  | Scaffold + config.py + db.py (auto-schema, reconnect, batched writes)     | (bundled in 25%) |
| 25  | repo.py (indexed queries, TTL caches, all queue-control SQL) + utils.py   | https://www.genspark.ai/api/files/s/RBvALXtJ |
| 40  | classify.py (single source of truth) + sync.py + userbot.py backfill v2   | https://www.genspark.ai/api/files/s/QDbHSqBC |
| 55  | posting.py (spoiler-forward-and-cache) + tg.py + DM delivery              | https://www.genspark.ai/api/files/s/1QVcJfcq |
| 70  | scheduler.py + main.py + all 7 handler modules (22 files syntax-verified) | https://www.genspark.ai/api/files/s/qHsnLsHX |
| 95  | smoke_v2.py — 33/33 pass; fixed real dedupe bug in db.insert()            | https://www.genspark.ai/api/files/s/US6o9wGj |
| 100 | Final package telegram-file-bot_v2.zip                                    | (this file) |

## Why v2 exists
v13 leaked ~521M rows-read on Turso free tier: no indexes (full table scans
on every query), 3–5 SELECTs per backfill message, chatty webhook hot paths,
and a `/health` endpoint that touched the DB every 5 seconds.

## v2 read-budget engineering
- `posts(source_chat_id, source_message_id)` UNIQUE → dedupe via
  INSERT OR IGNORE, zero pre-SELECT
- `posts(kind, published_at)` composite index → queue reads touch pending
  rows only, LIMIT 1
- `posts(source_chat_id, parent_source_message_id)` index → files_of_cover
  reads only that cover's files
- In-memory 60s TTL caches: channels, settings, admin flags, db-id set
- /health: zero DB access
- /backfill_status: in-memory state only
- Backfill: RAM parent pointer + 100-row executemany batches +
  one cursor checkpoint per batch
- db.insert() returns 0 on ignored duplicate (rowcount-checked) — fixes a
  stale-lastrowid bug that could corrupt queue math

## Classifier contract (v2 spec)
- Photo / image document → cover/photo (spoiler-capable)
- Video → cover/video
- .pdf .cbz .cbr .cbt .cb7 .zip .rar .7z .epub (+ MIME matches) → file/document
- Sticker (native / webp-doc / tgs / DocumentAttributeSticker) → file/sticker
  (stored ONLY if a cover exists above it; else dropped)
- Emoji dividers, service, audio, unknown docs → skip

## Publishing
- Only kind='cover' rows can publish (SQL-enforced)
- #N assigned atomically at publish (single UPDATE + subquery)
- Spoiler forced ON: fast path = cached file_id → sendPhoto(has_spoiler=True);
  slow path = copy DB→log → forward log→log to mint bot file_id → cache it →
  sendPhoto(has_spoiler=True) → delete log copies. First publish = 4 API calls,
  every repost = 1.
- Main post: title / #N / body / postcaption extra / [📥 Get File #N]
- DM: cover first (spoiler per setting), then files with ❤️ Save / 🗑 Remove;
  stickers delivered without buttons

## Command roster (47)
User: /start /help /whoami /favs /rfavs
Setup: /addchannel /removechannel /listchannels /setlog /setcursor
       /addadmin /removeadmin /listadmins
MTProto: /tgsetapi /tglogin /tgcode /tgstatus
         /backfill_start /backfill_resume /backfill_stop /backfill_status
         /backfill_reset
Queue: /queue /queueinfo /peek [N] /whereami /find <text>
       /dripnow [N] /dripstop /setschedule /scheduleoff
       /pauseposting /resumeposting
       /skip #N|link /skip_range #A-#B /unskip #N /jumpto #N
       /queue_reset CONFIRM /repost #N /preview #N /deletepost #N|code
Content: /spoiler 1|0 /protect 1|0 /postcaption <text> /filecaption <text>
Cleanup: /massdlt <chat> <start> <end> /massdlt_status /massdlt_stop
Diag: /debug /stats

## Deploy
1. Create fresh Turso database (no tables needed — auto-created on boot)
2. Render env: BOT_TOKEN, BASE_WEBHOOK_URL, WEBHOOK_SECRET,
   TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, SUPER_ADMIN_ID, TG_API_ID,
   TG_API_HASH (optional TELETHON_SESSION_STRING)
3. Push to GitHub → Render auto-deploys → schema self-initializes
4. /addchannel <db_id> database, /addchannel <main_id> main, /setlog <log_id>
5. /tgsetapi + /tglogin + /tgcode → userbot ready
6. /backfill_start <db_id> → one sweep, done
7. /skip 721 to leapfrog already-posted content
8. /dripnow 1 → smoke test → /setschedule 07:00,19:00 15

## Smoke results
A) classifier 12/12 ✅  B) grouping 5/5 ✅  C) parsers 9/9 ✅
D) captions 2/2 ✅  E) real-SQL schema+queue 21/21 ✅ (dedupe bug found & fixed)
TOTAL: all passed ✅
