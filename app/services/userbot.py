"""In-bot MTProto userbot (Telethon StringSession) for historical backfill.

Runs inside the same Python process as the aiogram bot on Render. No PC needed.

Env / settings:
  TG_API_ID, TG_API_HASH          -> set on Render env OR via /tgsetapi
  TELETHON_SESSION_STRING         -> optional pre-made StringSession (tries first)

Commands (admin only):
  /tgsetapi <api_id> <api_hash>   -> store credentials
  /tglogin <phone>                -> request a code, then /tgcode <code>
  /tgcode <code>                  -> completes login; saves session to bot_settings
  /tgstatus                       -> show MTProto state (logged in / user / balance)
  /backfill_start <db_chat_id> [from_id]  -> start background backfill
  /backfill_stop                  -> stop the running backfill gracefully
  /backfill_status                -> live progress (also updates every 20 msgs)
  /backfill_resume <db_chat_id>   -> resume from highest already-imported id
  /backfill_reset                 -> clear backfill state (does NOT delete posts)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("userbot")

# Telethon is heavy; import lazily so the bot can boot without it installed.
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import (
        PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError,
        FloodWaitError,
    )
    _TELETHON_OK = True
except Exception:
    _TELETHON_OK = False
    TelegramClient = None
    StringSession = None

from . import repo
from .. import db


# =============================================================================
# State
# =============================================================================
@dataclass
class BackfillState:
    running: bool = False
    db_chat_id: int = 0
    from_id: int = 1
    to_id: int = 0
    current_mid: int = 0
    covers: int = 0
    pdfs: int = 0
    skipped_dup: int = 0
    skipped_svc: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_error: str = ""
    end_reason: str = ""            # completed | stopped | error
    current_cover_msg_id: Optional[int] = None
    total_estimate: int = 0


@dataclass
class LoginState:
    phone: str = ""
    phone_code_hash: str = ""
    pending: bool = False


_state = BackfillState()
_login = LoginState()
_task: Optional[asyncio.Task] = None
_client = None           # TelegramClient, when connected
_progress_message: dict = {}   # {chat_id: message_id} for live-updating progress


# =============================================================================
# Credential helpers (settings-backed, env fallback)
# =============================================================================
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
    return repo.get_setting("tg_session_string") or os.environ.get("TELETHON_SESSION_STRING", "")


def set_session_string(s: str) -> None:
    repo.set_setting("tg_session_string", s)


def telethon_available() -> bool:
    return _TELETHON_OK


# =============================================================================
# Client lifecycle
# =============================================================================
async def get_client():
    """Return a connected, authorized TelegramClient. Raises if not logged in."""
    global _client
    if not _TELETHON_OK:
        raise RuntimeError("telethon not installed (pip install telethon)")
    api_id = get_api_id()
    api_hash = get_api_hash()
    session_str = get_session_string()
    if not api_id or not api_hash:
        raise RuntimeError("Set /tgsetapi first (or TG_API_ID / TG_API_HASH env vars)")
    if not session_str:
        raise RuntimeError("Not logged in. Use /tglogin <phone> then /tgcode <code>, "
                           "or set TELETHON_SESSION_STRING.")

    if _client is None:
        sess = StringSession(session_str)
        _client = TelegramClient(sess, api_id, api_hash)
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("Session is expired/revoked. Use /tglogin to log in again.")
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
    """Start the login flow by sending a code to <phone>."""
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
    """Complete the login using the code Telegram sent. Returns the session string."""
    global _client
    if not _login.pending or _client is None:
        raise RuntimeError("No login pending. Use /tglogin <phone> first.")
    clean = code.replace(" ", "").replace("-", "")
    try:
        await _client.sign_in(phone=_login.phone, code=clean,
                              phone_code_hash=_login.phone_code_hash)
    except SessionPasswordNeededError:
        raise RuntimeError("This account has 2FA enabled. Disable it temporarily, "
                           "or use a different account for backfill.")
    session_str = _client.session.save()
    set_session_string(session_str)
    _login.pending = False
    return session_str


async def get_me_info():
    c = await get_client()
    me = await c.get_me()
    return {
        "id": getattr(me, "id", None),
        "first_name": getattr(me, "first_name", ""),
        "username": getattr(me, "username", ""),
        "phone": getattr(me, "phone", ""),
    }


# =============================================================================
# Classification (same rules as PC script)
# =============================================================================
def _fname_of(doc) -> Optional[str]:
    for attr in getattr(doc, "attributes", None) or []:
        n = getattr(attr, "file_name", None)
        if n:
            return n
    return None


FILE_EXTS = (".pdf", ".cbz", ".cbr", ".cbt", ".cb7", ".zip", ".rar", ".7z", ".epub")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")

def _is_divider_text_ub(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) > 40:
        return False
    import re as _re
    stripped = _re.sub(
        r"[\U0001F300-\U0001FAFF\U0001F000-\U0001F9FF\u2600-\u27BF\u2300-\u23FF"
        r"\u2B00-\u2BFF\u25A0-\u25FF\u2700-\u27BF\u3000-\u303F"
        r"\uFE00-\uFE0F\u200B-\u200D\uFF00-\uFFEF]+", "", t)
    stripped = _re.sub(
        r"[\s\-\—\–\_\=\.\,\!\?\|\/\\*#@~`\^\(\)\[\]{}<>\+•▪▫◾◽◼◻■□●○★☆♦♢♥♡♠♣]+",
        "", stripped)
    return len(stripped) == 0


def classify(msg):
    caption = msg.message or None
    # v15: drop stickers at ingest so they never reach the main channel or DB.
    if getattr(msg, "sticker", None) is not None:
        return ("skip", "sticker", None, caption)
    d = getattr(msg, "document", None)
    if d is not None:
        # Sticker-as-document (webp / tgs) with no filename -> also skip.
        _dmime = (getattr(d, "mime_type", "") or "").lower()
        _dname = _fname_of(d) or ""
        if _dmime in ("image/webp", "application/x-tgsticker") and not _dname:
            return ("skip", "sticker", None, caption)
        # Extra safety: DocumentAttributeSticker in Telethon attributes.
        for _attr in getattr(d, "attributes", None) or []:
            if type(_attr).__name__ == "DocumentAttributeSticker":
                return ("skip", "sticker", None, caption)
    if d is not None:
        name = _fname_of(d)
        mime = (getattr(d, "mime_type", "") or "").lower()
        lname = (name or "").lower()
        # Image / video documents are ALWAYS covers, never attachable files —
        # even when uploaded with a generic MIME like application/octet-stream.
        is_image = mime.startswith("image/") or any(lname.endswith(e) for e in IMAGE_EXTS)
        is_video = mime.startswith("video/") or any(lname.endswith(e) for e in VIDEO_EXTS)
        if not is_image and not is_video:
            if any(lname.endswith(ext) for ext in FILE_EXTS) or any(k in mime for k in ("pdf","cbz","cbr","cbt","epub","zip","rar","7z","comicbook","x-cbz","x-cbr","x-cbt")):
                return ("pdf", "document", name, caption)
        if is_image:
            return ("cover", "photo", name, caption)
        if is_video:
            return ("cover", "video", name, caption)
        return ("cover", "document", name, caption)
    if getattr(msg, "photo", None) is not None:
        return ("cover", "photo", None, caption)
    if getattr(msg, "video", None) is not None:
        return ("cover", "video", None, caption)
    if getattr(msg, "audio", None) is not None:
        return ("cover", "audio", None, caption)
    if caption:
        if _is_divider_text_ub(caption):
            return ("skip", "other", None, caption)
        return ("cover", "text", None, caption)
    return ("skip", "other", None, caption)

def _retry_write(fn, *args, attempts: int = 6, **kw):
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn(*args, **kw)
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = ("stream not found" in msg
                         or ("hrana" in msg and "404" in msg)
                         or ("connection" in msg and "close" in msg)
                         or ("reset" in msg and "peer" in msg))
            if not transient:
                raise
            try:
                db.reset_conn()
            except Exception:
                pass
            wait = min(2 * i, 15)
            log.warning("DB stream lost — reconnecting in %ss (attempt %s/%s)",
                        wait, i, attempts)
            time.sleep(wait)
    if last:
        raise last


def _highest_imported(chat_id: int) -> int:
    row = db.query_one(
        "SELECT COALESCE(MAX(source_message_id),0) AS mx FROM posts WHERE source_chat_id=?",
        (chat_id,))
    return int((row or {}).get("mx") or 0)


# =============================================================================
# Live progress
# =============================================================================
def render_status() -> str:
    if not _state.running and not _state.started_at:
        return ("💤 <b>Backfill</b>: idle.\n"
                "Start with /backfill_start &lt;chan&gt; or /backfill_resume &lt;chan&gt;")
    dt = time.time() - _state.started_at if _state.started_at else 0
    total_done = _state.covers + _state.pdfs + _state.skipped_dup + _state.skipped_svc
    rate = total_done / dt if dt > 0 else 0.0
    pct = ""
    if _state.total_estimate > 0 and _state.current_mid > 0:
        p = min(100.0, 100.0 * (_state.current_mid - _state.from_id)
                / max(1, _state.total_estimate - _state.from_id))
        pct = f" ~{p:.1f}%"
    bar_len = 14
    filled = 0
    if _state.total_estimate > 0:
        try:
            ratio = (_state.current_mid - _state.from_id) / max(1, _state.total_estimate - _state.from_id)
            filled = int(bar_len * max(0.0, min(1.0, ratio)))
        except Exception:
            filled = 0
    bar = "🟩" * filled + "⬜" * (bar_len - filled)

    if _state.running:
        flag = "🟢 running"
    elif _state.end_reason == "completed":
        flag = "✅ completed"
    elif _state.end_reason == "error":
        flag = "🛑 crashed"
    else:
        flag = "⏸ stopped"
    lines = [
        f"📦 <b>Backfill</b> — {flag}{pct}",
        bar,
        f"📡 channel: <code>{_state.db_chat_id}</code>",
        f"🔢 msg-id: <code>{_state.current_mid}</code> "
        f"(range {_state.from_id}..{_state.to_id or 'latest'})",
        f"🖼 covers: <b>+{_state.covers}</b>",
        f"📄 pdfs:    <b>+{_state.pdfs}</b>",
        f"⏭ dup:     {_state.skipped_dup}",
        f"🚫 svc:     {_state.skipped_svc}",
        f"⚠️ errors:  {_state.errors}",
        f"⏱ elapsed: {int(dt)}s  ·  rate: {rate:.1f} msg/s",
    ]
    if _state.last_error:
        lines.append(f"🛑 last error: <code>{_state.last_error[:120]}</code>")
    return "\n".join(lines)


async def _update_progress(bot, admin_chat_id: int) -> None:
    """Update the sticky progress message in admin's DM."""
    text = render_status()
    mid = _progress_message.get(admin_chat_id)
    from .tg import send_message, edit_message_text
    try:
        if mid:
            await edit_message_text(bot, admin_chat_id, mid, text)
        else:
            m = await send_message(bot, admin_chat_id, text)
            _progress_message[admin_chat_id] = m.message_id
    except Exception:
        # If edit fails (message deleted), fall back to a new message next tick
        _progress_message.pop(admin_chat_id, None)


