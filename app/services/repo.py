"""Typed data-access repository."""
from __future__ import annotations

import json
from typing import Any, Optional

from .. import db
from ..utils import now_iso

# ---- settings

def get_setting(key: str) -> Optional[str]:
    return db.query_scalar("SELECT value FROM bot_settings WHERE key = ?", (key,))


def set_setting(key: str, value: Any) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    db.execute(
        "INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


def get_setting_json(key: str, default=None):
    raw = get_setting(key)
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def get_setting_bool(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "on", "yes")


# ---- sync state

SYNC_KEY = "last_processed_message_id"


def get_cursor() -> int:
    val = db.query_scalar("SELECT value FROM sync_state WHERE key = ?", (SYNC_KEY,))
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def set_cursor(message_id: int) -> None:
    db.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (SYNC_KEY, str(int(message_id)), now_iso()),
    )


# ---- channels

CHANNEL_ROLES = ("database", "main", "log", "backup", "forcesub")


def add_channel(chat_id, role, title=None, invite_link=None, username=None, added_by=None):
    db.execute(
        "INSERT INTO channels (telegram_chat_id, title, role, invite_link, username, added_by) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(telegram_chat_id) DO UPDATE SET "
        "title=excluded.title, role=excluded.role, invite_link=excluded.invite_link, "
        "username=excluded.username",
        (chat_id, title, role, invite_link, username, added_by),
    )


def remove_channel(chat_id):
    db.execute("DELETE FROM channels WHERE telegram_chat_id = ?", (chat_id,))


def set_channel_flag(chat_id, field, on: bool) -> bool:
    if field not in ("also_post", "also_fsub", "also_backup"):
        return False
    db.execute(f"UPDATE channels SET {field}=? WHERE telegram_chat_id=?",
               (1 if on else 0, chat_id))
    return True


def get_channel(chat_id) -> Optional[dict]:
    return db.query_one("SELECT * FROM channels WHERE telegram_chat_id=?", (chat_id,))


def get_database_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role='database'")


def get_main_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role='main' OR also_post=1")


def get_backup_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role='backup' OR also_backup=1")


def get_forcesub_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role='forcesub' OR also_fsub=1")


def get_log_channel_id() -> int:
    row = db.query_one("SELECT telegram_chat_id FROM channels WHERE role='log' LIMIT 1")
    return int(row["telegram_chat_id"]) if row else 0


# ---- posts

def post_exists(source_chat_id, source_message_id) -> bool:
    return db.query_scalar(
        "SELECT 1 FROM posts WHERE source_chat_id=? AND source_message_id=?",
        (source_chat_id, source_message_id),
    ) is not None


def get_post_by_code(code) -> Optional[dict]:
    return db.query_one("SELECT * FROM posts WHERE code=?", (code,))


def get_post_by_position(pos) -> Optional[dict]:
    return db.query_one("SELECT * FROM posts WHERE position=?", (pos,))


def get_post_by_source(chat_id, msg_id) -> Optional[dict]:
    return db.query_one("SELECT * FROM posts WHERE source_chat_id=? AND source_message_id=?",
                        (chat_id, msg_id))


def get_next_position() -> int:
    return int(db.query_scalar("SELECT COALESCE(MAX(position),0) FROM posts") or 0) + 1


def total_posts() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM posts") or 0)


def queued_posts_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM posts WHERE posted_at IS NULL AND is_deleted=0") or 0)


def published_posts_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM posts WHERE posted_at IS NOT NULL") or 0)


def insert_post(code, position, source_chat_id, source_message_id, caption,
                media_kind, file_id=None, file_name=None, mime_type=None,
                extra_files=None, media_group_id=None, posted_at=None, created_at=None):
    sql = ("INSERT INTO posts (code, position, source_chat_id, source_message_id, caption, "
           "media_kind, file_id, file_name, mime_type, extra_files, media_group_id, posted_at"
           + (", created_at" if created_at else "") + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?"
           + (",?" if created_at else "") + ")")
    params = [code, position, source_chat_id, source_message_id, caption, media_kind,
              file_id, file_name, mime_type,
              json.dumps(extra_files or [], ensure_ascii=False), media_group_id, posted_at]
    if created_at:
        params.append(created_at)
    return db.insert(sql, params)
