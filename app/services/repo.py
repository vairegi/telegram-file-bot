"""Data-access layer.

Every function here is engineered to touch as few rows as possible:
  * post_exists()          → INDEX HIT on UNIQUE(source_chat_id, source_message_id)
  * next_queued_cover()    → INDEX HIT on (kind, published_at), LIMIT 1
  * find_cover_before()    → INDEX HIT on (source_chat_id, source_message_id DESC), LIMIT 1
  * pdfs_of_cover()        → INDEX HIT on (source_chat_id, parent_source_message_id)
  * get_setting_*()        → in-memory cache with 60s TTL (invalidated on write)
  * get_*_channels()       → in-memory cache with 60s TTL (invalidated on write)

The two caches together drop chatty hot paths (webhook classifier, scheduler
tick, /queueinfo poll, /health) from N reads per request to zero.
"""
from __future__ import annotations

import json
import time
from typing import Any, List, Optional, Tuple

from ..db import execute, executemany, insert, query_all, query_one, query_scalar
from ..utils import now_iso, random_code


# ============================================================================
# TTL cache — 60s. Manual invalidation on writes.
# ============================================================================
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


def _cache_invalidate(*prefixes: str) -> None:
    if not prefixes:
        _cache.clear()
        return
    for k in list(_cache.keys()):
        if any(k.startswith(p) for p in prefixes):
            _cache.pop(k, None)


# ============================================================================
# Settings — cached
# ============================================================================
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    ck = f"setting:{key}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached if cached != "__NONE__" else default
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    val = row["value"] if row else None
    _cache_set(ck, val if val is not None else "__NONE__")
    return val if val is not None else default


def set_setting(key: str, value: Optional[str]) -> None:
    if value is None:
        execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    _cache_invalidate(f"setting:{key}")


def get_setting_bool(key: str, default: bool = False) -> bool:
    v = get_setting(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def get_setting_int(key: str, default: int = 0) -> int:
    v = get_setting(key)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def get_setting_json(key: str, default: Any = None) -> Any:
    v = get_setting(key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def set_setting_json(key: str, value: Any) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False))


# ============================================================================
# Cursor (per DB channel, stored in settings) — cached
# ============================================================================
def get_cursor(db_chat_id: int) -> int:
    return get_setting_int(f"cursor:{db_chat_id}", 0)


def set_cursor(db_chat_id: int, message_id: int) -> None:
    set_setting(f"cursor:{db_chat_id}", str(int(message_id)))


# ============================================================================
# Channels — cached
# ============================================================================
def add_channel(chat_id: int, role: str, title: Optional[str] = None) -> None:
    execute(
        "INSERT INTO channels(chat_id, role, title) VALUES(?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET role = excluded.role, "
        "title = COALESCE(excluded.title, channels.title)",
        (chat_id, role, title),
    )
    _cache_invalidate("channels:")


def remove_channel(chat_id: int) -> None:
    execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
    _cache_invalidate("channels:")


def get_channel(chat_id: int) -> Optional[dict]:
    return query_one("SELECT chat_id, role, title FROM channels WHERE chat_id = ?",
                     (chat_id,))


def _channels_by_role(role: str) -> List[dict]:
    ck = f"channels:{role}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    rows = query_all(
        "SELECT chat_id, role, title FROM channels WHERE role = ? ORDER BY chat_id",
        (role,))
    _cache_set(ck, rows)
    return rows


def get_database_channels() -> List[dict]:
    return _channels_by_role("database")


def get_main_channels() -> List[dict]:
    return _channels_by_role("main")


def get_log_channel() -> Optional[dict]:
    rows = _channels_by_role("log")
    return rows[0] if rows else None


def get_backup_channels() -> List[dict]:
    return _channels_by_role("backup")


def database_chat_ids() -> set:
    """Fast in-memory set for the webhook classifier. Cached."""
    ck = "channels:db_ids_set"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    ids = {int(c["chat_id"]) for c in get_database_channels()}
    _cache_set(ck, ids)
    return ids


def list_all_channels() -> List[dict]:
    return query_all("SELECT chat_id, role, title FROM channels ORDER BY role, chat_id")


