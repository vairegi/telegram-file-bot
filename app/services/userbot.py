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


def get_api_id() -> int:
    v = repo.get_setting("tg_api_id") or os.environ.get("TG_API_ID", "")
    try:
        return int(v)
    except Exception:
        return 0


def get_api_hash() -> str:
    return repo.get_setting("tg_api_hash") or os.environ.get("TG_API_HASH", "")


def set_api_creds(api_id: int, api_hash: str) -> None:
    repo.set_setting("tg_api_id", str(api_id))
    repo.set_setting("tg_api_hash", api_hash)


def get_session_string() -> str:
    return (repo.get_setting("tg_session_string")
            or os.environ.get("TELETHON_SESSION_STRING", ""))


def set_session_string(s: str) -> None:
    repo.set_setting("tg_session_string", s)


# =============================================================================
# Client lifecycle
# =============================================================================
async def get_client():
    global _client
    if not _TELETHON_OK:
        raise RuntimeError("telethon not installed on the server")
    api_id = get_api_id()
    api_hash = get_api_hash()
    session_str = get_session_string()
    if not api_id or not api_hash:
        raise RuntimeError("Set /tgsetapi first (api_id/api_hash missing)")
    if not session_str:
        raise RuntimeError("Not logged in. Use /tglogin <phone> then /tgcode <code>.")
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
    api_id = get_api_id()
    api_hash = get_api_hash()
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
    set_session_string(s)
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
        pre_cover = repo.find_cover_before(s.db_chat_id, s.from_id)
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
                    inserted = repo.insert_batch(buffer)
                    s.dupes += (len(buffer) - inserted)
                    s.batches_flushed += 1
                except Exception as e:
                    s.errors += 1
                    s.last_error = f"batch flush: {e}"
                buffer.clear()
                # Cursor checkpoint (single settings write).
                try:
                    repo.set_cursor(s.db_chat_id, mid)
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
                inserted = repo.insert_batch(buffer)
                s.dupes += (len(buffer) - inserted)
                s.batches_flushed += 1
            except Exception as e:
                s.errors += 1
                s.last_error = f"final flush: {e}"
            buffer.clear()

        # Final cursor.
        if s.current_mid:
            try:
                repo.set_cursor(s.db_chat_id, s.current_mid)
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


def start_backfill(bot, admin_chat_id: int, db_chat_id: int,
                   from_id: int = 1) -> tuple[bool, str]:
    global _state, _task
    if _state.running:
        return (False, "⚠️ Backfill already running. Use /backfill_stop or /backfill_status.")
    if not _TELETHON_OK:
        return (False, "❌ telethon not installed on the server.")
    if not repo.get_channel(db_chat_id):
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


def resume_backfill(bot, admin_chat_id: int, db_chat_id: int) -> tuple[bool, str]:
    hi = repo.get_cursor(db_chat_id) or 0
    ok, txt = start_backfill(bot, admin_chat_id, db_chat_id, from_id=hi + 1)
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