# =============================================================================
# Backfill task
# =============================================================================
async def _backfill_loop(bot, admin_chat_id: int) -> None:
    global _state
    completed_naturally = False
    try:
        client = await get_client()
        entity = await client.get_entity(_state.db_chat_id)
        title = getattr(entity, "title", str(_state.db_chat_id))
        # Cache the channel title for /listchannels
        try:
            _retry_write(repo.add_channel, _state.db_chat_id, "database", title=title)
        except Exception:
            pass

        log.info("[backfill] starting: %s (%s) ids %s..%s",
                 title, _state.db_chat_id, _state.from_id, _state.to_id or "latest")

        # Total estimate (for the progress bar): latest message id in channel
        try:
            latest = await client.get_messages(entity, limit=1)
            if latest and latest[0] and latest[0].id:
                _state.total_estimate = int(latest[0].id)
        except Exception:
            _state.total_estimate = 0

        await _update_progress(bot, admin_chat_id)

        since_progress = 0
        completed_naturally = True
        async for msg in client.iter_messages(
            entity,
            min_id=_state.from_id - 1,
            max_id=(_state.to_id or 0),
            reverse=True,
        ):
            if not _state.running:
                log.info("[backfill] 🛑 stop requested by admin")
                completed_naturally = False
                break
            if msg is None or msg.id is None:
                continue
            mid = int(msg.id)
            _state.current_mid = mid

            try:
                exists = db.query_one_retry(
                    "SELECT id, kind FROM posts WHERE source_chat_id=? AND source_message_id=?",
                    (_state.db_chat_id, mid))
            except Exception as e:
                _state.errors += 1
                _state.last_error = str(e)
                exists = None

            if exists:
                _state.skipped_dup += 1
                if exists.get("kind") == "cover":
                    _state.current_cover_msg_id = mid
            else:
                kind, media_kind, file_name, caption = classify(msg)
                if kind == "skip":
                    _state.skipped_svc += 1
                else:
                    raw = {"message_id": mid,
                           "date": msg.date.isoformat() if msg.date else None}
                    try:
                        if kind == "cover":
                            _retry_write(repo.insert_cover,
                                         source_chat_id=_state.db_chat_id,
                                         source_message_id=mid,
                                         caption=caption, media_kind=media_kind,
                                         file_id=None, file_name=file_name, raw=raw)
                            _state.current_cover_msg_id = mid
                            _state.covers += 1
                        else:
                            parent = _state.current_cover_msg_id
                            if parent is None:
                                pc = repo.find_cover_before(_state.db_chat_id, mid)
                                parent = pc["source_message_id"] if pc else None
                            _retry_write(repo.insert_pdf,
                                         source_chat_id=_state.db_chat_id,
                                         source_message_id=mid,
                                         parent_msg_id=parent, caption=caption,
                                         media_kind=media_kind, file_id=None,
                                         file_name=file_name, raw=raw)
                            _state.pdfs += 1
                    except Exception as e:
                        _state.errors += 1
                        _state.last_error = str(e)
                        log.exception("[backfill] write failed mid=%s", mid)

            since_progress += 1
            if since_progress >= 20:
                since_progress = 0
                # Heartbeat to Render logs (emojified) + sticky DM update
                dt = time.time() - _state.started_at
                done = _state.covers + _state.pdfs + _state.skipped_dup + _state.skipped_svc
                rate = done / dt if dt > 0 else 0.0
                log.info("[backfill] 📦 mid=%s | 🖼+%s 📄+%s | dup=%s svc=%s err=%s | %.1f msg/s",
                         mid, _state.covers, _state.pdfs,
                         _state.skipped_dup, _state.skipped_svc, _state.errors, rate)
                await _update_progress(bot, admin_chat_id)

            # Gentle pace to avoid rate-limit; Telethon already throttles flood-waits
            await asyncio.sleep(0.05)

        # Final cursor update
        if _state.current_mid:
            try:
                _retry_write(repo.set_cursor, _state.db_chat_id, _state.current_mid)
            except Exception:
                pass

        await _update_progress(bot, admin_chat_id)
        log.info("[backfill] ✅ completed: covers=+%s pdfs=+%s dup=%s svc=%s err=%s",
                 _state.covers, _state.pdfs, _state.skipped_dup,
                 _state.skipped_svc, _state.errors)
    except Exception as e:
        _state.last_error = f"{type(e).__name__}: {e}"
        _state.end_reason = "error"
        log.exception("[backfill] 🛑 fatal")
        try:
            await _update_progress(bot, admin_chat_id)
        except Exception:
            pass
    finally:
        if not _state.end_reason:
            _state.end_reason = "completed" if completed_naturally else "stopped"
        _state.running = False
        global _task
        _task = None