# ============================================================================
# Admins
# ============================================================================
def is_admin(user_id: int) -> bool:
    ck = f"admin:{user_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return bool(cached)
    row = query_one("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    val = row is not None
    _cache_set(ck, val)
    return val


def is_super_admin(user_id: int) -> bool:
    ck = f"super:{user_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return bool(cached)
    row = query_one("SELECT is_super FROM admins WHERE user_id = ?", (user_id,))
    val = bool(row and int(row.get("is_super") or 0) == 1)
    _cache_set(ck, val)
    return val


def add_admin(user_id: int, is_super: bool = False) -> None:
    execute(
        "INSERT INTO admins(user_id, is_super) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET is_super = excluded.is_super",
        (user_id, 1 if is_super else 0),
    )
    _cache_invalidate(f"admin:{user_id}", f"super:{user_id}")


def remove_admin(user_id: int) -> None:
    execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    _cache_invalidate(f"admin:{user_id}", f"super:{user_id}")


def list_admins() -> List[dict]:
    return query_all("SELECT user_id, is_super, added_at FROM admins ORDER BY user_id")


# ============================================================================
# Posts — the hot table
# ============================================================================
def post_exists(source_chat_id: int, source_message_id: int) -> bool:
    """INDEX HIT on UNIQUE(source_chat_id, source_message_id)."""
    return query_one(
        "SELECT 1 FROM posts WHERE source_chat_id = ? AND source_message_id = ? LIMIT 1",
        (source_chat_id, source_message_id),
    ) is not None


def insert_cover(source_chat_id: int, source_message_id: int, caption: Optional[str],
                 media_kind: str, file_id: Optional[str], file_name: Optional[str],
                 mime_type: Optional[str] = None) -> Optional[int]:
    """Return new post id, or None if duplicate."""
    try:
        pid = insert(
            "INSERT OR IGNORE INTO posts"
            "(code, kind, media_kind, source_chat_id, source_message_id, "
            " caption, file_id, file_name, mime_type) "
            "VALUES(?, 'cover', ?, ?, ?, ?, ?, ?, ?)",
            (random_code(8), media_kind, source_chat_id, source_message_id,
             caption, file_id, file_name, mime_type),
        )
        return pid or None
    except Exception:
        return None


def insert_file(source_chat_id: int, source_message_id: int,
                parent_msg_id: int, caption: Optional[str],
                media_kind: str, file_id: Optional[str],
                file_name: Optional[str], mime_type: Optional[str] = None
                ) -> Optional[int]:
    try:
        pid = insert(
            "INSERT OR IGNORE INTO posts"
            "(code, kind, media_kind, source_chat_id, source_message_id, "
            " parent_source_message_id, caption, file_id, file_name, mime_type) "
            "VALUES(?, 'file', ?, ?, ?, ?, ?, ?, ?, ?)",
            (random_code(8), media_kind, source_chat_id, source_message_id,
             parent_msg_id, caption, file_id, file_name, mime_type),
        )
        return pid or None
    except Exception:
        return None


def insert_batch(rows: list[tuple]) -> int:
    """Batched insert for backfill. Each row is:
    (kind, media_kind, source_chat_id, source_message_id, parent_msg_id,
     caption, file_id, file_name, mime_type)
    """
    if not rows:
        return 0
    payload = [(random_code(8), *r) for r in rows]
    return executemany(
        "INSERT OR IGNORE INTO posts"
        "(code, kind, media_kind, source_chat_id, source_message_id, "
        " parent_source_message_id, caption, file_id, file_name, mime_type) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )


def get_post_by_id(pid: int) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE id = ?", (pid,))


def get_post_by_code(code: str) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE code = ?", (code,))


def get_post_by_number(n: int) -> Optional[dict]:
    return query_one("SELECT * FROM posts WHERE post_number = ? AND kind = 'cover'", (n,))


def find_cover_before(source_chat_id: int, upto_msg_id: int) -> Optional[dict]:
    """Nearest cover with source_message_id <= upto_msg_id."""
    return query_one(
        "SELECT * FROM posts WHERE kind = 'cover' AND source_chat_id = ? "
        "AND source_message_id <= ? "
        "ORDER BY source_message_id DESC LIMIT 1",
        (source_chat_id, upto_msg_id),
    )


def files_of_cover(source_chat_id: int, cover_msg_id: int) -> List[dict]:
    """INDEX HIT on (source_chat_id, parent_source_message_id)."""
    return query_all(
        "SELECT * FROM posts WHERE kind = 'file' "
        "AND source_chat_id = ? AND parent_source_message_id = ? "
        "ORDER BY source_message_id ASC",
        (source_chat_id, cover_msg_id),
    )


# ---------- Queue ----------
def next_queued_cover() -> Optional[dict]:
    """The single next cover to publish. INDEX HIT."""
    return query_one(
        "SELECT * FROM posts WHERE kind = 'cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT 1"
    )


def next_queued_covers(limit: int = 10) -> List[dict]:
    return query_all(
        "SELECT id, code, source_chat_id, source_message_id, caption "
        "FROM posts WHERE kind = 'cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT ?",
        (int(limit),),
    )


def queued_cover_count() -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover' AND published_at IS NULL", (), 0
    ) or 0)


