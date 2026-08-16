"""Sync engine: capture Database-Channel posts, enforce the resume cursor,
and queue them for publishing to Main channels.

Resume logic
------------
Telegram delivers only *forward-going* `channel_post` updates to a Bot-API bot,
so the bot cannot re-scan history by itself. The cursor therefore serves two
purposes:
  1. On first boot, seed the cursor from START_MESSAGE_ID (env) so anything you
     already posted is treated as "done".
  2. Guard against accidental duplicate delivery of old channel posts that
     Telegram may re-send (retries) — any message_id <= cursor is ignored.

The authoritative "already handled" signal is the UNIQUE(source_chat_id,
source_message_id) constraint in `posts` plus the high-water-mark cursor.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .. import db
from ..config import settings
from ..utils import now_iso, random_code
from . import repo
from .tg import get_me, send_message


async def ensure_cursor_seeded() -> int:
    """Seed the cursor from START_MESSAGE_ID on first boot if it is empty."""
    cursor = repo.get_cursor()
    if cursor == 0 and settings.start_message_id > 0:
        repo.set_cursor(settings.start_message_id)
        cursor = settings.start_message_id
    return cursor


def classify_message(msg: dict) -> tuple[str, dict]:
    """Return (kind, media). kind: main|file. media has file_id/kind fields.

    Mirrors the original bot's layout convention:
      - photo          -> main
      - video + caption-> main
      - video no caption-> file
      - document/audio -> file
      - text only      -> main
    """
    if msg.get("photo"):
        photos = msg["photo"]
        largest = photos[-1]
        return "main", {"kind": "photo", "file_id": largest["file_id"]}
    if msg.get("video"):
        vid = msg["video"]
        caption = (msg.get("caption") or "").strip()
        if caption:
            kind = "main"
        else:
            kind = "file"
        return kind, {
            "kind": "video",
            "file_id": vid.get("file_id"),
            "file_name": vid.get("file_name"),
            "mime_type": vid.get("mime_type"),
        }
    if msg.get("document"):
        doc = msg["document"]
        return "file", {
            "kind": "document",
            "file_id": doc.get("file_id"),
            "file_name": doc.get("file_name"),
            "mime_type": doc.get("mime_type"),
        }
    if msg.get("audio"):
        aud = msg["audio"]
        return "file", {
            "kind": "audio",
            "file_id": aud.get("file_id"),
            "file_name": aud.get("file_name"),
            "mime_type": aud.get("mime_type"),
        }
    return "main", {"kind": "text"}


# In-memory buffer for grouping: main post + trailing files within a window.
_pending: dict[int, dict] = {}


async def handle_channel_post(chat_id: int, message_id: int, msg: dict) -> Optional[str]:
    """Process a channel_post from a Database Channel. Returns a short status.

    chat_id is negative for channels/supergroups.
    """
    # Only care about configured database channels.
    db_channels = {int(c["telegram_chat_id"]) for c in repo.get_database_channels()}
    if not db_channels:
        return "no-database-channels"

    if chat_id not in db_channels:
        return "not-database-channel"

    cursor = await ensure_cursor_seeded()

    # Resume guard: ignore anything at/below the cursor (already handled).
    if message_id <= cursor:
        return "skipped-below-cursor"

    kind, media = classify_message(msg)
    caption = msg.get("caption") or msg.get("text") or ""
    media_group_id = msg.get("media_group_id")

    if kind == "main":
        # Finalize any previous buffered post first.
        await _flush_pending(chat_id)
        # Start a new post.
        _pending[chat_id] = {
            "source_chat_id": chat_id,
            "source_message_id": message_id,
            "caption": caption,
            "media": media,
            "extra_files": [],
            "media_group_id": media_group_id,
            "created_at": now_iso(),
        }
        # A standalone main post is immediately persistable.
        await _persist(chat_id)
        repo.set_cursor(message_id)
        return "captured-main"
    else:
        # A file attaches to the previous main post (if within same chat).
        buf = _pending.get(chat_id)
        if buf is None:
            # Orphan file -> its own post so nothing is lost.
            _pending[chat_id] = {
                "source_chat_id": chat_id,
                "source_message_id": message_id,
                "caption": caption,
                "media": media,
                "extra_files": [],
                "media_group_id": media_group_id,
                "created_at": now_iso(),
            }
            await _persist(chat_id)
            repo.set_cursor(message_id)
            return "captured-orphan-file"
        buf["extra_files"].append(media)
        _pending[chat_id] = buf
        repo.set_cursor(message_id)
        return "attached-file"


async def _persist(chat_id: int) -> None:
    buf = _pending.pop(chat_id, None)
    if not buf:
        return
    code = random_code()
    position = repo.get_next_position()
    extra = json.dumps(buf.get("extra_files") or [], ensure_ascii=False)
    media = buf["media"]
    db.insert(
        "INSERT INTO posts (code, position, source_chat_id, source_message_id, "
        "caption, media_kind, file_id, file_name, mime_type, extra_files, "
        "media_group_id, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            code,
            position,
            buf["source_chat_id"],
            buf["source_message_id"],
            buf["caption"],
            media.get("kind", "text"),
            media.get("file_id"),
            media.get("file_name"),
            media.get("mime_type"),
            extra,
            buf.get("media_group_id"),
        ),
    )


async def _flush_pending(chat_id: int) -> None:
    if chat_id in _pending:
        # already persisted in _persist; nothing to do but clear stale state
        _pending.pop(chat_id, None)


async def notify_admins(text: str) -> None:
    """Send a notification to the log channel (if set) — best-effort."""
    log_id = repo.get_setting("log_channel_id")
    if not log_id:
        return
    try:
        await send_message(int(log_id), text)
    except Exception:  # noqa: BLE001
        pass