def start_backfill(bot, admin_chat_id: int, db_chat_id: int,
                   from_id: int = 1, to_id: int = 0) -> tuple[bool, str]:
    """Start the background backfill task. Returns (started, message)."""
    global _state, _task
    if _state.running:
        return (False, "⚠️ Backfill is already running. Use /backfill_status or /backfill_stop.")
    if not _TELETHON_OK:
        return (False, "❌ telethon not installed on the server. Run: pip install telethon")
    if not repo.get_channel(db_chat_id):
        return (False, f"❌ <code>{db_chat_id}</code> is not a registered channel. "
                       f"Use /addchannel {db_chat_id} database first.")

    _state = BackfillState(
        running=True, db_chat_id=db_chat_id,
        from_id=max(1, int(from_id)), to_id=int(to_id or 0),
        started_at=time.time(), end_reason="",
    )
    _task = asyncio.create_task(_backfill_loop(bot, admin_chat_id))
    return (True, f"🚀 Backfill started for <code>{db_chat_id}</code> from id {from_id}. "
                  f"Progress updates every 20 messages in this DM.")


def resume_backfill(bot, admin_chat_id: int, db_chat_id: int) -> tuple[bool, str]:
    hi = _highest_imported(db_chat_id)
    start_from = max(1, hi)  # re-check the highest row (it'll skip fast) then go up
    ok, txt = start_backfill(bot, admin_chat_id, db_chat_id, from_id=start_from)
    if ok:
        txt = (f"🔁 Resuming from highest already-imported msg-id <code>{hi}</code>.\n" + txt)
    return (ok, txt)


