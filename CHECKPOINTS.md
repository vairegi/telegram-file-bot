# Checkpoint ledger

| % | Milestone | Link |
|---|-----------|------|
| 25 | Foundation (utils/db migrations/repo/sync + numbering + multi-cursor) | (cp1, superseded) |
| 50 | Posting engine + tg client + protect_content | (cp2, superseded) |
| 75 | Handlers + scheduler + main.py + role separation | (cp3, superseded) |
| 100 | v13 packaged (archive-as-cover, divider skip, image-mime spoiler) | (v13, superseded) |
| 100 | v14 — publish-path leak fix + MIME-aware spoiler recognition | (this build) |

## v14 changelog
- `app/services/sync.py`: split image/video/file classification. Image and
  video documents are ALWAYS covers (never attachable files) even under
  `application/octet-stream` MIME. Video-documents now remap to
  `media_kind="video"` so spoilers work on backfilled `.mp4` covers.
- `app/services/userbot.py`: same MIME-vs-extension split so the MTProto
  backfill classifier matches live sync exactly.
- `app/services/posting.py`:
  - `_is_image_cover()` / `_is_video_cover()` now also match by MIME
    (from the `mime_type` column or `extra_json`), tolerating documents
    where Telegram stripped the filename.
  - Publish path routes image/video covers through `sendPhoto` /
    `sendVideo` unconditionally (with `has_spoiler=bool(spoiler)`), so a
    raw image-document can no longer leak to the main channel via
    `copyMessage` when `/spoiler` is OFF.
  - Added `[publish] id=… kind=… mime=… name=… spoiler=… is_img=… is_vid=…`
    diagnostic line so future misroutes are traceable in Render logs.