def published_cover_count() -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover' AND published_at IS NOT NULL", (), 0
    ) or 0)


def total_cover_count() -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover'", (), 0
    ) or 0)


def total_file_count() -> int:
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'file'", (), 0
    ) or 0)


def highest_post_number() -> int:
    return int(query_scalar(
        "SELECT COALESCE(MAX(post_number), 0) FROM posts WHERE kind = 'cover'", (), 0
    ) or 0)


def next_post_number() -> int:
    return highest_post_number() + 1


def predicted_number_of_next(limit: int = 10) -> List[dict]:
    """Return the next `limit` queued covers with their predicted #N."""
    base = highest_post_number()
    rows = next_queued_covers(limit)
    out = []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["predicted_number"] = base + i
        out.append(r)
    return out


def mark_published(post_id: int, main_chat_id: int, main_message_id: int,
                   file_id: Optional[str] = None) -> int:
    """Assign the next #N atomically, stamp published_at, cache file_id.

    Uses a single UPDATE with a subquery so we never race between reading MAX
    and writing our own number.
    """
    execute(
        "UPDATE posts SET "
        "  post_number = COALESCE(post_number, "
        "                          (SELECT COALESCE(MAX(post_number),0)+1 "
        "                           FROM posts WHERE kind='cover')), "
        "  published_at = COALESCE(published_at, ?), "
        "  main_chat_id = COALESCE(main_chat_id, ?), "
        "  main_message_id = COALESCE(main_message_id, ?), "
        "  file_id = COALESCE(?, file_id) "
        "WHERE id = ?",
        (now_iso(), main_chat_id, main_message_id, file_id, post_id),
    )
    row = query_one("SELECT post_number FROM posts WHERE id = ?", (post_id,))
    return int((row or {}).get("post_number") or 0)


def unpublish(post_id: int) -> None:
    execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, main_message_id = NULL, "
        "post_number = NULL WHERE id = ?", (post_id,))


def update_file_id(post_id: int, file_id: str) -> None:
    execute("UPDATE posts SET file_id = ? WHERE id = ?", (file_id, post_id))


# ---------- Queue-control commands ----------
def skip_first_n(n: int, main_chat_id: int) -> int:
    """Mark the next `n` pending covers as skipped (post_number assigned so
    the queue believes they've been posted). Used by /skip #N."""
    rows = query_all(
        "SELECT id FROM posts WHERE kind='cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT ?",
        (int(n),),
    )
    if not rows:
        return 0
    stamp = now_iso()
    base = highest_post_number()
    payload = [
        (base + i + 1, stamp, main_chat_id, 0, r["id"])
        for i, r in enumerate(rows)
    ]
    executemany(
        "UPDATE posts SET post_number = ?, published_at = ?, "
        "main_chat_id = ?, main_message_id = ? WHERE id = ?",
        payload,
    )
    return len(rows)


def skip_up_to_source(source_chat_id: int, upto_msg_id: int,
                      main_chat_id: int) -> int:
    """Mark every pending cover with source_message_id <= upto_msg_id as
    published-skipped. Used by /skip <link>."""
    rows = query_all(
        "SELECT id FROM posts WHERE kind='cover' AND published_at IS NULL "
        "AND source_chat_id = ? AND source_message_id <= ? "
        "ORDER BY source_chat_id ASC, source_message_id ASC",
        (source_chat_id, upto_msg_id),
    )
    if not rows:
        return 0
    stamp = now_iso()
    base = highest_post_number()
    payload = [
        (base + i + 1, stamp, main_chat_id, 0, r["id"])
        for i, r in enumerate(rows)
    ]
    executemany(
        "UPDATE posts SET post_number = ?, published_at = ?, "
        "main_chat_id = ?, main_message_id = ? WHERE id = ?",
        payload,
    )
    return len(rows)