def stop_backfill() -> tuple[bool, str]:
    global _state
    if not _state.running:
        return (False, "💤 Backfill is not running.")
    _state.running = False
    _state.end_reason = "stopped"
    return (True, "🛑 Stop requested — the loop will exit after the current message.")


def backfill_state() -> BackfillState:
    return _state


def reset_backfill_state() -> str:
    """Clear state without deleting posts."""
    global _state
    if _state.running:
        return "⚠️ Stop the running backfill first (/backfill_stop)."
    _state = BackfillState()
    return "🧹 Backfill state cleared. Existing posts in DB are untouched."


# =============================================================================
# v15 — Publish-time truth probe
# =============================================================================
# The legacy classifier (v11-v13) persisted image/video documents as
# media_kind='document' with a NULL file_name and no mime_type. Publish path
# therefore fell through to copyMessage → raw docs (cbz/stickers/pngs) leaked
# into main channels and spoilers could not be applied. This probe re-inspects
# the source message via MTProto at publish time and patches the row.

_ATTACHABLE_EXTS_PB = (".pdf", ".cbz", ".cbr", ".cbt", ".cb7", ".zip",
                       ".rar", ".7z", ".epub")
_ATTACHABLE_MIME_KEYS_PB = ("pdf", "cbz", "cbr", "cbt", "epub", "zip",
                            "rar", "7z", "comicbook", "x-cbz", "x-cbr", "x-cbt")
