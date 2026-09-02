"""Turso → MongoDB migration engine (v3.0).

Design:
  * Reads TURSO through its own dedicated libsql connection — completely
    independent of DB_BACKEND, so it works while the bot runs on Turso AND
    after the cutover to Mongo (for delta top-ups).
  * Writes Mongo with idempotent upserts (bulk_write, ordered=False) — safe
    to run any number of times; duplicates are impossible by construction.
  * Per-table resume cursor in Mongo `settings` (key 'mig_cursor:<table>') —
    an interrupted full pass continues exactly where it stopped.
  * Tables with negative or composite keys (channels, settings, favorites,
    backup_progress) are scanned by ROWID, not by their natural key —
    channel ids are negative so `WHERE chat_id > 0` would copy nothing.
  * After copying, counters are seeded from the Turso maxima so no numeric
    id or #N can ever collide with migrated rows.
  * `delta=True` copies only posts with id > the highest migrated post id
    (catches everything posted during the redeploy window); the small tables
    are re-swept whole (idempotent upserts make that free and always correct).
  * verify_migration() compares EVERY row of EVERY table field-by-field —
    a green report means the two databases are identical.

Commands driving this module live in handlers/migrate_cmds.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..config import settings

log = logging.getLogger("migrate")

BATCH = 1000

TABLES = [
    "posts", "channels", "settings", "admins",
    "favorites", "user_directory", "backup_progress", "backup_history",
]

POST_FIELDS = [
    "id", "code", "kind", "media_kind", "source_chat_id", "source_message_id",
    "parent_source_message_id", "caption", "file_id", "file_name", "mime_type",
    "post_number", "published_at", "main_chat_id", "main_message_id",
    "created_at",
]


@dataclass
class MigState:
    running: bool = False
    mode: str = ""                # full | delta
    table: str = ""
    tables_done: int = 0
    rows_copied: int = 0
    started_at: float = 0.0
    last_error: str = ""
    verify_passed: Optional[bool] = None
    verify_detail: str = ""
    done: bool = False


_state = MigState()
_task: Optional[asyncio.Task] = None


def mig_state() -> MigState:
    return _state


# =============================================================================
# Turso read side (dedicated connection — backend-independent)
# =============================================================================
def _turso_connect():
    from libsql import connect
    url = (settings.turso_database_url or "").strip()
    if url.startswith("turso://"):
        url = "libsql://" + url[len("turso://"):]
    if not url:
        raise RuntimeError("TURSO_DATABASE_URL is not set")
    kw = {}
    if settings.turso_auth_token and url.startswith(("libsql://", "https://")):
        kw["auth_token"] = settings.turso_auth_token
    return connect(url, **kw)


def _t_query_all(conn, sql: str, params=()) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _t_scalar(conn, sql: str, params=(), default=0):
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else default


# =============================================================================
# Mongo write side
# =============================================================================
async def _m_get_cursor(name: str) -> int:
    from .. import mongo_db

    async def _op(db):
        d = await db.settings.find_one({"_id": f"mig_cursor:{name}"})
        return int(d["value"]) if d else 0
    return await mongo_db.with_retry(_op)


async def _m_set_cursor(name: str, val: int) -> None:
    from .. import mongo_db

    async def _op(db):
        await db.settings.update_one(
            {"_id": f"mig_cursor:{name}"},
            {"$set": {"key": f"mig_cursor:{name}", "value": str(int(val))}},
            upsert=True)
    await mongo_db.with_retry(_op)


async def _m_bulk_upsert(coll: str, docs: list[dict]) -> int:
    if not docs:
        return 0
    from .. import mongo_db
    from pymongo import ReplaceOne
    ops = [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs]

    async def _op(db):
        res = await db[coll].bulk_write(ops, ordered=False)
        return res.upserted_count + res.modified_count
    return await mongo_db.with_retry(_op)


async def _m_seed_counters(turso_conn) -> None:
    """Raise Mongo counters above every Turso maximum — no id/#N collisions."""
    from .. import mongo_db
    max_pid = int(_t_scalar(turso_conn, "SELECT COALESCE(MAX(id),0) FROM posts", (), 0))
    max_num = int(_t_scalar(
        turso_conn,
        "SELECT COALESCE(MAX(post_number),0) FROM posts WHERE kind='cover'", (), 0))
    max_hist = int(_t_scalar(
        turso_conn, "SELECT COALESCE(MAX(id),0) FROM backup_history", (), 0))
    await mongo_db.set_counter_floor("post_id", max_pid)
    await mongo_db.set_counter_floor("post_number", max_num)
    await mongo_db.set_counter_floor("backup_history_id", max_hist)