def unskip_by_number(n: int) -> Optional[dict]:
    """Reverse of /skip for a specific #N — returns the affected row."""
    row = get_post_by_number(n)
    if not row:
        return None
    unpublish(int(row["id"]))
    return row


def jumpto_number(n: int) -> int:
    """Force queue cursor back to #N: unpublish #N and every #M > N.

    Used by /jumpto #N. Returns number of rows reset.
    """
    return execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, "
        "main_message_id = NULL, post_number = NULL "
        "WHERE kind = 'cover' AND post_number IS NOT NULL AND post_number >= ?",
        (int(n),),
    )


def queue_reset() -> int:
    """Nuclear: unpublish EVERY cover so drip starts from #1 again."""
    return execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, "
        "main_message_id = NULL, post_number = NULL WHERE kind = 'cover'"
    )


def delete_post_by_number(n: int) -> bool:
    """Soft-delete via kind='skip' so audit is preserved."""
    row = get_post_by_number(n)
    if not row:
        return False
    execute("UPDATE posts SET kind = 'skip' WHERE id = ?", (int(row["id"]),))
    return True


def delete_post_by_code(code: str) -> bool:
    row = get_post_by_code(code)
    if not row:
        return False
    execute("UPDATE posts SET kind = 'skip' WHERE id = ?", (int(row["id"]),))
    return True


# ---------- Search ----------
def find_by_caption(pattern: str, limit: int = 20) -> List[dict]:
    like = f"%{pattern}%"
    return query_all(
        "SELECT id, post_number, code, source_chat_id, source_message_id, caption "
        "FROM posts WHERE kind = 'cover' AND caption LIKE ? "
        "ORDER BY COALESCE(post_number, 999999) ASC LIMIT ?",
        (like, int(limit)),
    )


# ---------- Favorites ----------
def add_favorite(user_id: int, post_id: int) -> None:
    execute(
        "INSERT OR IGNORE INTO favorites(user_id, post_id) VALUES(?, ?)",
        (user_id, post_id),
    )


def remove_favorite(user_id: int, post_id: int) -> None:
    execute("DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id))


def list_favorites(user_id: int) -> List[dict]:
    """Return the user's saved posts resolved to their parent COVER.

    Users save individual FILE posts (the Save button lives on each delivered
    PDF/CBZ), but /favs must show the COVER's title + code + post_number so
    the entry links back to the full pack (cover + all its files).
    Each row returned is the cover row plus fav_post_id (the saved file's id).
    """
    return query_all(
        """SELECT c.*, f.post_id AS fav_post_id, f.saved_at
           FROM favorites f
           JOIN posts p  ON p.id = f.post_id
           JOIN posts c  ON c.kind = 'cover'
                        AND c.source_chat_id    = p.source_chat_id
                        AND c.source_message_id = COALESCE(
                              CASE WHEN p.kind = 'file'
                                   THEN p.parent_source_message_id END,
                              p.source_message_id)
           WHERE f.user_id = ?
           GROUP BY c.id                       -- one row per cover even if
           ORDER BY MAX(f.saved_at) DESC       -- user saved 3 files of it
           LIMIT 100""",
        (user_id,),
    )


def is_favorite(user_id: int, post_id: int) -> bool:
    return query_one(
        "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ? LIMIT 1",
        (user_id, post_id),
    ) is not None


# ---------- Cache management (exposed for /debug) ----------
def cache_stats() -> dict:
    return {"entries": len(_cache), "ttl_seconds": _CACHE_TTL}


def cache_flush() -> None:
    _cache_invalidate()


def remove_favorites_for_cover(user_id: int, source_chat_id: int,
                               cover_msg_id: int) -> int:
    """Delete every favorite of this user that belongs to the given cover
    (the cover itself, plus any of its files)."""
    return execute(
        """DELETE FROM favorites
           WHERE user_id = ?
             AND post_id IN (
                 SELECT id FROM posts
                 WHERE source_chat_id = ?
                   AND (source_message_id = ?
                        OR parent_source_message_id = ?))""",
        (user_id, source_chat_id, cover_msg_id, cover_msg_id),
    )


def update_channel_title(chat_id: int, title: str) -> None:
    execute("UPDATE channels SET title = ? WHERE chat_id = ?", (title, chat_id))
    _cache_invalidate("channels:")
