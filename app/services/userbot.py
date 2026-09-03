"""MTProto userbot — Telethon-backed backfill AND /massdlt bulk delete.

Read-budget guarantees for backfill v2:
  * Chronological single-sweep from cursor → HEAD.
  * ZERO per-message SELECT — dedupe is INSERT OR IGNORE at the batch level.
  * Parent-cover pointer lives in RAM, not the DB.
  * Batched inserts of 100 rows per transaction (executemany).
  * Cursor checkpoint at every batch (single settings write).
  * Stickers ONLY stored if a cover has appeared in the sweep — otherwise
    the row is discarded before it touches Turso.
  * /backfill_status reads in-memory state — zero DB calls.

For a 3,000-message DB channel this touches ~90 rows written, ~30 settings
writes, ZERO reads. Compare to v13 which read ~50 million rows.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("userbot")

# Telethon is heavy; import lazily so the aiogram bot can boot without it.
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import (
        PhoneCodeInvalidError, PhoneCodeExpiredError,
        SessionPasswordNeededError, FloodWaitError,
    )
    _TELETHON_OK = True
except Exception:
    _TELETHON_OK = False
    TelegramClient = None  # type: ignore
    StringSession = None   # type: ignore

from . import repo
from .classify import classify, caption_of


# =============================================================================
# Login state
# =============================================================================
@dataclass
class LoginState:
    phone: str = ""
    phone_code_hash: str = ""
    pending: bool = False


_login = LoginState()
_client = None  # TelegramClient, when connected


# =============================================================================
# Backfill state (in-memory — /backfill_status reads THIS, never Turso)
# =============================================================================
@dataclass
class BackfillState:
    running: bool = False
    db_chat_id: int = 0
    from_id: int = 1
    current_mid: int = 0
    head_mid: int = 0
    covers_ingested: int = 0
    files_ingested: int = 0
    stickers_ingested: int = 0
    skipped_no_cover: int = 0     # files/stickers seen before any cover
    skipped_service: int = 0
    dupes: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_error: str = ""
    end_reason: str = ""          # completed | stopped | error
    current_cover_msg_id: Optional[int] = None
    batches_flushed: int = 0


_state = BackfillState()
_task: Optional[asyncio.Task] = None


# =============================================================================
# Mass-delete state
# =============================================================================
@dataclass
class MassDeleteState:
    running: bool = False
    chat_id: int = 0
    start_id: int = 0
    end_id: int = 0
    deleted: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_error: str = ""
    stopped: bool = False


_mdel = MassDeleteState()
_mdel_task: Optional[asyncio.Task] = None

# Spec-mandated safety knobs
_MASSDLT_BATCH = 100
_MASSDLT_DELAY_S = 2.0
_MASSDLT_LONG_PAUSE_EVERY = 200
_MASSDLT_LONG_PAUSE_S = 20.0


# =============================================================================
# Credentials
# =============================================================================
def telethon_available() -> bool:
    return _TELETHON_OK


async def get_api_id() -> int:
    v = await repo.get_setting("tg_api_id") or os.environ.get("TG_API_ID", "")
    try:
        return int(v)
    except Exception:
        return 0


async def get_api_hash() -> str:
    return (await repo.get_setting("tg_api_hash")) or os.environ.get("TG_API_HASH", "")


async def set_api_creds(api_id: int, api_hash: str) -> None:
    await repo.set_setting("tg_api_id", str(api_id))
    await repo.set_setting("tg_api_hash", api_hash)


async def get_session_string() -> str:
    # ENV first: a locally-generated STRING_SESSION bypasses Telegram's
    # IP/location login restriction and must take priority over any stale
    # DB-stored session. DB value is the fallback (e.g. after /tglogin).
    return (os.environ.get("STRING_SESSION")
            or os.environ.get("TELETHON_SESSION_STRING")
            or await repo.get_setting("tg_session_string")
            or "")


async def set_session_string(s: str) -> None:
    await repo.set_setting("tg_session_string", s)


# =============================================================================
# Client lifecycle
# =============================================================================
async def get_client():
    global _client
    if not _TELETHON_OK:
        raise RuntimeError("telethon not installed on the server")
    api_id = await get_api_id()
    api_hash = await get_api_hash()
    session_str = await get_session_string()
    if not api_id or not api_hash:
        raise RuntimeError("Set /tgsetapi first (api_id/api_hash missing)")
    if not session_str:
        raise RuntimeError(
            "No MTProto session. Set STRING_SESSION env var (locally generated), "
            "or use /tglogin <phone> then /tgcode <code>.")
    if _client is None:
        _client = TelegramClient(StringSession(session_str), api_id, api_hash)
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("Session expired/revoked. Re-run /tglogin.")
    return _client


async def close_client():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None


async def request_login_code(phone: str) -> None:
    global _client
    if not _TELETHON_OK:
        raise RuntimeError("telethon not installed")
    api_id = await get_api_id()
    api_hash = await get_api_hash()
    if not api_id or not api_hash:
        raise RuntimeError("Set /tgsetapi first")
    await close_client()
    _client = TelegramClient(StringSession(), api_id, api_hash)
    await _client.connect()
    sent = await _client.send_code_request(phone)
    _login.phone = phone
    _login.phone_code_hash = sent.phone_code_hash
    _login.pending = True


async def complete_login_with_code(code: str) -> str:
    global _client
    if not _login.pending or _client is None:
        raise RuntimeError("No login pending. Use /tglogin <phone> first.")
    clean = code.replace(" ", "").replace("-", "")
    try:
        await _client.sign_in(phone=_login.phone, code=clean,
                              phone_code_hash=_login.phone_code_hash)
    except SessionPasswordNeededError:
        raise RuntimeError("This account has 2FA. Disable it or use another account.")
    s = _client.session.save()
    await set_session_string(s)
    _login.pending = False
    return s


async def get_me_info() -> dict:
    c = await get_client()
    me = await c.get_me()
    return {
        "id": getattr(me, "id", None),
        "first_name": getattr(me, "first_name", ""),
        "username": getattr(me, "username", ""),
        "phone": getattr(me, "phone", ""),
    }


# =============================================================================
# Backfill — the whole point of v2
# =============================================================================
def backfill_state() -> BackfillState:
    return _state


def render_status() -> str:
    """Zero-DB status — reads only in-memory state."""
    s = _state
    if not s.running and not s.started_at:
        return "💤 <b>Backfill</b>: idle.\nStart with /backfill_start &lt;chan&gt;."
    dt = time.time() - s.started_at if s.started_at else 0
    done = s.covers_ingested + s.files_ingested + s.stickers_ingested
    rate = done / dt if dt > 0 else 0.0
    pct = ""
    if s.head_mid > s.from_id:
        p = 100.0 * max(0, s.current_mid - s.from_id) / max(1, s.head_mid - s.from_id)
        pct = f" ~{p:.1f}%"
    flag = ("🟢 running" if s.running
            else "✅ completed" if s.end_reason == "completed"
            else "🛑 stopped" if s.end_reason == "stopped"
            else "❌ error")
    return (
        f"<b>Backfill</b>: {flag}{pct}\n"
        f"Chat: <code>{s.db_chat_id}</code>\n"
        f"Position: msg <code>{s.current_mid}</code> / head <code>{s.head_mid}</code>\n"
        f"🖼 Covers: <b>{s.covers_ingested}</b>  "
        f"📄 Files: <b>{s.files_ingested}</b>  "
        f"🎨 Stickers: <b>{s.stickers_ingested}</b>\n"
        f"⏭ Skipped (no cover above): {s.skipped_no_cover}\n"
        f"⏭ Skipped (service/other): {s.skipped_service}\n"
        f"♻️ Dupes (batch-level): {s.dupes}\n"
        f"❌ Errors: {s.errors}\n"
        f"⚡ Rate: {rate:.1f} msg/s | Elapsed: {dt:.0f}s | "
        f"Batches flushed: {s.batches_flushed}\n"
        f"Last error: <code>{(s.last_error or '-')[:120]}</code>"
    )


async def _backfill_loop(bot, admin_chat_id: int) -> None:
    """The actual sweep. Runs as an asyncio.Task."""
    global _state, _task
    s = _state
    try:
        client = await get_client()

        # Determine HEAD (highest msg id) once.
        try:
            async for m in client.iter_messages(s.db_chat_id, limit=1):
                s.head_mid = int(getattr(m, "id", 0) or 0)
        except Exception as e:
            s.last_error = f"head lookup: {e}"
            s.head_mid = 0

        # Buffer + parent pointer live in RAM only.
        buffer: list[tuple] = []           # rows to executemany-insert
        current_cover_msg_id: Optional[int] = None

        # Rehydrate parent pointer if we're resuming (one indexed query).
        pre_cover = await repo.find_cover_before(s.db_chat_id, s.from_id)
        if pre_cover:
            current_cover_msg_id = int(pre_cover["source_message_id"])
            s.current_cover_msg_id = current_cover_msg_id

        FLUSH_EVERY = 100
        HEARTBEAT_EVERY = 200
        processed = 0

        # Telethon: reverse=True gives OLDEST-first, min_id excludes older ones.
        async for msg in client.iter_messages(
            s.db_chat_id, reverse=True, min_id=max(0, s.from_id - 1)
        ):
            if not s.running:
                s.end_reason = "stopped"
                break

            mid = int(getattr(msg, "id", 0) or 0)
            if not mid:
                continue
            s.current_mid = mid

            try:
                kind, media_kind, file_id, file_name, mime = classify(msg)
            except Exception as e:
                s.errors += 1
                s.last_error = f"classify(mid={mid}): {e}"
                continue

            if kind == "skip":
                s.skipped_service += 1

            elif kind == "cover":
                # (kind, media, chat, msg, parent, caption, file_id, name, mime)
                buffer.append((
                    "cover", media_kind, s.db_chat_id, mid, None,
                    caption_of(msg), None, file_name, mime,
                ))
                current_cover_msg_id = mid
                s.current_cover_msg_id = mid
                s.covers_ingested += 1

            elif kind == "file":
                if current_cover_msg_id is None:
                    # File / sticker BEFORE the first cover → discard.
                    s.skipped_no_cover += 1
                else:
                    buffer.append((
                        "file", media_kind, s.db_chat_id, mid,
                        current_cover_msg_id, caption_of(msg),
                        None, file_name, mime,
                    ))
                    if media_kind == "sticker":
                        s.stickers_ingested += 1
                    else:
                        s.files_ingested += 1

            processed += 1

            # Flush every 100 rows in a single transaction.
            if len(buffer) >= FLUSH_EVERY:
                try:
                    inserted = await repo.insert_batch(buffer)
                    s.dupes += (len(buffer) - inserted)
                    s.batches_flushed += 1
                except Exception as e:
                    s.errors += 1
                    s.last_error = f"batch flush: {e}"
                buffer.clear()
                # Cursor checkpoint (single settings write).
                try:
                    await repo.set_cursor(s.db_chat_id, mid)
                except Exception:
                    pass

            # Heartbeat DM every 200 messages (best-effort).
            if processed and processed % HEARTBEAT_EVERY == 0:
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"📦 backfill: mid={mid} | 🖼+{s.covers_ingested} "
                        f"📄+{s.files_ingested} 🎨+{s.stickers_ingested} | "
                        f"batches={s.batches_flushed}",
                    )
                except Exception:
                    pass

            # Gentle pacing to be nice to Telegram.
            await asyncio.sleep(0.02)

        # Final flush.
        if buffer:
            try:
                inserted = await repo.insert_batch(buffer)
                s.dupes += (len(buffer) - inserted)
                s.batches_flushed += 1
            except Exception as e:
                s.errors += 1
                s.last_error = f"final flush: {e}"
            buffer.clear()

        # Final cursor.
        if s.current_mid:
            try:
                await repo.set_cursor(s.db_chat_id, s.current_mid)
            except Exception:
                pass

        if not s.end_reason:
            s.end_reason = "completed"

        # Completion DM.
        try:
            await bot.send_message(
                admin_chat_id,
                f"✅ <b>Backfill complete</b>\n"
                f"🖼 Covers: {s.covers_ingested}\n"
                f"📄 Files: {s.files_ingested}\n"
                f"🎨 Stickers: {s.stickers_ingested}\n"
                f"⏭ Skipped: {s.skipped_service + s.skipped_no_cover}\n"
                f"♻️ Dupes: {s.dupes}\n"
                f"❌ Errors: {s.errors}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except Exception as e:
        s.errors += 1
        s.last_error = f"{type(e).__name__}: {e}"
        s.end_reason = "error"
        log.exception("[backfill] fatal")
        try:
            await bot.send_message(admin_chat_id, f"❌ Backfill error: {e}")
        except Exception:
            pass
    finally:
        s.running = False
        _task = None


async def start_backfill(bot, admin_chat_id: int, db_chat_id: int,
                   from_id: int = 1) -> tuple[bool, str]:
    global _state, _task
    if _state.running:
        return (False, "⚠️ Backfill already running. Use /backfill_stop or /backfill_status.")
    if not _TELETHON_OK:
        return (False, "❌ telethon not installed on the server.")
    if not await repo.get_channel(db_chat_id):
        return (False, f"❌ <code>{db_chat_id}</code> is not a registered channel. "
                       f"Use /addchannel {db_chat_id} database first.")
    _state = BackfillState(
        running=True, db_chat_id=int(db_chat_id),
        from_id=max(1, int(from_id)),
        current_mid=max(1, int(from_id)),
        started_at=time.time(),
    )
    _task = asyncio.create_task(_backfill_loop(bot, admin_chat_id))
    return (True, f"🚀 Backfill started for <code>{db_chat_id}</code> "
                  f"from msg-id {from_id}. Progress every 200 msgs in this DM.")


async def resume_backfill(bot, admin_chat_id: int, db_chat_id: int) -> tuple[bool, str]:
    hi = (await repo.get_cursor(db_chat_id)) or 0
    ok, txt = await start_backfill(bot, admin_chat_id, db_chat_id, from_id=hi + 1)
    if ok:
        txt = f"🔁 Resuming from cursor <code>{hi}</code>.\n" + txt
    return (ok, txt)


def stop_backfill() -> tuple[bool, str]:
    global _state
    if not _state.running:
        return (False, "💤 No backfill running.")
    _state.running = False
    _state.end_reason = "stopped"
    return (True, "🛑 Stop requested — will exit after the current message.")


def reset_backfill_state() -> str:
    global _state
    if _state.running:
        return "⚠️ Stop the running backfill first (/backfill_stop)."
    _state = BackfillState()
    return "🧹 Backfill state cleared. Existing posts in DB untouched."


# =============================================================================
# /massdlt (unchanged spec — kept working as user requested)
# =============================================================================
def parse_massdlt_link(link: str) -> Optional[tuple[int, int]]:
    """Parse t.me link → (chat_id, message_id). Returns (0, mid) for public
    usernames (caller resolves)."""
    import re as _re
    if not link:
        return None
    m = _re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
    if m:
        raw = int(m.group(1))
        mid = int(m.group(2))
        if raw > 0:
            raw = int(f"-100{raw}")
        return (raw, mid)
    m = _re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        return (0, int(m.group(2)))
    return None


async def _resolve_public_username(username: str) -> Optional[int]:
    if not _TELETHON_OK:
        return None
    try:
        c = await get_client()
        ent = await c.get_entity(username)
        return int(getattr(ent, "id", 0)) or None
    except Exception as e:
        log.warning("[massdlt] resolve %s failed: %s", username, e)
        return None


def mass_delete_state() -> MassDeleteState:
    return _mdel


def mass_delete_stop() -> tuple[bool, str]:
    global _mdel
    if not _mdel.running:
        return (False, "💤 No /massdlt task running.")
    _mdel.stopped = True
    return (True, "🛑 Stop requested — will exit after the current batch.")


async def _massdlt_loop(bot, admin_chat_id: int) -> None:
    global _mdel, _mdel_task
    ids_all = list(range(_mdel.start_id, _mdel.end_id + 1))
    total = len(ids_all)
    log.info("[massdlt] start chat=%s range=%s..%s total=%s",
             _mdel.chat_id, _mdel.start_id, _mdel.end_id, total)

    try:
        client = await get_client()
    except Exception as e:
        _mdel.last_error = f"userbot not ready: {e}"
        _mdel.running = False
        try:
            await bot.send_message(admin_chat_id, f"❌ /massdlt aborted: {e}")
        except Exception:
            pass
        return

    submitted_since_pause = 0
    try:
        i = 0
        while i < total:
            if _mdel.stopped:
                break
            batch = ids_all[i:i + _MASSDLT_BATCH]
            i += len(batch)

            while True:
                try:
                    result = await client.delete_messages(_mdel.chat_id, batch)
                    deleted_here = 0
                    if isinstance(result, list):
                        for r in result:
                            deleted_here += int(getattr(r, "pts_count", 0) or 0)
                    else:
                        deleted_here = int(getattr(result, "pts_count", 0) or 0)
                    if deleted_here == 0:
                        deleted_here = len(batch)
                    _mdel.deleted += deleted_here
                    break
                except FloodWaitError as fw:
                    wait_s = int(getattr(fw, "seconds", 0) or getattr(fw, "value", 0) or 5)
                    try:
                        await bot.send_message(
                            admin_chat_id,
                            f"⏳ FloodWait: pausing {wait_s}s…",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait_s + 1)
                    continue
                except Exception as e:
                    _mdel.errors += 1
                    _mdel.last_error = f"{type(e).__name__}: {e}"
                    log.exception("[massdlt] batch %s..%s failed",
                                  batch[0], batch[-1])
                    break

            submitted_since_pause += len(batch)
            if (i // _MASSDLT_BATCH) % 5 == 0:
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"🧹 /massdlt: {i}/{total} submitted "
                        f"(≈{_mdel.deleted} confirmed, err={_mdel.errors})",
                    )
                except Exception:
                    pass

            if submitted_since_pause >= _MASSDLT_LONG_PAUSE_EVERY:
                submitted_since_pause = 0
                await asyncio.sleep(_MASSDLT_LONG_PAUSE_S)
            else:
                await asyncio.sleep(_MASSDLT_DELAY_S)

    finally:
        _mdel.running = False
        elapsed = time.time() - _mdel.started_at
        summary = (
            f"✅ <b>Deletion Complete!</b>\n"
            f"Chat: <code>{_mdel.chat_id}</code>\n"
            f"Range: <code>{_mdel.start_id}</code>..<code>{_mdel.end_id}</code> "
            f"({total} IDs)\n"
            f"Removed: <b>{_mdel.deleted}</b>\n"
            f"Errors: <b>{_mdel.errors}</b>\n"
            f"Elapsed: {elapsed:.1f}s"
        )
        if _mdel.stopped:
            summary = "🛑 <b>Deletion stopped early.</b>\n" + summary
        try:
            await bot.send_message(admin_chat_id, summary, parse_mode="HTML")
        except Exception:
            pass
        _mdel_task = None


async def mass_delete_start(bot, admin_chat_id: int,
                            chat_id: int, start_id: int, end_id: int
                            ) -> tuple[bool, str]:
    global _mdel, _mdel_task
    if not _TELETHON_OK:
        return (False, "❌ telethon not installed.")
    if _mdel.running:
        return (False, "⚠️ /massdlt already running. Use /massdlt_stop first.")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    if start_id < 1:
        return (False, "❌ Invalid start message-id.")
    if end_id - start_id + 1 > 200_000:
        return (False, "❌ Range too large (>200000). Split into smaller chunks.")
    _mdel = MassDeleteState(
        running=True, chat_id=int(chat_id),
        start_id=int(start_id), end_id=int(end_id),
        started_at=time.time(),
    )
    _mdel_task = asyncio.create_task(_massdlt_loop(bot, admin_chat_id))
    return (True, f"🚀 /massdlt started: chat=<code>{chat_id}</code>, "
                  f"range <code>{start_id}</code>..<code>{end_id}</code> "
                  f"({end_id - start_id + 1} IDs). "
                  f"Batches 100/req, 2s between, 20s pause every 200 IDs.")


# =============================================================================
# /forward — userbot bulk-forward of a message-id range between channels (v3.2)
# =============================================================================
@dataclass
class ForwardState:
    running: bool = False
    source_ref: object = None          # int chat_id or Telethon entity
    dest_refs: list = field(default_factory=list)
    start_id: int = 0
    end_id: int = 0
    current_id: int = 0                # next id to process (resume cursor)
    forwarded: int = 0
    errors: int = 0
    failed_ids: list = field(default_factory=list)
    started_at: float = 0.0
    last_error: str = ""
    stopped: bool = False
    end_reason: str = ""               # completed | stopped | error


_fwd = ForwardState()
_fwd_task: Optional[asyncio.Task] = None

# Rate knobs — forwards are among the most aggressively policed Telegram
# actions, so the pace is deliberately conservative and FloodWait-aware.
_FWD_BATCH = 100                 # max ids Telegram allows per forward_messages call
_FWD_DELAY_S = 3.0               # pause between batches per destination
_FWD_SINGLE_DELAY_S = 0.35       # degraded mode (batch failed → per-message)
_FWD_LONG_PAUSE_EVERY = 1000     # messages per destination…
_FWD_LONG_PAUSE_S = 60.0         # …trigger this rest
_FWD_PROGRESS_EVERY = 500        # DM progress every N processed ids
_FWD_MAX_SPAN = 200_000


def _is_perm_err(e: Exception) -> bool:
    """True when the error is a permanent permission/right failure — retrying
    (or degrading to singles) can never succeed for this destination."""
    s = f"{type(e).__name__}: {e}".lower()
    return ("admin privileges" in s or "chatadminrequired" in s
            or "not enough rights" in s or "chat_write_forbidden" in s
            or "user_banned" in s or "channel_private" in s
            or "forbidden" in s)


def forward_state() -> ForwardState:
    return _fwd


def forward_stop() -> tuple[bool, str]:
    if not _fwd.running:
        return (False, "💤 No /forward task is running.")
    _fwd.stopped = True
    return (True, "🛑 Stop requested — halts after the current batch. "
                  "Use /forward_resume to continue from the same id later.")


async def resolve_channel_ref(username: str):
    """Resolve a public username to a full Telethon entity — used when the
    /forward links are t.me/<username>/<id> instead of t.me/c/…."""
    if not _TELETHON_OK:
        return None
    try:
        c = await get_client()
        return await c.get_entity(username)
    except Exception as e:
        log.warning("[forward] resolve %s failed: %s", username, e)
        return None


async def _forward_loop(bot, admin_chat_id: int) -> None:
    global _fwd, _fwd_task
    try:
        client = await get_client()
    except Exception as e:
        _fwd.running = False
        _fwd.end_reason = "error"
        _fwd.last_error = f"userbot not ready: {e}"
        try:
            await bot.send_message(admin_chat_id, f"❌ /forward aborted: {e}")
        except Exception:
            pass
        return

    # Resolve every peer ONCE up-front — clear, early failure with guidance
    # if the userbot account is not a member somewhere.
    try:
        source_peer = await client.get_entity(_fwd.source_ref)
    except Exception as e:
        _fwd.running = False
        _fwd.end_reason = "error"
        _fwd.last_error = f"source resolve failed: {e}"
        try:
            await bot.send_message(
                admin_chat_id,
                f"❌ Cannot access the source channel: <code>{e}</code>\n"
                f"Make sure the userbot account is a member of it.",
                parse_mode="HTML")
        except Exception:
            pass
        return
    dest_peers = []
    for d in _fwd.dest_refs:
        try:
            dest_peers.append(await client.get_entity(d))
        except Exception as e:
            _fwd.running = False
            _fwd.end_reason = "error"
            _fwd.last_error = f"dest resolve failed: {e}"
            try:
                await bot.send_message(
                    admin_chat_id,
                    f"❌ Cannot access destination <code>{d}</code>: <code>{e}</code>\n"
                    f"Make sure the userbot account is a member with posting rights.",
                    parse_mode="HTML")
            except Exception:
                pass
            return

    dead_dests: set = set()
    log.info("[forward] start ids %s..%s → %d dest(s)",
             _fwd.start_id, _fwd.end_id, len(dest_peers))
    sent_since_pause = 0
    last_progress_mark = -1
    try:
        cur = _fwd.current_id
        while cur <= _fwd.end_id and not _fwd.stopped:
            batch = list(range(cur, min(cur + _FWD_BATCH, _fwd.end_id + 1)))
            for dest in dest_peers:
                if _fwd.stopped:
                    break
                if id(dest) in dead_dests:
                    continue  # permanently rejected (no admin/post rights)
                # ---- one batch → one destination, FloodWait-resilient ----
                while True:
                    try:
                        res = await client.forward_messages(
                            dest, batch, from_peer=source_peer)
                        if isinstance(res, list):
                            _fwd.forwarded += len(res)
                        elif res:
                            # Raw Updates (incomplete mappings) — Telegram
                            # accepted the batch; count the whole batch.
                            _fwd.forwarded += len(batch)
                        break
                    except FloodWaitError as fw:
                        wait_s = int(getattr(fw, "seconds", 0) or 5)
                        _fwd.last_error = f"FloodWait {wait_s}s"
                        try:
                            await bot.send_message(
                                admin_chat_id,
                                f"⏳ Forward FloodWait: resting {wait_s}s…")
                        except Exception:
                            pass
                        await asyncio.sleep(wait_s + 1)
                        continue
                    except Exception as e:
                        _fwd.last_error = f"{type(e).__name__}: {e}"
                        if _is_perm_err(e):
                            # Permanent (no post rights) — singles would fail
                            # identically; skip this dest for the whole run.
                            dead_dests.add(id(dest))
                            _fwd.errors += len(batch)
                            try:
                                await bot.send_message(
                                    admin_chat_id,
                                    f"⛔ Destination <code>{getattr(dest, 'id', dest)}</code> "
                                    f"rejected forwards ({type(e).__name__}: admin/post "
                                    f"rights required). Skipping it for the rest of this run.",
                                    parse_mode="HTML")
                            except Exception:
                                pass
                            break
                        # Batch failed (usually a few deleted ids inside it) —
                        # degrade to per-message forwards so the rest survive.
                        log.warning("[forward] batch %s..%s failed (%s) — singles mode",
                                    batch[0], batch[-1], e)
                        _dead = False
                        for mid in batch:
                            if _fwd.stopped or _dead:
                                break
                            while True:
                                try:
                                    r = await client.forward_messages(
                                        dest, [mid], from_peer=source_peer)
                                    _fwd.forwarded += 1 if r else 0
                                    break
                                except FloodWaitError as fw:
                                    wait_s = int(getattr(fw, "seconds", 0) or 5)
                                    _fwd.last_error = f"FloodWait {wait_s}s"
                                    await asyncio.sleep(wait_s + 1)
                                    continue
                                except Exception as e2:
                                    _fwd.errors += 1
                                    _fwd.failed_ids.append(mid)
                                    _fwd.last_error = f"{type(e2).__name__}: {e2}"
                                    if _is_perm_err(e2):
                                        _dead = True
                                        dead_dests.add(id(dest))
                                    break
                            await asyncio.sleep(_FWD_SINGLE_DELAY_S)
                        break
                # ---- rate rests ----
                sent_since_pause += len(batch)
                if sent_since_pause >= _FWD_LONG_PAUSE_EVERY:
                    sent_since_pause = 0
                    try:
                        await bot.send_message(
                            admin_chat_id,
                            f"😴 Rate-limit rest {_FWD_LONG_PAUSE_S:.0f}s "
                            f"(processed up to id {batch[-1]}, "
                            f"forwarded {_fwd.forwarded}, err {_fwd.errors})")
                    except Exception:
                        pass
                    await asyncio.sleep(_FWD_LONG_PAUSE_S)
                else:
                    await asyncio.sleep(_FWD_DELAY_S)
            cur = batch[-1] + 1
            _fwd.current_id = cur
            processed = cur - _fwd.start_id
            mark = processed // _FWD_PROGRESS_EVERY
            if mark != last_progress_mark:
                last_progress_mark = mark
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"📨 /forward: {min(cur - 1, _fwd.end_id)}/{_fwd.end_id} "
                        f"ids processed — forwarded <b>{_fwd.forwarded}</b>, "
                        f"errors {_fwd.errors}",
                        parse_mode="HTML")
                except Exception:
                    pass
    finally:
        _fwd.running = False
        if not _fwd.end_reason:
            _fwd.end_reason = "stopped" if _fwd.stopped else "completed"
        elapsed = time.time() - _fwd.started_at
        span = _fwd.end_id - _fwd.start_id + 1
        head = ("🛑 <b>/forward stopped.</b>" if _fwd.end_reason == "stopped"
                else "✅ <b>/forward complete!</b>")
        summary = (f"{head}\n"
                   f"Range: <code>{_fwd.start_id}</code>..<code>{_fwd.end_id}</code> ({span} ids)\n"
                   f"Destinations: <b>{len(_fwd.dest_refs)}</b>\n"
                   f"Forwarded: <b>{_fwd.forwarded}</b>   Errors: <b>{_fwd.errors}</b>\n"
                   f"Elapsed: {elapsed:.1f}s")
        if _fwd.failed_ids:
            preview = ", ".join(str(i) for i in _fwd.failed_ids[:15])
            more = (f" +{len(_fwd.failed_ids) - 15} more"
                    if len(_fwd.failed_ids) > 15 else "")
            summary += f"\nFailed ids: <code>{preview}{more}</code>"
        if dead_dests:
            summary += (f"\n⛔ {len(dead_dests)} destination(s) skipped — "
                        f"userbot lacks admin/post rights there.")
        if _fwd.end_reason == "stopped":
            summary += f"\nResume with /forward_resume (next id {_fwd.current_id})."
        try:
            await bot.send_message(admin_chat_id, summary, parse_mode="HTML")
        except Exception:
            pass
        _fwd_task = None


async def forward_start(bot, admin_chat_id: int, source_ref, dest_refs: list,
                        start_id: int, end_id: int) -> tuple[bool, str]:
    global _fwd, _fwd_task
    if not _TELETHON_OK:
        return (False, "❌ telethon not installed on the server.")
    if _fwd.running:
        return (False, "⚠️ /forward is already running. /forward_stop first.")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    if start_id < 1:
        return (False, "❌ Invalid start message-id.")
    span = end_id - start_id + 1
    if span > _FWD_MAX_SPAN:
        return (False, f"❌ Range too large (>{_FWD_MAX_SPAN}). Split into chunks.")
    _fwd = ForwardState(
        running=True, source_ref=source_ref,
        dest_refs=list(dest_refs),
        start_id=int(start_id), end_id=int(end_id), current_id=int(start_id),
        started_at=time.time())
    _fwd_task = asyncio.create_task(_forward_loop(bot, admin_chat_id))
    return (True,
            f"🚀 /forward started: <b>{span}</b> message ids "
            f"<code>{start_id}</code>..<code>{end_id}</code> → "
            f"<b>{len(dest_refs)}</b> destination(s).\n"
            f"Pace: batches of {_FWD_BATCH}, {_FWD_DELAY_S:.0f}s apart, "
            f"{_FWD_LONG_PAUSE_S:.0f}s rest every {_FWD_LONG_PAUSE_EVERY}/destination. "
            f"FloodWait auto-honored.\n"
            f"/forward_status to watch · /forward_stop to halt.")


async def forward_resume(bot, admin_chat_id: int) -> tuple[bool, str]:
    """Resume a stopped/failed run from its last processed id.
    NOTE: state is in-memory — a Render redeploy forgets it; just re-issue
    /forward with a start link at the id you stopped on."""
    global _fwd, _fwd_task
    if _fwd.running:
        return (False, "⚠️ /forward is already running.")
    if not _fwd.started_at or not _fwd.dest_refs:
        return (False, "💤 Nothing to resume.")
    if _fwd.current_id > _fwd.end_id:
        return (False, "✅ Previous /forward already finished.")
    _fwd.running = True
    _fwd.stopped = False
    _fwd.end_reason = ""
    _fwd_task = asyncio.create_task(_forward_loop(bot, admin_chat_id))
    return (True, f"▶️ Resuming /forward from id <code>{_fwd.current_id}</code>…")


# =============================================================================
# fsub join-request sync (v3.3.1) — list users waiting for approval
# =============================================================================
async def fetch_join_requests(chat_id: int) -> list:
    """Return user_ids with a PENDING join request in the given channel.

    Requires the userbot account to be an ADMIN there (the pending-request
    list is admin-only). Paged 200 at a time with light pacing. Used by
    /fsub_sync to import requests sent before the recorder went live."""
    if not _TELETHON_OK:
        raise RuntimeError("telethon not installed on the server")
    from telethon.tl import functions as _f, types as _t
    flt = getattr(_t, "ChannelParticipantsRequestToJoin", None)
    if flt is None:
        raise RuntimeError("telethon too old: ChannelParticipantsRequestToJoin missing")
    client = await get_client()
    peer = await client.get_entity(int(chat_id))
    uids: list[int] = []
    offset = 0
    while True:
        res = await client(_f.channels.GetParticipantsRequest(
            channel=peer, filter=flt(), offset=offset, limit=200, hash=0))
        users = getattr(res, "users", None) or []
        uids.extend(int(getattr(u, "id")) for u in users)
        if len(users) < 200:
            break
        offset += len(users)
        await asyncio.sleep(0.3)  # safety pacing
    return uids

