"""Data-access repository (SQLite/libsql).

Aligned to the existing db.py schema:
- Settings live in `bot_settings(key,value)`.
- Channels live in `channels(telegram_chat_id, role, ...)`.
- Posts live in `posts(...)`; migrations add:
    kind ('cover'|'pdf'), parent_source_message_id, post_number,
    main_chat_id, published_at, extra_json.

Cursor is stored in bot_settings as either:
  cursor:<db_chat_id>              -> global cursor for a DB channel
  cursor:<db_chat_id>:<main_chat_id> -> per-main-channel cursor (multi-main)
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

from ..db import execute, insert, query_all, query_one, query_scalar
from ..utils import now_iso, random_code

CHANNEL_ROLES = ("database", "main", "log", "backup", "forcesub")


# ------------------------- settings ---------------------------------
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = query_one("SELECT value FROM bot_settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Optional[str]) -> None:
    if value is None:
        execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        return
    execute(
        "INSERT INTO bot_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


def get_setting_json(key: str, default: Any = None) -> Any:
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_setting_json(key: str, value: Any) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False))


def get_setting_bool(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ------------------------- cursor (multi) ---------------------------
def _cursor_key(db_chat_id: int, main_chat_id: Optional[int] = None) -> str:
    return f"cursor:{db_chat_id}" if main_chat_id is None else f"cursor:{db_chat_id}:{main_chat_id}"


def get_cursor(db_chat_id: int, main_chat_id: Optional[int] = None) -> int:
    v = get_setting(_cursor_key(db_chat_id, main_chat_id))
    try:
        return int(v) if v else 0
    except Exception:
        return 0


def set_cursor(db_chat_id: int, message_id: int, main_chat_id: Optional[int] = None) -> None:
    set_setting(_cursor_key(db_chat_id, main_chat_id), str(int(message_id)))


def all_cursor_keys() -> List[Tuple[str, str]]:
    rows = query_all("SELECT key, value FROM bot_settings WHERE key LIKE 'cursor:%'")
    return [(r["key"], r["value"]) for r in rows]


# ------------------------- channels ---------------------------------
def add_channel(chat_id: int, role: str, title: Optional[str] = None,
                added_by: Optional[int] = None, invite_link: Optional[str] = None) -> None:
    execute(
        "INSERT INTO channels(telegram_chat_id,title,role,invite_link,added_by) VALUES(?,?,?,?,?) "
        "ON CONFLICT(telegram_chat_id) DO UPDATE SET role=excluded.role, "
        "title=COALESCE(excluded.title,channels.title), added_by=COALESCE(excluded.added_by,channels.added_by), "
        "invite_link=COALESCE(excluded.invite_link,channels.invite_link)",
        (chat_id, title, role, invite_link, added_by),
    )


def remove_channel(chat_id: int) -> None:
    execute("DELETE FROM channels WHERE telegram_chat_id = ?", (chat_id,))


def set_channel_flag(chat_id: int, flag: str, value: bool) -> None:
    if flag not in ("also_fsub", "also_backup", "also_post"):
        return
    execute(f"UPDATE channels SET {flag} = ? WHERE telegram_chat_id = ?",
            (1 if value else 0, chat_id))


def get_channel(chat_id: int) -> Optional[dict]:
    return query_one(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link "
        "FROM channels WHERE telegram_chat_id = ?", (chat_id,))


def _by_role(role: str) -> List[dict]:
    return query_all(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link "
        "FROM channels WHERE role = ? ORDER BY telegram_chat_id", (role,))


def get_database_channels() -> List[dict]:
    return _by_role("database")


def get_main_channels() -> List[dict]:
    return query_all(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link "
        "FROM channels WHERE role='main' OR also_post=1 ORDER BY telegram_chat_id")


def get_backup_channels() -> List[dict]:
    return query_all(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link "
        "FROM channels WHERE role='backup' OR also_backup=1 ORDER BY telegram_chat_id")


def get_forcesub_channels() -> List[dict]:
    return query_all(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link "
        "FROM channels WHERE role='forcesub' OR also_fsub=1 ORDER BY telegram_chat_id")


def get_log_channel_id() -> Optional[int]:
    row = query_one("SELECT telegram_chat_id FROM channels WHERE role='log' LIMIT 1")
    return int(row["telegram_chat_id"]) if row else None


def list_all_channels() -> List[dict]:
    return query_all(
        "SELECT telegram_chat_id AS chat_id, role, title, invite_link, also_post, also_backup, also_fsub "
        "FROM channels ORDER BY role, telegram_chat_id")


# ------------------------- posts ------------------------------------
def post_exists(source_chat_id: int, source_message_id: int) -> bool:
    row = query_one("SELECT id FROM posts WHERE source_chat_id=? AND source_message_id=?",
                    (source_chat_id, source_message_id))
    return bool(row)


def get_post_by_code(code: str) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE code = ?", (code,))


def get_post_by_number(n: int) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE post_number=? AND kind='cover'", (n,))


def get_post_by_id(pid: int) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE id=?", (pid,))


def get_post_by_source(chat_id: int, msg_id: int) -> Optional[dict]:
    return query_one(
        "SELECT * FROM posts WHERE source_chat_id=? AND source_message_id=?",
        (chat_id, msg_id))


def next_post_number() -> int:
    n = query_scalar("SELECT COALESCE(MAX(post_number),0) FROM posts WHERE post_number IS NOT NULL")
    return int(n or 0) + 1


def next_position() -> int:
    n = query_scalar("SELECT COALESCE(MAX(position),0) FROM posts")
    return int(n or 0) + 1


def total_posts() -> int:
    return int(query_scalar("SELECT COUNT(*) FROM posts") or 0)


def total_covers() -> int:
    return int(query_scalar("SELECT COUNT(*) FROM posts WHERE kind='cover'") or 0)


def queued_covers_count(db_chat_id: int = 0) -> int:
    if db_chat_id:
        return int(query_scalar(
            "SELECT COUNT(*) FROM posts WHERE kind='cover' AND published_at IS NULL "
            "AND is_deleted=0 AND source_chat_id=?",
            (db_chat_id,)) or 0)
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind='cover' AND published_at IS NULL AND is_deleted=0") or 0)


def published_covers_count() -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind='cover' AND published_at IS NOT NULL") or 0)


def next_queued_covers(limit: int = 10, db_chat_id: int = 0) -> List[dict]:
    """Queued covers in TRUE channel order (source_chat_id, source_message_id)."""
    if db_chat_id:
        return query_all(
            "SELECT * FROM posts WHERE kind='cover' AND published_at IS NULL AND is_deleted=0 "
            "AND source_chat_id=? ORDER BY source_message_id ASC LIMIT ?",
            (db_chat_id, limit))
    return query_all(
        "SELECT * FROM posts WHERE kind='cover' AND published_at IS NULL AND is_deleted=0 "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT ?",
        (limit,))


def next_queued_cover(db_chat_id: int = 0) -> Optional[dict]:
    rows = next_queued_covers(1, db_chat_id)
    return rows[0] if rows else None


def insert_cover(source_chat_id: int, source_message_id: int, caption: Optional[str],
                 media_kind: str, file_id: Optional[str], file_name: Optional[str],
                 raw: Optional[dict] = None) -> Tuple[int, Optional[int], str]:
    code = random_code(8)
    position = next_position()
    pid = insert(
        "INSERT INTO posts(code, position, kind, source_chat_id, source_message_id, "
        "parent_source_message_id, caption, media_kind, file_id, file_name, extra_json, "
        "post_number, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, position, "cover", source_chat_id, source_message_id, None,
         caption, media_kind, file_id, file_name,
         json.dumps(raw or {}, ensure_ascii=False), None, now_iso()))
    return (pid, None, code)


def insert_pdf(source_chat_id: int, source_message_id: int, parent_msg_id: Optional[int],
               caption: Optional[str], media_kind: str, file_id: Optional[str],
               file_name: Optional[str], raw: Optional[dict] = None) -> int:
    position = next_position()
    return insert(
        "INSERT INTO posts(code, position, kind, source_chat_id, source_message_id, "
        "parent_source_message_id, caption, media_kind, file_id, file_name, extra_json, "
        "post_number, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (random_code(8), position, "pdf", source_chat_id, source_message_id, parent_msg_id,
         caption, media_kind, file_id, file_name,
         json.dumps(raw or {}, ensure_ascii=False), None, now_iso()))


def find_cover_before(chat_id: int, msg_id: int) -> Optional[dict]:
    return query_one(
        "SELECT * FROM posts WHERE kind='cover' AND source_chat_id=? AND source_message_id<=? "
        "ORDER BY source_message_id DESC LIMIT 1", (chat_id, msg_id))


def pdfs_of_cover(cover_msg_id: int, chat_id: int) -> List[dict]:
    return query_all(
        "SELECT * FROM posts WHERE kind='pdf' AND source_chat_id=? AND parent_source_message_id=? "
        "ORDER BY source_message_id ASC", (chat_id, cover_msg_id))


def mark_published(post_id: int, main_chat_id: int, main_message_id: int) -> int:
    """Assign permanent #N at publish time. Returns the assigned number."""
    row = query_one("SELECT post_number FROM posts WHERE id=?", (post_id,))
    number = (row or {}).get("post_number")
    if number is None:
        number = next_post_number()
        execute(
            "UPDATE posts SET published_at=?, main_chat_id=?, main_message_id=?, posted_at=?, post_number=? "
            "WHERE id=?",
            (now_iso(), main_chat_id, main_message_id, now_iso(), number, post_id))
    else:
        execute(
            "UPDATE posts SET published_at=?, main_chat_id=?, main_message_id=?, posted_at=? WHERE id=?",
            (now_iso(), main_chat_id, main_message_id, now_iso(), post_id))
    return int(number)


