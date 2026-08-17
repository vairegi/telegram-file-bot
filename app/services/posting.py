"""Publishing / delivery engine."""
from __future__ import annotations

import json
from typing import Optional

from .. import db
from ..utils import now_iso
from . import repo
from .tg import (copy_message, forward_message, get_me, send_audio,
                 send_document, send_message, send_photo, send_video)

_bot_username: str | None = None


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username:
        return _bot_username
    me = await get_me()
    _bot_username = me.get("username") if isinstance(me, dict) else me.username
    return _bot_username


def _deep_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


def _apply_extra(base: str, extra: str) -> str:
    if not extra:
        return base
    if not base:
        return extra
    if extra.strip() in base:
        return base
    return f"{base}\n\n{extra}"


def _render_template(template: str, ctx: dict) -> str:
    text = template
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", str(v or ""))
    return text.strip()


async def _build_main_caption(post: dict) -> str:
    template = repo.get_setting("caption_template") or "{caption}"
    extra = (repo.get_setting("post_caption_extra") or "").strip()
    base = _render_template(template, {
        "caption": post.get("caption") or "",
        "code": post.get("code") or "",
    })
    return _apply_extra(base, extra)


async def _build_file_caption(post: dict) -> str:
    extra = (repo.get_setting("file_caption_extra") or "").strip()
    base = post.get("caption") or ""
    return _apply_extra(base, extra)


def _get_file_keyboard(code: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "❤️ Save", "callback_data": f"fav:{code}"},
        {"text": "🗑 Remove", "callback_data": f"unfav:{code}"},
    ]]}


def _get_channel_keyboard(bot_username: str, code: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "📥 Get File", "url": _deep_link(bot_username, code)}
    ]]}


async def post_to_main_channel(post: dict, chat_id: int) -> Optional[int]:
    source_chat = int(post["source_chat_id"])
    source_msg = int(post["source_message_id"])
    caption = await _build_main_caption(post)
    media_kind = post.get("media_kind") or "text"
    protect = repo.get_setting_bool("protect_content", False)
    spoiler = repo.get_setting_bool("spoiler_media", False)
    uname = await get_bot_username()
    keyboard = _get_channel_keyboard(uname, post["code"])
    sent = None
    try:
        if media_kind in ("photo", "video", "document", "audio"):
            sent = await copy_message(
                chat_id=chat_id, from_chat_id=source_chat, message_id=source_msg,
                caption=caption or None, protect_content=protect, reply_markup=keyboard)
        else:
            sent = await send_message(chat_id, caption or "📎",
                                      protect_content=protect, reply_markup=keyboard)
    except Exception:
        fid = post.get("file_id")
        kw = {"caption": caption or None, "protect_content": protect, "reply_markup": keyboard}
        if media_kind == "photo" and fid:
            sent = await send_photo(chat_id, fid, has_spoiler=spoiler, **kw)
        elif media_kind == "video" and fid:
            sent = await send_video(chat_id, fid, has_spoiler=spoiler, **kw)
        elif media_kind == "document" and fid:
            sent = await send_document(chat_id, fid, **kw)
        elif media_kind == "audio" and fid:
            sent = await send_audio(chat_id, fid, **kw)
        else:
            sent = await send_message(chat_id, caption or "📎",
                                      protect_content=protect, reply_markup=keyboard)
    main_msg_id = sent.get("message_id") if isinstance(sent, dict) else None
    db.execute(
        "INSERT INTO post_copies (post_id, target_chat_id, message_id) VALUES (?,?,?) "
        "ON CONFLICT(post_id, target_chat_id) DO UPDATE SET message_id=excluded.message_id",
        (post["id"], chat_id, main_msg_id))
    if not post.get("posted_at"):
        db.execute("UPDATE posts SET posted_at=? WHERE id=?", (now_iso(), post["id"]))
    if main_msg_id:
        db.execute("UPDATE posts SET main_message_id=? WHERE id=?", (main_msg_id, post["id"]))
    return main_msg_id


async def publish_post_to_mains(post: dict) -> int:
    mains = repo.get_main_channels()
    if not mains:
        print(f"[posting] no main channels; skipping post {post['id']}")
        return 0
    sent = 0
    for ch in mains:
        try:
            await post_to_main_channel(post, int(ch["telegram_chat_id"]))
            sent += 1
        except Exception as exc:
            print(f"[posting] main-publish failed to {ch['telegram_chat_id']}: {exc}")
    return sent


async def deliver_file_to_user(user_id: int, post: dict) -> bool:
    source_chat = int(post["source_chat_id"])
    source_msg = int(post["source_message_id"])
    caption = await _build_file_caption(post)
    keyboard = _get_file_keyboard(post["code"])
    try:
        await copy_message(chat_id=user_id, from_chat_id=source_chat,
                           message_id=source_msg, caption=caption or None,
                           reply_markup=keyboard)
    except Exception as exc:
        await send_message(user_id, caption or "📎", reply_markup=keyboard)
        print(f"[deliver] cover copy failed: {exc}")
    extras = json.loads(post.get("extra_files") or "[]")
    for f in extras:
        fid = f.get("file_id")
        kind = f.get("kind")
        try:
            if kind == "document" and fid:
                await send_document(user_id, fid, reply_markup=keyboard)
            elif kind == "audio" and fid:
                await send_audio(user_id, fid, reply_markup=keyboard)
            elif kind == "photo" and fid:
                await send_photo(user_id, fid, reply_markup=keyboard)
            elif kind == "video" and fid:
                await send_video(user_id, fid, reply_markup=keyboard)
            else:
                raise RuntimeError("no file_id")
        except Exception as exc:
            src_msg = f.get("source_message_id")
            if src_msg:
                try:
                    await copy_message(chat_id=user_id, from_chat_id=source_chat,
                                       message_id=int(src_msg), reply_markup=keyboard)
                    continue
                except Exception:
                    pass
            print(f"[deliver] extra {kind} failed: {exc}")
    db.execute(
        "UPDATE users SET files_fetched=files_fetched+1, "
        "files_fetched_today=CASE WHEN last_fetch_day=date('now') "
        "  THEN files_fetched_today+1 ELSE 1 END, "
        "last_fetch_day=date('now'), last_fetch_at=? WHERE telegram_user_id=?",
        (now_iso(), user_id))
    return True


async def mirror_post_to_backup(post: dict, backup_chat_id: int) -> Optional[int]:
    try:
        sent = await forward_message(chat_id=backup_chat_id,
                                     from_chat_id=int(post["source_chat_id"]),
                                     message_id=int(post["source_message_id"]))
    except Exception as exc:
        db.execute("INSERT INTO backup_failures (backup_chat_id, post_id, reason) VALUES (?,?,?)",
                   (backup_chat_id, post["id"], str(exc)[:500]))
        return None
    mid = sent.get("message_id") if isinstance(sent, dict) else None
    db.execute(
        "INSERT INTO backup_copies (backup_chat_id, post_id, message_id) VALUES (?,?,?) "
        "ON CONFLICT(backup_chat_id, post_id) DO UPDATE SET message_id=excluded.message_id",
        (backup_chat_id, post["id"], mid))
    return mid