_IMAGE_EXTS_PB = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_VIDEO_EXTS_PB = (".mp4", ".mov", ".webm", ".mkv")


async def probe_source_message(chat_id: int, message_id: int) -> Optional[dict]:
    """Return the ground-truth classification of a DB-channel message.

    Result dict:
        {
          'exists':      bool,
          'kind':        'cover'|'pdf'|'skip',
          'media_kind':  'photo'|'video'|'document'|'audio'|'text'|'other',
          'mime_type':   str,
          'file_name':   Optional[str],
          'is_sticker':  bool,
          'is_image':    bool,
          'is_video':    bool,
          'is_file':     bool,
          'caption':     Optional[str],
        }

    Returns None if the userbot is not available (Telethon missing or not
    logged in). Caller should fall back to whatever the DB row says.
    """
    if not _TELETHON_OK:
        return None
    try:
        client = await get_client()
    except Exception as e:
        log.warning("[probe] userbot unavailable: %s", e)
        return None

    try:
        msg = await client.get_messages(chat_id, ids=message_id)
    except Exception as e:
        log.warning("[probe] get_messages(%s,%s) failed: %s", chat_id, message_id, e)
        return None
    if msg is None:
        return {"exists": False, "kind": "skip", "media_kind": "other",
                "mime_type": "", "file_name": None, "is_sticker": False,
                "is_image": False, "is_video": False, "is_file": False,
                "caption": None}

    caption = getattr(msg, "message", None)
    doc = getattr(msg, "document", None)
    photo = getattr(msg, "photo", None)
    video = getattr(msg, "video", None)
    audio = getattr(msg, "audio", None)
    sticker = getattr(msg, "sticker", None)

    if sticker is not None:
        return {"exists": True, "kind": "skip", "media_kind": "other",
                "mime_type": (getattr(sticker, "mime_type", "") or "").lower(),
                "file_name": None, "is_sticker": True,
                "is_image": False, "is_video": False, "is_file": False,
                "caption": caption}

    if photo is not None:
        return {"exists": True, "kind": "cover", "media_kind": "photo",
                "mime_type": "image/jpeg", "file_name": None,
                "is_sticker": False, "is_image": True, "is_video": False,
                "is_file": False, "caption": caption}

    if video is not None and doc is None:
        return {"exists": True, "kind": "cover", "media_kind": "video",
                "mime_type": (getattr(video, "mime_type", "") or "video/mp4").lower(),
                "file_name": None, "is_sticker": False, "is_image": False,
                "is_video": True, "is_file": False, "caption": caption}

    if doc is not None:
        # Telethon exposes stickers via .sticker AND .document; also detect
        # via DocumentAttributeSticker so we don't leak a sticker as a doc.
        for attr in getattr(doc, "attributes", None) or []:
            if type(attr).__name__ == "DocumentAttributeSticker":
                return {"exists": True, "kind": "skip", "media_kind": "other",
                        "mime_type": (getattr(doc, "mime_type", "") or "").lower(),
                        "file_name": _fname_of(doc), "is_sticker": True,
                        "is_image": False, "is_video": False, "is_file": False,
                        "caption": caption}
        name = _fname_of(doc)
        mime = (getattr(doc, "mime_type", "") or "").lower()
        lname = (name or "").lower()

        is_image = mime.startswith("image/") or any(lname.endswith(e) for e in _IMAGE_EXTS_PB)
        is_video = mime.startswith("video/") or any(lname.endswith(e) for e in _VIDEO_EXTS_PB)
        is_file = (not is_image and not is_video and (
            any(lname.endswith(ext) for ext in _ATTACHABLE_EXTS_PB)
            or any(k in mime for k in _ATTACHABLE_MIME_KEYS_PB)
        ))

        if is_file:
            return {"exists": True, "kind": "pdf", "media_kind": "document",
                    "mime_type": mime, "file_name": name, "is_sticker": False,
                    "is_image": False, "is_video": False, "is_file": True,
                    "caption": caption}
        if is_image:
            return {"exists": True, "kind": "cover", "media_kind": "photo",
                    "mime_type": mime or "image/jpeg", "file_name": name,
                    "is_sticker": False, "is_image": True, "is_video": False,
                    "is_file": False, "caption": caption}
        if is_video:
            return {"exists": True, "kind": "cover", "media_kind": "video",
                    "mime_type": mime or "video/mp4", "file_name": name,
                    "is_sticker": False, "is_image": False, "is_video": True,
                    "is_file": False, "caption": caption}
        return {"exists": True, "kind": "cover", "media_kind": "document",
                "mime_type": mime, "file_name": name, "is_sticker": False,
                "is_image": False, "is_video": False, "is_file": False,
                "caption": caption}

    if audio is not None:
        return {"exists": True, "kind": "cover", "media_kind": "audio",
                "mime_type": "audio/mpeg", "file_name": None,
                "is_sticker": False, "is_image": False, "is_video": False,
                "is_file": False, "caption": caption}

    if caption:
        return {"exists": True, "kind": "cover", "media_kind": "text",
                "mime_type": "", "file_name": None, "is_sticker": False,
                "is_image": False, "is_video": False, "is_file": False,
                "caption": caption}
    return {"exists": True, "kind": "skip", "media_kind": "other",
            "mime_type": "", "file_name": None, "is_sticker": False,
            "is_image": False, "is_video": False, "is_file": False,
            "caption": caption}