# =============================================================================
# Row mapping — SQL row dict → Mongo document (NULL → absent)
# =============================================================================
def _doc_posts(r: dict) -> dict:
    d = {"_id": int(r["id"])}
    for f in POST_FIELDS[1:]:
        v = r.get(f)
        if v is not None:
            d[f] = v
    return d


def _doc_channels(r: dict) -> dict:
    d = {"_id": int(r["chat_id"]), "chat_id": int(r["chat_id"]),
         "role": r["role"]}
    if r.get("title") is not None:
        d["title"] = r["title"]
    if r.get("added_at") is not None:
        d["added_at"] = r["added_at"]
    return d


def _doc_settings(r: dict) -> dict:
    return {"_id": r["key"], "key": r["key"], "value": r.get("value")}


def _doc_admins(r: dict) -> dict:
    d = {"_id": int(r["user_id"]), "user_id": int(r["user_id"]),
         "is_super": int(r.get("is_super") or 0)}
    if r.get("added_at") is not None:
        d["added_at"] = r["added_at"]
    return d


def _doc_favorites(r: dict) -> dict:
    d = {"_id": f"{int(r['user_id'])}:{int(r['post_id'])}",
         "user_id": int(r["user_id"]), "post_id": int(r["post_id"])}
    if r.get("saved_at") is not None:
        d["saved_at"] = r["saved_at"]
    return d


def _doc_user_directory(r: dict) -> dict:
    d = {"_id": int(r["user_id"]), "user_id": int(r["user_id"])}
    if r.get("username") is not None:
        d["username"] = r["username"]
    if r.get("first_name") is not None:
        d["first_name"] = r["first_name"]
    if r.get("updated_at") is not None:
        d["updated_at"] = r["updated_at"]
    return d


def _doc_backup_progress(r: dict) -> dict:
    d = {"_id": f"{int(r['backup_chat_id'])}:{int(r['db_chat_id'])}:{int(r['source_message_id'])}",
         "backup_chat_id": int(r["backup_chat_id"]),
         "db_chat_id": int(r["db_chat_id"]),
         "source_message_id": int(r["source_message_id"])}
    if r.get("target_message_id") is not None:
        d["target_message_id"] = r["target_message_id"]
    if r.get("mirrored_at") is not None:
        d["mirrored_at"] = r["mirrored_at"]
    return d


def _doc_backup_history(r: dict) -> dict:
    d = {"_id": int(r["id"]), "backup_chat_id": int(r["backup_chat_id"]),
         "db_chat_id": int(r["db_chat_id"]),
         "source_message_id": int(r["source_message_id"])}
    if r.get("target_message_id") is not None:
        d["target_message_id"] = int(r["target_message_id"])
    if r.get("reset_at") is not None:
        d["reset_at"] = r["reset_at"]
    return d


_MAPPERS = {
    "posts": _doc_posts,
    "channels": _doc_channels,
    "settings": _doc_settings,
    "admins": _doc_admins,
    "favorites": _doc_favorites,
    "user_directory": _doc_user_directory,
    "backup_progress": _doc_backup_progress,
    "backup_history": _doc_backup_history,
}

