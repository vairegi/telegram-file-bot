# GUIDE.md — Telegram File Bot (full verified workflow documentation)

> **Canonical architecture reference** for any future work on this bot.
> Supersedes README.md on any conflict. Covers the v3.0 dual-backend release
> (Turso / MongoDB Atlas) incl. the verified migration runbook.

## What the bot does (end-to-end)

Mirrors a private **Database Channel** (cover photo + caption, then its
attached `.pdf`/`.cbz`/etc. files, sometimes stickers) into public **Main
Channel(s)** where ONLY a spoiler-blurred cover is shown. A
**📥 Get File #N** deep-link button on each main post opens a bot DM that
delivers the cover + every file attached under that cover.

DB channel layout (verified from owner screenshots):

    msg N   : cover photo + caption (literal ** and __ appear as PLAIN TEXT —
              posted by scraper clients; stripped at caption-build time by
              utils.clean_caption / caption_plain — NEVER regress this)
    msg N+1 : my-title-...-1234567.pdf
    msg N+2 : (optional) sticker
    msg N+3 : next cover …

## Pipeline (services)

- `services/classify.py` — ONE classifier shared by live sync AND MTProto
  backfill. photo / image-doc / video → `cover`; `.pdf .cbz .cbr .cbt .cb7
  .zip .rar .7z .epub` (+ MIME) → `file/document`; sticker → `file/sticker`;
  emoji/punct-only dividers, audio, unknown → `skip` (never stored).
- `services/sync.py` — live `channel_post` ingest. Zero-DB hot path
  (cached `database_chat_ids()`); dedupe via unique index; files attach to
  the nearest cover ABOVE them (`parent_source_message_id`); files/stickers
  above the FIRST cover are dropped (owner rule); stickers under a cover ARE
  stored and DM'd (no Save button — stickers can't carry captions).
- `services/posting.py` — only `kind='cover'` rows publish. `#N` assigned
  ATOMICALLY at publish time. Spoiler forced ON via the log-channel
  round-trip trick (mint a bot-usable `file_id`, cache it back).
  DM delivery = cover + files (❤️ Save / 🗑 Remove) with fsub gate +
  optional autodelete.
- `services/backup.py` — mirrors every cover/file to backup channels via
  `copy_message`; per-channel cursors in `backup_progress`; FloodWait-aware;
  ~3 msg/s; one run per channel at a time; 60s auto-loop unless paused.
- `services/userbot.py` — Telethon MTProto backfill + `/massdlt` bulk
  delete. Chronological single-sweep; parent pointer in RAM; batched
  inserts of 100; cursor checkpoint per batch. massdlt: batches of 100,
  2s apart, 20s pause every 200 IDs.
- `services/scheduler.py` — IST-slot drip (`settings['schedule']`).
- `services/fsub.py` — join-gate; membership check FAILS OPEN on API error.
- `services/autodelete.py` — self-destruct timer for delivered DM content.
- `services/tg.py` — thin aiogram-3 wrappers (single place for API spellings).
- `services/migrate.py` — Turso→Mongo migration engine (see runbook below).

## Database layer — v3.0 dual backend

`DB_BACKEND` env var picks the backend at boot. **Default `turso`** —
deploying this code with no env change is a complete no-op.

| Backend | Connection module | Notes |
|---|---|---|
| `turso` | `app/db.py` | sync libsql, self-healing, auto schema. Frozen fallback — code kept intact. |
| `mongo` | `app/mongo_db.py` | async PyMongo `AsyncMongoClient`, self-healing, auto collections+indexes. |

`app/services/repo.py` is the ONLY data-access layer — every function is
`async` and internally dispatches on `_mongo()`. Handlers never write SQL or
Mongo queries directly.

**Mongo mapping rules (must stay consistent):**
- `posts.id` → `_id` (int from `counters['post_id']`) — numeric ids preserved
  so favorites + `save:<id>` callbacks are unchanged.
- `post_number` → `counters['post_number']` via atomic `find_one_and_update($inc)`.
  `/jumpto` and `/queue_reset` RESET this counter so next `#N` matches Turso's
  recompute-from-MAX behaviour (no gaps, no divergence).
- SQL `NULL` → field ABSENT at write time; `_row(..., fields=...)` fills
  missing columns with None on read so Mongo rows have the EXACT same dict
  shape as SQL rows (row-shape contract — regression-tested).
- Composite PKs → string `_id`: favorites = `"user:post"`,
  backup_progress = `"backup:db:msg"`.
- 60s TTL in-memory caches (settings/channels/admins) are backend-agnostic.

**Hard-won platform facts (do not relearn these):**
- `channels.chat_id` is `INTEGER PRIMARY KEY` → it IS the rowid alias in
  SQLite. ROWID-paging on it matched nothing (ids are negative) — page on
  `chat_id` from a sentinel (-10^15) instead. Same trap exists for any
  table whose PK is `INTEGER PRIMARY KEY` (admins, user_directory).
- MongoDB standalone servers REJECT `retryWrites=true` (the driver default
  we want for Atlas). Atlas is a replica set; for local testing run mongod
  with `--replSet rs0` + `rs.initiate()`.
- `AsyncMongoClient`'s connection pool binds to the event loop that created
  it — a fresh loop per call breaks it. Use one shared loop.
- `insert_batch` returns `len(rows)` on Turso (v2.9 executemany contract,
  dupes not subtracted) but the TRUE inserted count on Mongo. Only the
  backfill "dupes" stat reads it — cosmetic, documented difference.

## Migration runbook (services/migrate.py + handlers/migrate_cmds.py)

Commands (super-admin only):
- `/migrate_mongo` — full copy (resumable, idempotent), auto top-up delta,
  then auto-verify every row of every table.
- `/migrate_mongo delta` — top-up only the posts newer than the last
  migrated id (+ small-table re-sweep, idempotent).
- `/migrate_mongo_status` — progress + last verification result.

Guarantees:
- Reads Turso on its OWN libsql connection — works regardless of DB_BACKEND
  (before AND after cutover).
- Idempotent upserts (`ReplaceOne`, `ordered=False`) — safe to re-run forever.
- Resume cursor per table in Mongo `settings` (`mig_cursor:<table>`).
- After copying, counters are seeded above the Turso maxima (`$max`) — no
  id/#N collision is possible.
- Verification compares EVERY row of EVERY table field-by-field and names
  any mismatch precisely.
- While the bot runs on Turso, live posts keep flowing into Turso during
  the migration; the post-cutover `/migrate_mongo delta` catches the window.

Verified in sandbox against the production Turso (read-only): 39.5k posts +
39.5k backup_progress + 563 favorites + 497 users + 4 channels byte-identical,
counters seeded, `#N` parity, second full pass copies 0 rows (resume proven).

## Deploy / ops notes (hard-won)

- `/health` does ZERO DB access (Render probes every ~5s).
- Never `delete_webhook` on shutdown (Render overlap) — webhook survives restarts.
- Keepalive self-pings `/health` every 240s.
- Render free tier + Atlas: Network Access `0.0.0.0/0` (Render has no static
  IPs) and a STRONG database password; rotate it if it ever appeared in chat.
- Rollback from Mongo = set `DB_BACKEND=turso` on Render and redeploy.
  Turso data is a frozen snapshot until the Phase-5 cleanup (only after the
  owner is 100% confident) removes the Turso layer.

## Commands

See `/help` in-bot (menus set from `main.py` at boot). Migration commands
above; everything else unchanged from v2.9.