# =============================================================================
# v15 — /massdlt bulk-delete engine (MTProto)
# =============================================================================
@dataclass
class MassDeleteState:
    running: bool = False
    chat_id: int = 0
    start_id: int = 0
    end_id: int = 0
    deleted: int = 0
    not_found: int = 0
    errors: int = 0
    started_at: float = 0.0
    last_error: str = ""
    stopped: bool = False


_mdel = MassDeleteState()
_mdel_task: Optional[asyncio.Task] = None

# Safety knobs — spec-mandated
_MASSDLT_BATCH = 100            # message IDs per delete_messages() call
_MASSDLT_DELAY_S = 2.0          # sleep between batches
_MASSDLT_LONG_PAUSE_EVERY = 200 # every N *submitted* IDs
_MASSDLT_LONG_PAUSE_S = 20.0    # 20-second safety pause


def parse_massdlt_link(link: str) -> Optional[tuple[int, int]]:
    """Parse a t.me link into (chat_id, message_id). Supports:
      https://t.me/c/2298797194/12345           -> (-1002298797194, 12345)
      https://t.me/somepublic/12345             -> (public username kept as -1
                                                    sentinel; caller resolves)
    Returns None on garbage input.
    """
    import re as _re
    if not link:
        return None
    m = _re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
    if m:
        raw = int(m.group(1)); mid = int(m.group(2))
        if raw > 0:
            raw = int(f"-100{raw}")
        return (raw, mid)
    m = _re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        # Public username — caller must resolve via userbot.get_entity.
        return (0, int(m.group(2)))  # 0 = "resolve from second link/context"
    return None


