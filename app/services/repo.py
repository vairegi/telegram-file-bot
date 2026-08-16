"""Data-access repository: typed helpers over the db module."""
from __future__ import annotations

import json
from typing import Any, Optional

from .. import db
from ..utils import now_iso


# ---------------------------------------------------------------- settings

def get_setting(key: str) -> Optional[str]:
    return db.query_scalar("SELECT value FROM bot_settings WHERE key = ?", (key,))


def set_setting(key: str, value: Any) -> None:
    db.execute(
        "INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value, now_iso()),
    )


def get_setting_json(key: str, default=None):
    raw = get_setting(key)
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------- sync state

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
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SYNC_KEY, str(int(message_id)), now_iso()),
    )


# ---------------------------------------------------------------- channels

CHANNEL_ROLES = ("database", "main", "log", "backup", "forcesub")


def add_channel(chat_id: int, role: str, title: str | None = None,
                invite_link: str | None = None, username: str | None = None,
                added_by: int | None = None) -> None:
    db.execute(
        "INSERT INTO channels (telegram_chat_id, title, role, invite_link, username, added_by) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(telegram_chat_id) DO UPDATE SET "
        "title=excluded.title, role=excluded.role, invite_link=excluded.invite_link, "
        "username=excluded.username",
        (chat_id, title, role, invite_link, username, added_by),
    )


def remove_channel(chat_id: int) -> None:
    db.execute("DELETE FROM channels WHERE telegram_chat_id = ?", (chat_id,))


def get_channels(role: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM channels WHERE role = ? OR also_post = 1", (role,)
    )


def get_channel(chat_id: int) -> Optional[dict]:
    return db.query_one("SELECT * FROM channels WHERE telegram_chat_id = ?", (chat_id,))


def get_database_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role = 'database'")


def get_main_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role = 'main' OR also_post = 1")


def get_backup_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role = 'backup'")


def get_forcesub_channels() -> list[dict]:
    return db.query_all("SELECT * FROM channels WHERE role = 'forcesub' OR also_fsub = 1")


# ---------------------------------------------------------------- posts

def post_exists(source_chat_id: int, source_message_id: int) -> bool:
    return db.query_scalar(
        "SELECT 1 FROM posts WHERE source_chat_id = ? AND source_message_id = ?",
        (source_chat_id, source_message_id),
    ) is not None


def get_post_by_code(code: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM posts WHERE code = ?", (code,))


def get_post_extra_files(post_id: int) -> list[dict]:
    raw = db.query_scalar("SELECT extra_files FROM posts WHERE id = ?", (post_id,))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def get_next_position() -> int:
    n = db.query_scalar("SELECT COALESCE(MAX(position), 0) FROM posts")
    return int(n or 0) + 1
