"""Database layer: a single Turso/SQLite connection + schema + query helpers.

Uses the `libsql` SDK. Two modes:
  * Remote Turso (set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN)
  * Local file   (set DATABASE_PATH, or default "bot.db")

All access is guarded by a lock and serialized. Rows are returned as dicts.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Iterable, Optional, Sequence

from libsql import connect

from .config import settings

_lock = threading.RLock()
_conn = None

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA: list[str] = [
    # ---- people ---------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS admins (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL UNIQUE,
        username        TEXT,
        first_name      TEXT,
        is_super_admin  INTEGER NOT NULL DEFAULT 0,
        added_by        INTEGER,
        created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        telegram_user_id     INTEGER PRIMARY KEY,
        username             TEXT,
        first_name           TEXT,
        last_name            TEXT,
        joined_at            TEXT,
        last_active_at       TEXT,
        is_banned            INTEGER NOT NULL DEFAULT 0,
        ban_reason           TEXT,
        warn_count           INTEGER NOT NULL DEFAULT 0,
        files_fetched        INTEGER NOT NULL DEFAULT 0,
        files_fetched_today  INTEGER NOT NULL DEFAULT 0,
        last_fetch_day       TEXT,
        last_fetch_at        TEXT,
        -- shortener gate
        sh_verified_until    TEXT,
        sh_files_used        INTEGER NOT NULL DEFAULT 0,
        sh_pending_token     TEXT,
        sh_pending_issued_at TEXT,
        sh_pending_verified_at TEXT,
        sh_pending_code      TEXT,
        sh_bypass_count      INTEGER NOT NULL DEFAULT 0
    )
    """,

    # ---- channels -------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS channels (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_chat_id INTEGER NOT NULL UNIQUE,
        title            TEXT,
        role             TEXT NOT NULL,             -- database|main|log|backup|forcesub
        invite_link      TEXT,
        username         TEXT,
        also_post        INTEGER NOT NULL DEFAULT 0,
        also_fsub        INTEGER NOT NULL DEFAULT 0,
        added_by         INTEGER,
        created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    # ---- posts (the core catalog + resume cursor) ------------------------ #
    """
    CREATE TABLE IF NOT EXISTS posts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        code              TEXT NOT NULL UNIQUE,
        position          INTEGER NOT NULL,          -- sequential #N for backfill
        source_chat_id    INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        main_message_id   INTEGER,                   -- id in the main channel after posting
        caption           TEXT,
        media_kind        TEXT NOT NULL,             -- photo|video|document|audio|text
        file_id           TEXT,
        file_name         TEXT,
        mime_type         TEXT,
        extra_files       TEXT,                      -- JSON array of file entries
        media_group_id    TEXT,
        posted_at         TEXT,                      -- null = queued
        is_deleted        INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        UNIQUE(source_chat_id, source_message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_position ON posts(position)",
    "CREATE INDEX IF NOT EXISTS idx_posts_code ON posts(code)",
    "CREATE INDEX IF NOT EXISTS idx_posts_queue ON posts(posted_at, position)",

    # ---- post copies (which main/backup channel already has which post) --- #
    """
    CREATE TABLE IF NOT EXISTS post_copies (
        post_id      INTEGER NOT NULL,
        target_chat_id INTEGER NOT NULL,
        message_id   INTEGER,
        created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (post_id, target_chat_id)
    )
    """,

    # ---- engagement ------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS favorites (
        user_id    INTEGER NOT NULL,
        post_id    INTEGER NOT NULL,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (user_id, post_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS post_ratings (post_id INTEGER PRIMARY KEY, up INTEGER NOT NULL DEFAULT 0, down INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS user_post_ratings (user_id INTEGER NOT NULL, post_id INTEGER NOT NULL, vote TEXT NOT NULL, PRIMARY KEY (user_id, post_id))",
    """
    CREATE TABLE IF NOT EXISTS user_streaks (
        user_id        INTEGER PRIMARY KEY,
        current        INTEGER NOT NULL DEFAULT 0,
        longest        INTEGER NOT NULL DEFAULT 0,
        last_fetch_day TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS referrals (
        referrer_id INTEGER NOT NULL,
        referee_id  INTEGER PRIMARY KEY,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    "CREATE TABLE IF NOT EXISTS referral_bonuses (user_id INTEGER PRIMARY KEY, bonus_files_remaining INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS tag_subscriptions (user_id INTEGER NOT NULL, tag TEXT NOT NULL, created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), PRIMARY KEY (user_id, tag))",

    # ---- forced-subscription state --------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS fsub_satisfied (
        user_id          INTEGER NOT NULL,
        channel_chat_id  INTEGER NOT NULL,
        satisfied_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (user_id, channel_chat_id)
    )
    """,

    # ---- moderation / audit ---------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS warnings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        admin_id   INTEGER NOT NULL,
        reason     TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id   INTEGER NOT NULL,
        action     TEXT NOT NULL,
        details    TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id   INTEGER NOT NULL,
        action     TEXT NOT NULL,
        target     TEXT,
        details    TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    # ---- deletion TTL ------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS pending_deletions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        delete_at  TEXT NOT NULL,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    # ---- deleted posts archive ------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS deleted_posts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id    INTEGER NOT NULL,
        code       TEXT,
        caption    TEXT,
        deleted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        deleted_by INTEGER,
        snapshot   TEXT
    )
    """,

    # ---- scheduling -------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS scheduled_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,        -- code|oneshot
        post_code    TEXT,
        media        TEXT,
        caption      TEXT,
        scheduled_for TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending', -- pending|done|cancelled|failed
        last_error   TEXT,
        created_by   INTEGER,
        created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        processed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broadcast_jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        text          TEXT,
        media         TEXT,
        status        TEXT NOT NULL DEFAULT 'pending', -- pending|scheduled|done|cancelled
        scheduled_for TEXT,
        progress      INTEGER NOT NULL DEFAULT 0,
        total         INTEGER NOT NULL DEFAULT 0,
        created_by    INTEGER,
        created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    # ---- backups ----------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS backup_copies (
        backup_chat_id INTEGER NOT NULL,
        post_id        INTEGER NOT NULL,
        message_id     INTEGER,
        created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (backup_chat_id, post_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_failures (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_chat_id INTEGER NOT NULL,
        post_id        INTEGER,
        reason         TEXT,
        created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    # ---- settings + sync state + idempotency ------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS bot_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, received_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
    """
    CREATE TABLE IF NOT EXISTS link_tokens (
        token      TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        user_id    INTEGER,
        payload    TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
]

# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

def get_conn():
    global _conn
    if _conn is None:
        if settings.turso_database_url:
            _conn = connect(
                database=settings.turso_database_url,
                auth_token=settings.turso_auth_token,
            )
        else:
            path = settings.database_path or "bot.db"
            _conn = connect(database=f"file:{path}", auth_token="")
        _init_schema(_conn)
    return _conn


def _init_schema(conn) -> None:
    for stmt in SCHEMA:
        try:
            conn.execute(stmt)
        except Exception as exc:  # pragma: no cover
            # Log and continue — most statements are idempotent CREATE IF NOT EXISTS.
            print(f"[db] schema statement failed ({exc}): {stmt[:80]!r}")


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #

def _cols(cur) -> list[str]:
    return [d[0] for d in cur.description] if cur.description else []


def execute(sql: str, params: Sequence = ()) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(sql, params)
        conn.commit()


def insert(sql: str, params: Sequence = ()) -> int:
    """Execute an INSERT and return lastrowid."""
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid or 0)


def query_one(sql: str, params: Sequence = ()) -> Optional[dict]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip(_cols(cur), row))


def query_all(sql: str, params: Sequence = ()) -> list[dict]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cols = _cols(cur)
        return [dict(zip(cols, r)) for r in rows]


def query_scalar(sql: str, params: Sequence = ()) -> Optional[Any]:
    row = query_one(sql, params)
    if row is None:
        return None
    return next(iter(row.values()), None)


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #

def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text: Optional[str], default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default