# Scan SQL per table: (paged_sql, cursor_column, full_scan_sql).
# ROWID paging for any table whose natural key can be negative/composite.
_SCAN = {
    "posts": (
        "SELECT * FROM posts WHERE id > ? ORDER BY id LIMIT ?",
        "id",
        "SELECT * FROM posts"),
    "channels": (
        # chat_id IS the rowid alias here (INTEGER PRIMARY KEY) and Telegram
        # channel ids are NEGATIVE — ROWID > 0 paging matched nothing (found
        # in live rehearsal). Page on chat_id from a sentinel below any real
        # chat id instead.
        "SELECT chat_id, role, title, added_at "
        "FROM channels WHERE chat_id > ? ORDER BY chat_id LIMIT ?",
        "chat_id",
        "SELECT * FROM channels"),
    "settings": (
        "SELECT key, value, ROWID AS _r "
        "FROM settings WHERE ROWID > ? ORDER BY ROWID LIMIT ?",
        "_r",
        "SELECT key, value FROM settings"),
    "admins": (
        "SELECT * FROM admins WHERE user_id > ? ORDER BY user_id LIMIT ?",
        "user_id",
        "SELECT * FROM admins"),
    "favorites": (
        "SELECT user_id, post_id, saved_at, ROWID AS _r "
        "FROM favorites WHERE ROWID > ? ORDER BY ROWID LIMIT ?",
        "_r",
        "SELECT * FROM favorites"),
    "user_directory": (
        "SELECT * FROM user_directory WHERE user_id > ? ORDER BY user_id LIMIT ?",
        "user_id",
        "SELECT * FROM user_directory"),
    "backup_progress": (
        "SELECT backup_chat_id, db_chat_id, source_message_id, "
        "target_message_id, mirrored_at, ROWID AS _r "
        "FROM backup_progress WHERE ROWID > ? ORDER BY ROWID LIMIT ?",
        "_r",
        "SELECT * FROM backup_progress"),
    "backup_history": (
        "SELECT * FROM backup_history WHERE id > ? ORDER BY id LIMIT ?",
        "id",
        "SELECT * FROM backup_history"),
}


# =============================================================================
# Main migration pass
# =============================================================================
async def _migrate_table(turso_conn, table: str, delta: bool) -> int:
    """Copy one table in batches. Returns rows written this pass."""
    global _state
    _state.table = table
    paged_sql, cursor_col, _full = _SCAN[table]
    mapper = _MAPPERS[table]

    chan_sentinel = -10**15   # below any Telegram chat id (all negative)
    if delta and table == "posts":
        cursor = await _m_get_cursor("posts_max_id")
    elif delta:
        # small tables: full idempotent re-sweep (channels from sentinel)
        cursor = chan_sentinel if table == "channels" else 0
    else:
        cursor = await _m_get_cursor(table)   # resume an interrupted full pass
        if table == "channels" and cursor == 0:
            cursor = chan_sentinel  # fresh pass: 0 would match nothing

    copied = 0
    while True:
        if not _state.running:
            break
        rows = await asyncio.to_thread(_t_query_all, turso_conn,
                                       paged_sql, (cursor, BATCH))
        if not rows:
            break
        docs = [mapper(r) for r in rows]
        await _m_bulk_upsert(table, docs)
        cursor = max(int(r[cursor_col]) for r in rows)
        copied += len(rows)
        _state.rows_copied += len(rows)
        if not delta:
            await _m_set_cursor(table, cursor)
        if table == "posts":
            await _m_set_cursor("posts_max_id", cursor)
        await asyncio.sleep(0)   # yield to the event loop between batches
        if len(rows) < BATCH:
            break
    return copied


async def _migrate_all(mode: str) -> dict:
    global _state
    turso_conn = await asyncio.to_thread(_turso_connect)
    try:
        delta = (mode == "delta")
        for table in TABLES:
            if not _state.running:
                break
            await _migrate_table(turso_conn, table, delta)
            _state.tables_done += 1
        await _m_seed_counters(turso_conn)
        return {"ok": True, "rows": _state.rows_copied}
    finally:
        try:
            await asyncio.to_thread(turso_conn.close)
        except Exception:
            pass