async def _resolve_public_username(username: str) -> Optional[int]:
    if not _TELETHON_OK:
        return None
    try:
        client = await get_client()
        ent = await client.get_entity(username)
        return int(getattr(ent, "id", 0)) or None
    except Exception as e:
        log.warning("[massdlt] resolve %s failed: %s", username, e)
        return None


async def _massdlt_loop(bot, admin_chat_id: int) -> None:
    """The actual worker. See mass_delete_start() for entry."""
    global _mdel, _mdel_task
    from telethon.errors import FloodWaitError as _FloodWait  # local import

    ids_all = list(range(_mdel.start_id, _mdel.end_id + 1))
    total = len(ids_all)
    log.info("[massdlt] 🚀 start chat=%s range=%s..%s total=%s",
             _mdel.chat_id, _mdel.start_id, _mdel.end_id, total)

    try:
        client = await get_client()
    except Exception as e:
        _mdel.last_error = f"userbot not ready: {e}"
        _mdel.running = False
        try:
            await bot.send_message(admin_chat_id,
                f"❌ /massdlt aborted: {_mdel.last_error}")
        except Exception:
            pass
        return

    submitted_since_pause = 0

    try:
        # Chunk the ID range into batches of _MASSDLT_BATCH.
        i = 0
        while i < total:
            if _mdel.stopped:
                log.info("[massdlt] 🛑 stop requested at %s/%s", i, total)
                break
            batch = ids_all[i:i + _MASSDLT_BATCH]
            i += len(batch)

            # ---- Rate-limit-aware delete with FloodWait retry ----
            while True:
                try:
                    result = await client.delete_messages(_mdel.chat_id, batch)
                    # Telethon returns list[AffectedMessages] with .pts_count
                    # OR just count on some builds; be defensive.
                    deleted_here = 0
                    if isinstance(result, list):
                        for r in result:
                            deleted_here += int(getattr(r, "pts_count", 0) or 0)
                    else:
                        deleted_here = int(getattr(result, "pts_count", 0) or 0)
                    if deleted_here == 0:
                        # Fallback: assume batch length (older Telethon returns
                        # nothing useful). "not_found" is derived at the end.
                        deleted_here = len(batch)
                    _mdel.deleted += deleted_here
                    break
                except _FloodWait as fw:
                    wait_s = int(getattr(fw, "seconds", 0) or getattr(fw, "value", 0) or 5)
                    log.warning("[massdlt] ⏳ FloodWait %ss (batch %s..%s)",
                                wait_s, batch[0], batch[-1])
                    try:
                        await bot.send_message(admin_chat_id,
                            f"⏳ FloodWait: pausing {wait_s}s before continuing…")
                    except Exception:
                        pass
                    await asyncio.sleep(wait_s + 1)
                    continue
                except Exception as e:
                    _mdel.errors += 1
                    _mdel.last_error = f"{type(e).__name__}: {e}"
                    log.exception("[massdlt] batch %s..%s failed", batch[0], batch[-1])
                    break

            submitted_since_pause += len(batch)

            # Progress heartbeat every ~5 batches so the admin sees motion
            if (i // _MASSDLT_BATCH) % 5 == 0:
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"🧹 /massdlt progress: {i}/{total} submitted "
                        f"(≈{_mdel.deleted} confirmed, err={_mdel.errors})")
                except Exception:
                    pass

            # Long safety pause every _MASSDLT_LONG_PAUSE_EVERY IDs
            if submitted_since_pause >= _MASSDLT_LONG_PAUSE_EVERY:
                submitted_since_pause = 0
                log.info("[massdlt] 🛌 long safety pause %ss", _MASSDLT_LONG_PAUSE_S)
                await asyncio.sleep(_MASSDLT_LONG_PAUSE_S)
            else:
                # Short delay between batches (spec)
                await asyncio.sleep(_MASSDLT_DELAY_S)

    finally:
        _mdel.running = False
        elapsed = time.time() - _mdel.started_at
        # Final completion alert (spec item 4)
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
        log.info("[massdlt] done deleted=%s errors=%s elapsed=%.1fs",
                 _mdel.deleted, _mdel.errors, elapsed)


