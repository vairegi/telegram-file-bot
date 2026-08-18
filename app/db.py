"""Database layer: Turso (hosted SQLite) or local SQLite via libsql.

FIX #1 (Turso URL scheme): the libsql driver understands `libsql://…`;
users often copy `turso://…`, which the driver would treat as a local file
path and blow up. We normalize the scheme before connect().
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional, Sequence

from libsql import connect

from .config import settings

_lock = threading.RLock()
_conn = None


def _normalize_turso_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.startswith("turso://"):
        return "libsql://" + u[len("turso://"):]
    if u.startswith(("libsql://", "https://", "http://", "file:")):
        return u
    return f"file:{u}"


SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS admins (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER NOT NULL UNIQUE,
        username         TEXT,
        first_name       TEXT,
        is_super_admin   INTEGER NOT NULL DEFAULT 0,
        added_by         INTEGER,
        created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        telegram_user_id       INTEGER PRIMARY KEY,
        username               TEXT,
        first_name             TEXT,
        last_name              TEXT,
        joined_at              TEXT,
        last_active_at         TEXT,
        is_banned              INTEGER NOT NULL DEFAULT 0,
        ban_reason             TEXT,
        warn_count             INTEGER NOT NULL DEFAULT 0,
        files_fetched          INTEGER NOT NULL DEFAULT 0,
        files_fetched_today    INTEGER NOT NULL DEFAULT 0,
        last_fetch_day         TEXT,
        last_fetch_at          TEXT,
        sh_verified_until      TEXT,
        sh_files_used          INTEGER NOT NULL DEFAULT 0,
        sh_pending_token       TEXT,
        sh_pending_issued_at   TEXT,
        sh_pending_verified_at TEXT,
        sh_pending_code        TEXT,
        sh_bypass_count        INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_chat_id INTEGER NOT NULL UNIQUE,
        title            TEXT,
        role             TEXT NOT NULL,
        invite_link      TEXT,
        username         TEXT,
        also_post        INTEGER NOT NULL DEFAULT 0,
        also_fsub        INTEGER NOT NULL DEFAULT 0,
        also_backup      INTEGER NOT NULL DEFAULT 0,
        added_by         INTEGER,
        created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        code              TEXT NOT NULL UNIQUE,
        position          INTEGER NOT NULL,
        source_chat_id    INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        main_message_id   INTEGER,
        caption           TEXT,
        media_kind        TEXT NOT NULL,
        file_id           TEXT,
        file_name         TEXT,
        mime_type         TEXT,
        extra_files       TEXT,
        media_group_id    TEXT,
        posted_at         TEXT,
        is_deleted        INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        UNIQUE(source_chat_id, source_message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_position ON posts(position)",
    "CREATE INDEX IF NOT EXISTS idx_posts_code ON posts(code)",
    "CREATE INDEX IF NOT EXISTS idx_posts_queue ON posts(posted_at, position)",
    """
    CREATE TABLE IF NOT EXISTS post_copies (
        post_id        INTEGER NOT NULL,
        target_chat_id INTEGER NOT NULL,
        message_id     INTEGER,
        created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (post_id, target_chat_id)
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS fsub_satisfied (
        user_id         INTEGER NOT NULL,
        channel_chat_id INTEGER NOT NULL,
        satisfied_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (user_id, channel_chat_id)
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS pending_deletions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        delete_at  TEXT NOT NULL,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS scheduled_posts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT NOT NULL,
        post_code     TEXT,
        media         TEXT,
        caption       TEXT,
        scheduled_for TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',
        last_error    TEXT,
        created_by    INTEGER,
        created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        processed_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broadcast_jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        text          TEXT,
        media         TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        scheduled_for TEXT,
        progress      INTEGER NOT NULL DEFAULT 0,
        total         INTEGER NOT NULL DEFAULT 0,
        created_by    INTEGER,
        created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS backfill_jobs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_ids   TEXT NOT NULL,
        from_pos   INTEGER NOT NULL,
        to_pos     INTEGER NOT NULL,
        next_pos   INTEGER NOT NULL,
        posted     INTEGER NOT NULL DEFAULT 0,
        skipped    INTEGER NOT NULL DEFAULT 0,
        failed     INTEGER NOT NULL DEFAULT 0,
        status     TEXT NOT NULL DEFAULT 'running',
        last_error TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
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


def get_conn():
    global _conn
    if _conn is None:
        if settings.turso_database_url:
            url = _normalize_turso_url(settings.turso_database_url)
            _conn = connect(database=url, auth_token=settings.turso_auth_token)
        else:
            path = settings.database_path or "bot.db"
            _conn = connect(database=f"file:{path}", auth_token="")
        _init_schema(_conn)
    return _conn




MIGRATIONS: list[str] = [
    # posts
    "ALTER TABLE posts ADD COLUMN kind TEXT NOT NULL DEFAULT 'cover'",
    "ALTER TABLE posts ADD COLUMN parent_source_message_id INTEGER",
    "ALTER TABLE posts ADD COLUMN post_number INTEGER",
    "ALTER TABLE posts ADD COLUMN main_chat_id INTEGER",
    "ALTER TABLE posts ADD COLUMN published_at TEXT",
    "ALTER TABLE posts ADD COLUMN extra_json TEXT",
    "ALTER TABLE posts ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_posts_kind_number ON posts(kind, post_number)",
    "CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(source_chat_id, parent_source_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_posts_pub ON posts(kind, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_src ON posts(source_chat_id, source_message_id)",
    # channels — missing on old DBs created before these flags existed
    "ALTER TABLE channels ADD COLUMN also_post INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE channels ADD COLUMN also_backup INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE channels ADD COLUMN also_fsub INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE channels ADD COLUMN username TEXT",
    "ALTER TABLE channels ADD COLUMN added_by INTEGER",
    # users — moderation/streak/shortener columns
    "ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN ban_reason TEXT",
    "ALTER TABLE users ADD COLUMN warn_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN files_fetched INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN files_fetched_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_fetch_day TEXT",
    "ALTER TABLE users ADD COLUMN last_fetch_at TEXT",
    "ALTER TABLE users ADD COLUMN sh_verified_until TEXT",
    "ALTER TABLE users ADD COLUMN sh_files_used INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN sh_pending_token TEXT",
    "ALTER TABLE users ADD COLUMN sh_pending_issued_at TEXT",
    "ALTER TABLE users ADD COLUMN sh_pending_verified_at TEXT",
    "ALTER TABLE users ADD COLUMN sh_pending_code TEXT",
    "ALTER TABLE users ADD COLUMN sh_bypass_count INTEGER NOT NULL DEFAULT 0",
]


def _apply_migrations(conn) -> None:
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column/index already exists
    try:
        conn.commit()
    except Exception:
        pass

def _init_schema(conn) -> None:
    for stmt in SCHEMA:
        try:
            conn.execute(stmt)
        except Exception as exc:
            print(f"[db] schema statement failed ({exc}): {stmt[:80]!r}")
    _apply_migrations(conn)
    try:
        conn.commit()
    except Exception:
        pass


def _cols(cur) -> list[str]:
    return [d[0] for d in cur.description] if cur.description else []


def execute(sql: str, params: Sequence = ()) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(sql, params)
        conn.commit()


def insert(sql: str, params: Sequence = ()) -> int:
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


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# ---- tuple-style convenience wrappers used by services/repo.py ----
def fetch_one(sql: str, params: Sequence = ()):
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: Sequence = ()):
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchall() or []


# ---- Turso Hrana disconnect recovery ----------------------------------
def reset_conn() -> None:
    """Drop the cached connection so the next call reconnects fresh.

    Turso Hrana streams expire (`status=404 Not Found, stream not found`)
    when idle for a while. Call this after such an error, then retry.
    """
    global _conn
    with _lock:
        try:
            if _conn is not None:
                _conn.close()
        except Exception:
            pass
        _conn = None


def _is_stream_lost(exc: BaseException) -> bool:
    msg = str(exc)
    return ("stream not found" in msg
            or "Hrana" in msg and "404" in msg
            or "connection" in msg.lower() and "closed" in msg.lower())


def execute_retry(sql: str, params: Sequence = (), attempts: int = 5) -> None:
    """execute() with reconnect-on-stream-lost."""
    last: Optional[BaseException] = None
    for i in range(1, attempts + 1):
        try:
            execute(sql, params)
            return
        except Exception as e:
            last = e
            if not _is_stream_lost(e):
                raise
            reset_conn()
            import time as _t
            _t.sleep(min(2 * i, 10))
    if last:
        raise last


def insert_retry(sql: str, params: Sequence = (), attempts: int = 5) -> int:
    """insert() with reconnect-on-stream-lost. Returns lastrowid."""
    last: Optional[BaseException] = None
    for i in range(1, attempts + 1):
        try:
            return insert(sql, params)
        except Exception as e:
            last = e
            if not _is_stream_lost(e):
                raise
            reset_conn()
            import time as _t
            _t.sleep(min(2 * i, 10))
    if last:
        raise last
    return 0


def query_one_retry(sql: str, params: Sequence = (), attempts: int = 5):
    last = None
    for i in range(1, attempts + 1):
        try:
            return query_one(sql, params)
        except Exception as e:
            last = e
            if not _is_stream_lost(e):
                raise
            reset_conn()
            import time as _t
            _t.sleep(min(2 * i, 10))
    if last:
        raise last