def unpublish(post_id: int) -> None:
    execute(
        "UPDATE posts SET published_at=NULL, main_chat_id=NULL, main_message_id=NULL, posted_at=NULL "
        "WHERE id=?", (post_id,))


def orphan_pdfs_between(chat_id: int, cover_msg_id: int, upto_msg_id: int) -> List[dict]:
    return query_all(
        "SELECT * FROM posts WHERE kind='pdf' AND source_chat_id=? "
        "AND parent_source_message_id IS NULL AND source_message_id > ? AND source_message_id <= ? "
        "ORDER BY source_message_id ASC", (chat_id, cover_msg_id, upto_msg_id))


def attach_pdf_to_cover(pdf_post_id: int, cover_msg_id: int) -> None:
    execute("UPDATE posts SET parent_source_message_id=? WHERE id=?",
            (cover_msg_id, pdf_post_id))


def predicted_number(cover_id: int) -> int:
    """Predicted #N for an unpublished cover = published_max + queue position."""
    n = next_post_number()
    queued = query_all(
        "SELECT id FROM posts WHERE kind='cover' AND published_at IS NULL AND is_deleted=0 "
        "ORDER BY source_chat_id ASC, source_message_id ASC")
    for i, r in enumerate(queued):
        if int(r["id"]) == int(cover_id):
            return n + i
    return n
