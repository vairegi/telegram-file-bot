"""Sync engine: capture Database-Channel posts and enforce the resume cursor."""
from __future__ import annotations

import json
from typing import Optional

from .. import db
from ..config import settings
from ..utils import now_iso, random_code
from . import repo
from .tg import send_message


async def ensure_cursor_seeded() -> int:
    cursor = repo.get_cursor()
    if cursor == 0 and settings.start_message_id > 0:
        repo.set_cursor(settings.start_message_id)
        cursor = settings.start_message_id
    return cursor


def classify_message(msg: dict) -> tuple[str, dict]:
    if msg.get("photo"):
        largest = msg["photo"][-1]
        return "main", {"kind": "photo", "file_id": largest["file_id"]}
    if msg.get("video"):
        vid = msg["video"]
        caption = (msg.get("caption") or "").strip()
        return ("main" if caption else "file"), {
            "kind": "video", "file_id": vid.get("file_id"),
            "file_name": vid.get("file_name"), "mime_type": vid.get("mime_type"),
        }
    if msg.get("document"):
        doc = msg["document"]
        return "file", {
            "kind": "document", "file_id": doc.get("file_id"),
            "file_name": doc.get("file_name"), "mime_type": doc.get("mime_type"),
        }
    if msg.get("audio"):
        aud = msg["audio"]
        return "file", {
            "kind": "audio", "file_id": aud.get("file_id"),
            "file_name": aud.get("file_name"), "mime_type": aud.get("mime_type"),
        }
    return "main", {"kind": "text"}


_pending: dict[int, dict] = {}


async def handle_channel_post(chat_id: int, message_id: int, msg: dict) -> Optional[str]:
    if repo.get_setting_bool("posting_paused"):
        return "posting-paused"
    db_channels = {int(c["telegram_chat_id"]) for c in repo.get_database_channels()}
    if not db_channels:
        return "no-database-channels"
    if chat_id not in db_channels:
        return "not-database-channel"

    cursor = await ensure_cursor_seeded()
    if message_id <= cursor:
        return "skipped-below-cursor"

    kind, media = classify_message(msg)
    caption = msg.get("caption") or msg.get("text") or ""
    media_group_id = msg.get("media_group_id")

    if kind == "main":
        await _flush_pending(chat_id)
        _pending[chat_id] = {
            "source_chat_id": chat_id, "source_message_id": message_id,
            "caption": caption, "media": media, "extra_files": [],
            "media_group_id": media_group_id,
        }
        await _persist(chat_id)
        repo.set_cursor(message_id)
        return "captured-main"

    buf = _pending.get(chat_id)
    if buf is None:
        _pending[chat_id] = {
            "source_chat_id": chat_id, "source_message_id": message_id,
            "caption": caption, "media": media, "extra_files": [],
            "media_group_id": media_group_id,
        }
        await _persist(chat_id)
        repo.set_cursor(message_id)
        return "captured-orphan-file"

    buf["extra_files"].append(media)
    _pending[chat_id] = buf
    repo.set_cursor(message_id)
    db.execute(
        "UPDATE posts SET extra_files=? WHERE source_chat_id=? AND source_message_id=?",
        (json.dumps(buf["extra_files"], ensure_ascii=False),
         buf["source_chat_id"], buf["source_message_id"]),
    )
    return "attached-file"


async def _persist(chat_id: int) -> None:
    buf = _pending.get(chat_id)
    if not buf:
        return
    if repo.post_exists(buf["source_chat_id"], buf["source_message_id"]):
        return
    repo.insert_post(
        code=random_code(),
        position=repo.get_next_position(),
        source_chat_id=buf["source_chat_id"],
        source_message_id=buf["source_message_id"],
        caption=buf["caption"],
        media_kind=buf["media"].get("kind", "text"),
        file_id=buf["media"].get("file_id"),
        file_name=buf["media"].get("file_name"),
        mime_type=buf["media"].get("mime_type"),
        extra_files=buf.get("extra_files"),
        media_group_id=buf.get("media_group_id"),
    )


async def _flush_pending(chat_id: int) -> None:
    _pending.pop(chat_id, None)


async def notify_admins(text: str) -> None:
    log_id = repo.get_log_channel_id()
    if not log_id:
        return
    try:
        await send_message(log_id, text)
    except Exception:
        pass
