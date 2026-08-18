# Checkpoint ledger — v15

| %   | Milestone                                                                     | Link |
|-----|-------------------------------------------------------------------------------|------|
| 5   | Fresh repo clone + baseline                                                   | (n/a — git clone) |
| 15  | `repo.reclassify_stored_row()` + `repo.row_mime()` MIME fallback readers      | (bundled) |
| 30  | `userbot.probe_source_message()` MTProto truth-probe                          | (bundled) |
| 45  | `posting._probe_and_reclassify()` gates publish path; sticker/file auto-drop  | (bundled) |
| 60  | Sync classifier: sticker→skip (native + webp/tgs doc); userbot parity          | https://www.genspark.ai/api/files/s/HGgAxcwY |
| 75  | `/massdlt` engine: link parse, batching, FloodWait, long-pause, completion alert | https://www.genspark.ai/api/files/s/CQyM3TTH |
| 85  | `/massdlt` + `/massdlt_stop` + `/massdlt_status` handlers, BotCommand roster, /help | https://www.genspark.ai/api/files/s/CQyM3TTH |
| 92  | `smoke_v15.py` — 24/24 cases pass (classifier, probe, link parse, cadence)     | https://www.genspark.ai/api/files/s/CQyM3TTH |
| 100 | Final package `telegram-file-bot_v15.zip`                                     | (this file) |

## v15 changes

### 1. Stop documents (cbz / zip / stickers) from leaking to main channel
Root cause: v13-era classifier mis-labelled some DB-channel documents as
`kind='cover', media_kind='document'` with a NULL filename. Publish path
fell through to `copyMessage`, which happily re-sent the raw file into the
main channel — and `copyMessage` cannot apply `has_spoiler`, so image
covers posted un-blurred too.

v15 fix — publish-time truth probe (`app/services/posting.py`):
- Before each cover is published, `userbot.probe_source_message()` re-
  inspects the actual DB-channel message via MTProto.
- If it's really an attachable file → `repo.reclassify_stored_row()` moves
  it to `kind='pdf'`, attaches it to the previous cover, and the publish
  is skipped (returns `{skipped: 'reclassified_as_pdf'}`).
- If it's a sticker → reclassified to `kind='skip'`, publish skipped.
- If it's an image/video document mis-stored as `media_kind='document'`,
  the row is patched to `'photo'` / `'video'` with the real MIME and
  filename, so the existing sendPhoto/sendVideo path now fires and
  `has_spoiler=True` is applied when `/spoiler` is ON.

### 2. Spoiler actually applied to image covers
Because the row is now patched to `media_kind='photo'` with the real
MIME before `_send_one()` runs, `_is_image_cover()` returns True and the
`tg.send_photo(..., has_spoiler=True)` branch is taken — never
`copyMessage`. When `/spoiler` is OFF the same branch runs with
`has_spoiler=False` so the image is still never leaked as a raw file.

### 3. Stickers never enter the pipeline (ingest-time rule)
- `app/services/sync.py`: `classify_message()` returns `('skip','sticker',…)`
  for `msg.sticker`, for webp/tgs MIME documents with no filename, and for
  DocumentAttributeSticker-bearing documents.
- `app/services/userbot.py`: `classify()` mirrors the same three rules so
  the MTProto backfill cannot re-introduce stickers either.

### 4. New command: `/massdlt <chat_id> <start_link> <end_link>`
Production-ready MTProto bulk delete in `app/services/userbot.py` +
`app/handlers/commands.py`:
- Parses `t.me/c/<id>/<mid>` and `t.me/<username>/<mid>` links.
- 3-arg form: explicit chat id + both links.
- 2-arg form: chat id inferred from links (must match).
- Batches of 100 IDs per `client.delete_messages()` call.
- 2-second delay between batches (`_MASSDLT_DELAY_S`).
- 20-second safety pause every 200 submitted IDs (`_MASSDLT_LONG_PAUSE_*`).
- FloodWait-aware: catches `telethon.errors.FloodWaitError`, sleeps
  `e.seconds + 1`, retries the same batch.
- Live progress DM every ~5 batches; final "Deletion Complete!" summary
  with chat, range, deleted count, error count, elapsed time.
- Range cap: 200 000 IDs (safety) — larger spans must be split.
- Companion commands: `/massdlt_status`, `/massdlt_stop`.
- Both handlers + BotCommand roster + `/help` text updated.

### Files touched in v15
- `app/services/repo.py`        — added `reclassify_stored_row()`, `row_mime()`
- `app/services/sync.py`        — sticker-drop in classifier
- `app/services/userbot.py`     — sticker-drop in classifier, added
                                   `probe_source_message()`, `/massdlt` engine
- `app/services/posting.py`     — added `_probe_and_reclassify()` gate in
                                   `publish_cover_to_mains()`
- `app/handlers/commands.py`    — added `/massdlt`, `/massdlt_stop`,
                                   `/massdlt_status` handlers + roster + help
- `smoke_v15.py`                — 24-case regression suite (deleted from zip
                                   per handoff rule; kept in source dir)

### Smoke test results
```
== 1) sync.classify_message drops stickers ==     5/5 ✅
== 2) userbot.classify drops stickers ==          3/3 ✅
== 3-5) posting._probe_and_reclassify ==          8/8 ✅
== 6) userbot.parse_massdlt_link ==               3/3 ✅
== 7) /massdlt loop cadence + FloodWait + alert == 5/5 ✅
ALL SMOKE CASES PASSED ✅
```

### Deploy notes
1. Push to GitHub, Render auto-deploys.
2. `/debug` should confirm `/spoiler 1` and that the userbot is logged in
   (`/tgstatus` → "logged in as @…").
3. Run `/dripnow 1` once — Render logs should show:
   - `[publish] id=… reclassified as image cover` (for any legacy image-doc)
   - `[publish] id=… kind=photo … is_img=True` (spoiler branch engaged)
   - `[publish] id=… reclassified as pdf …` (for any stray cbz/sticker that
     slipped in under v13) — those will NOT appear in the main channel.
4. `/massdlt -100<chan> <start_link> <end_link>` for the cleanup you asked
   about — progress and completion alerts land in your admin DM.
