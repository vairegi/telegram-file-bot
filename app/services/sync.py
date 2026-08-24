"""Live sync — handles real-time channel_post updates from the DB channel.

Zero-DB-read hot path:
  * `database_chat_ids()` is a CACHED set — non-DB updates exit immediately.
  * Dedupe is via INSERT OR IGNORE — no pre-SELECT.
  * Parent lookup is ONE indexed query.

Sticker rule (per v2 spec):
  * Stickers found BELOW a cover attach to that cover (kind='file', media='sticker').
  * Stickers found ABOVE the first cover are DROPPED (never stored).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import repo
from .classify import classify, caption_of

log = logging.getLogger("sync")


async def handle_channel_post(msg) -> Optional[dict]:
    """Ingest a single channel post. Returns a compact result dict, or None."""
    chat = getattr(msg, "chat", None)
    chat_id = int(getattr(chat, "id", 0) or 0)
    if not chat_id or chat_id not in repo.database_chat_ids():
        return None

    msg_id = int(getattr(msg, "message_id", 0) or 0)
    if not msg_id:
        return None

    # Cursor gate: skip everything <= cursor (previously imported).
    cursor = repo.get_cursor(chat_id)
    if cursor and msg_id <= cursor:
        return None

    kind, media_kind, file_id, file_name, mime = classify(msg)
    if kind == "skip":
        # Update cursor so we don't re-visit this msg id.
        repo.set_cursor(chat_id, msg_id)
        return None

    caption = caption_of(msg)

    if kind == "cover":
        pid = repo.insert_cover(
            source_chat_id=chat_id, source_message_id=msg_id,
            caption=caption, media_kind=media_kind,
            file_id=file_id, file_name=file_name, mime_type=mime,
        )
        repo.set_cursor(chat_id, msg_id)
        return {"kind": "cover", "id": pid, "chat_id": chat_id, "msg_id": msg_id}

    # kind == 'file' — need parent cover.
    parent = repo.find_cover_before(chat_id, msg_id - 1)
    if not parent:
        # File / sticker BEFORE the first cover in the channel → skip per spec.
        repo.set_cursor(chat_id, msg_id)
        return None

    pid = repo.insert_file(
        source_chat_id=chat_id, source_message_id=msg_id,
        parent_msg_id=int(parent["source_message_id"]),
        caption=caption, media_kind=media_kind,
        file_id=file_id, file_name=file_name, mime_type=mime,
    )
    repo.set_cursor(chat_id, msg_id)
    return {"kind": "file", "id": pid, "chat_id": chat_id, "msg_id": msg_id,
            "parent_msg_id": int(parent["source_message_id"])}