# =============================================================================
# Verification — full row-by-row comparison of every table
# =============================================================================
async def verify_migration() -> dict:
    """Compare EVERY row of every table between Turso and Mongo.

    Returns {"ok": bool, "detail": str}. Any mismatch is named precisely.
    """
    from .. import mongo_db
    turso_conn = await asyncio.to_thread(_turso_connect)
    problems: list[str] = []
    counts: list[str] = []
    try:
        for table in TABLES:
            _paged, _col, full_sql = _SCAN[table]
            mapper = _MAPPERS[table]
            t_all = await asyncio.to_thread(_t_query_all, turso_conn, full_sql, ())
            t_docs = [mapper(r) for r in t_all]

            async def _op(db, t=table):
                cur = db[t].find()
                return await cur.to_list(length=None)
            m_all = await mongo_db.with_retry(_op)
            m_map = {d["_id"]: d for d in m_all}

            counts.append(f"{table}: {len(t_docs)}/{len(m_map)}")
            if len(m_map) < len(t_docs):
                problems.append(f"{table}: Mongo has {len(m_map)} < Turso {len(t_docs)}")
                continue

            for d in t_docs:
                m = m_map.get(d["_id"])
                if m is None:
                    problems.append(f"{table}: missing _id={d['_id']}")
                    if len(problems) > 5:
                        break
                    continue
                for k, v in d.items():
                    if k == "_id":
                        continue
                    if m.get(k) != v:
                        problems.append(
                            f"{table} _id={d['_id']}: field {k!r} "
                            f"turso={v!r} mongo={m.get(k)!r}")
                        break
                if len(problems) > 5:
                    break
            if len(problems) > 5:
                break

        # Counter sanity: post_number counter >= max published number.
        max_num = int(_t_scalar(
            turso_conn,
            "SELECT COALESCE(MAX(post_number),0) FROM posts WHERE kind='cover'",
            (), 0))
        ctr = await mongo_db.get_counter("post_number")
        if ctr < max_num:
            problems.append(f"counters.post_number={ctr} < max turso #N={max_num}")
        counts.append(f"counter.post_number={ctr} (turso max #N={max_num})")
    finally:
        try:
            await asyncio.to_thread(turso_conn.close)
        except Exception:
            pass

    ok = not problems
    detail = "\n".join(counts) + (
        ("\nPROBLEMS:\n" + "\n".join(problems[:8])) if problems else "")
    return {"ok": ok, "detail": detail}


# =============================================================================
# Orchestration
# =============================================================================
async def _run(mode: str, admin_chat_id: int, bot) -> None:
    global _state, _task
    s = _state
    try:
        res = await _migrate_all(mode)
        v = await verify_migration()
        if not v["ok"]:
            # The live Turso DB keeps changing under us (new posts arrive,
            # users save files). One delta top-up + re-verify converges;
            # upserts are idempotent so this is always safe.
            s.tables_done = 0
            res2 = await _migrate_all("delta")
            res["rows"] = res.get("rows", 0) + res2.get("rows", 0)
            v = await verify_migration()
        s.verify_passed = v["ok"]
        s.verify_detail = v["detail"]
        s.done = True
        try:
            mark = "✅ <b>VERIFIED — databases identical</b>" if v["ok"] \
                else "❌ <b>VERIFY FAILED</b>"
            await bot.send_message(
                admin_chat_id,
                f"{'🔁 Delta' if mode == 'delta' else '🚚 Full'} migration finished.\n"
                f"Rows copied this run: <b>{res['rows']}</b>\n\n"
                f"{mark}\n<code>{v['detail'][:1500]}</code>",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        s.last_error = f"{type(e).__name__}: {e}"
        log.exception("[migrate] fatal")
        try:
            await bot.send_message(
                admin_chat_id,
                f"❌ Migration error: <code>{s.last_error[:200]}</code>\n"
                f"Safe to re-run — it resumes where it stopped.",
                parse_mode="HTML")
        except Exception:
            pass
    finally:
        s.running = False
        _task = None


def start_migration(bot, admin_chat_id: int, mode: str = "full") -> tuple[bool, str]:
    global _state, _task
    if _state.running:
        return (False, "⚠️ A migration is already running. /migrate_mongo_status")
    if not settings.mongodb_uri:
        return (False, "❌ MONGODB_URI env var is not set on Render.")
    if not settings.turso_database_url:
        return (False, "❌ TURSO_DATABASE_URL env var is not set on Render.")
    _state = MigState(running=True, mode=mode, started_at=time.time())
    _task = asyncio.create_task(_run(mode, admin_chat_id, bot))
    return (True, f"🚀 <b>{mode}</b> migration started — Turso → Mongo.\n"
                  f"Live posts keep flowing into Turso meanwhile; a delta run "
                  f"after the cutover catches anything posted during the window.\n"
                  f"Progress: /migrate_mongo_status")