def mass_delete_state() -> MassDeleteState:
    return _mdel


def mass_delete_stop() -> tuple[bool, str]:
    global _mdel
    if not _mdel.running:
        return (False, "💤 No /massdlt task is running.")
    _mdel.stopped = True
    return (True, "🛑 Stop requested — will exit after current batch.")


async def mass_delete_start(bot, admin_chat_id: int,
                            chat_id: int, start_id: int, end_id: int
                            ) -> tuple[bool, str]:
    """Kick off the bulk deletion. Non-blocking: schedules background task."""
    global _mdel, _mdel_task

    if not _TELETHON_OK:
        return (False, "❌ telethon not installed on the server.")
    if _mdel.running:
        return (False, "⚠️ A /massdlt task is already running. "
                       "Use /massdlt_stop to cancel it first.")
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    if start_id < 1:
        return (False, "❌ Invalid start message-id (must be ≥1).")
    if end_id - start_id + 1 > 200_000:
        return (False, "❌ Range too large (>200000). "
                       "Split it into smaller chunks for safety.")

    _mdel = MassDeleteState(
        running=True, chat_id=int(chat_id),
        start_id=int(start_id), end_id=int(end_id),
        started_at=time.time(),
    )
    _mdel_task = asyncio.create_task(_massdlt_loop(bot, admin_chat_id))
    return (True, f"🚀 /massdlt started: chat=<code>{chat_id}</code>, "
                  f"range <code>{start_id}</code>..<code>{end_id}</code> "
                  f"({end_id - start_id + 1} IDs). "
                  f"Batching 100/req, 2s between batches, "
                  f"20s pause every 200 IDs. "
                  f"Progress updates will land in this DM.")
