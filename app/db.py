"""Turso / libsql layer — self-initialising schema, self-healing connection.

Design goals:
  * Auto-create every table + index on first boot (no manual SQL in dashboard).
  * Single lazy connection, reconnect-on-stream-lost with exponential backoff.
  * Thread-safe (aiohttp handlers may run concurrently).
  * All indexed on the columns v2 actually queries so the free-tier read budget
    is measured in thousands per day, not millions.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Optional, Sequence

from libsql import connect

from .config import settings

log = logging.getLogger("db")

_lock = threading.RLock()
_conn = None


# =============================================================================
# Schema — every table has appropriate indexes; posts UNIQUE(source) blocks
# duplicates without a SELECT.
# =============================================================================
SCHEMA_STATEMENTS: list[str] = [
    # posts: covers, files (pdf/cbz/sticker), skipped rows kept for audit
    """
    CREATE TABLE IF NOT EXISTS posts (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        code                     TEXT    NOT NULL UNIQUE,
        kind                     TEXT    NOT NULL CHECK(kind IN ('cover','file','skip')),
        media_kind               TEXT    NOT NULL,
        source_chat_id           INTEGER NOT NULL,
        source_message_id        INTEGER NOT NULL,
        parent_source_message_id INTEGER,
        caption                  TEXT,
        file_id                  TEXT,
        file_name                TEXT,
        mime_type                TEXT,
        post_number              INTEGER,
        published_at             TEXT,
        main_chat_id             INTEGER,
        main_message_id          INTEGER,
        created_at               TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        UNIQUE(source_chat_id, source_message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_kind_pub    ON posts(kind, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_parent      ON posts(source_chat_id, parent_source_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_posts_number      ON posts(post_number)",
    "CREATE INDEX IF NOT EXISTS idx_posts_source_msg  ON posts(source_chat_id, source_message_id)",

    """
    CREATE TABLE IF NOT EXISTS channels (
        chat_id  INTEGER PRIMARY KEY,
        role     TEXT NOT NULL CHECK(role IN ('database','main','log','backup')),
        title    TEXT,
        added_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_channels_role ON channels(role)",

    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS admins (
        user_id   INTEGER PRIMARY KEY,
        is_super  INTEGER NOT NULL DEFAULT 0,
        added_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS favorites (
        user_id  INTEGER NOT NULL,
        post_id  INTEGER NOT NULL,
        saved_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (user_id, post_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id)",

    """
    CREATE TABLE IF NOT EXISTS backup_progress (
        backup_chat_id     INTEGER NOT NULL,
        db_chat_id         INTEGER NOT NULL,
        source_message_id  INTEGER NOT NULL,
        target_message_id  INTEGER,
        mirrored_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY (backup_chat_id, db_chat_id, source_message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_backup_lookup ON backup_progress(backup_chat_id, db_chat_id, source_message_id)",

    """
    CREATE TABLE IF NOT EXISTS backup_history (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_chat_id     INTEGER NOT NULL,
        db_chat_id         INTEGER NOT NULL,
        source_message_id  INTEGER NOT NULL,
        target_message_id  INTEGER,
        reset_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS user_directory (
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        first_name TEXT,
        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
]


def _normalize_turso_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("turso://"):
        return "libsql://" + u[len("turso://"):]
    if u.startswith(("libsql://", "https://", "http://", "file:")):
        return u
    return f"file:{u}"


def _open() -> Any:
    url = _normalize_turso_url(settings.turso_database_url)
    if not url:
        raise RuntimeError("TURSO_DATABASE_URL is not set")
    kwargs = {}
    if settings.turso_auth_token and url.startswith(("libsql://", "https://")):
        kwargs["auth_token"] = settings.turso_auth_token
    return connect(url, **kwargs)


def _get_conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = _open()
        return _conn


def reset_conn() -> None:
    """Force reconnect on next call (used by retry wrappers)."""
    global _conn
    with _lock:
        try:
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:
                    pass
        finally:
            _conn = None


def _is_stream_lost(exc: BaseException) -> bool:
    s = str(exc).lower()
    return ("stream not found" in s
            or ("hrana" in s and "404" in s)
            or ("connection" in s and ("close" in s or "reset" in s))
            or ("reset" in s and "peer" in s))


def init_schema() -> None:
    """Run all CREATE TABLE / CREATE INDEX statements. Idempotent."""
    with _lock:
        c = _get_conn()
        cur = c.cursor()
        for stmt in SCHEMA_STATEMENTS:
            try:
                cur.execute(stmt)
            except Exception as e:
                log.warning("schema init: %s — %s", stmt.split()[2] if len(stmt.split()) > 2 else "?", e)
        try:
            c.commit()
        except Exception:
            pass
        log.info("Turso schema initialised (%d statements)", len(SCHEMA_STATEMENTS))


# =============================================================================
# Query primitives — reconnect-on-stream-lost, all return dicts.
# =============================================================================
def _rows_to_dicts(cur, rows: Iterable) -> list[dict]:
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in rows]


def _run(sql: str, params: Sequence = (), *, attempts: int = 4):
    last: Optional[BaseException] = None
    for i in range(1, attempts + 1):
        try:
            with _lock:
                c = _get_conn()
                cur = c.cursor()
                cur.execute(sql, tuple(params))
                return cur
        except Exception as e:
            last = e
            if not _is_stream_lost(e):
                raise
            log.warning("stream lost, reconnecting (attempt %s/%s)", i, attempts)
            reset_conn()
            time.sleep(min(2 * i, 8))
    if last:
        raise last
    raise RuntimeError("db: unknown failure")


def execute(sql: str, params: Sequence = ()) -> int:
    """Run a write. Returns rowcount."""
    cur = _run(sql, params)
    try:
        with _lock:
            _get_conn().commit()
    except Exception:
        pass
    try:
        return cur.rowcount
    except Exception:
        return 0


def executemany(sql: str, seq: Iterable[Sequence]) -> int:
    """Batched write in a single transaction."""
    seq = list(seq)
    if not seq:
        return 0
    last: Optional[BaseException] = None
    for i in range(1, 5):
        try:
            with _lock:
                c = _get_conn()
                cur = c.cursor()
                cur.executemany(sql, [tuple(p) for p in seq])
                c.commit()
                return len(seq)
        except Exception as e:
            last = e
            if not _is_stream_lost(e):
                raise
            reset_conn()
            time.sleep(min(2 * i, 8))
    if last:
        raise last
    return 0


def insert(sql: str, params: Sequence = ()) -> int:
    """INSERT and return lastrowid. Returns 0 when an INSERT OR IGNORE was a
    no-op (duplicate) — lastrowid is stale/unreliable in that case, so we
    check rowcount instead."""
    cur = _run(sql, params)
    try:
        with _lock:
            _get_conn().commit()
    except Exception:
        pass
    try:
        if cur.rowcount == 0:
            return 0  # ignored duplicate
        return int(cur.lastrowid or 0)
    except Exception:
        return 0


def query_one(sql: str, params: Sequence = ()) -> Optional[dict]:
    cur = _run(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in (cur.description or [])]
    return dict(zip(cols, row))


def query_all(sql: str, params: Sequence = ()) -> list[dict]:
    cur = _run(sql, params)
    rows = cur.fetchall() or []
    return _rows_to_dicts(cur, rows)


def query_scalar(sql: str, params: Sequence = (), default=None):
    row = query_one(sql, params)
    if row is None:
        return default
    return next(iter(row.values()), default)
