"""Publishing/delivery engine.

Key design decision (flag this clearly):
---------------------------------------
The user ROTATED the BotFather token. Telegram file_ids are scoped to the bot
that fetched them, so every file_id the OLD Lovable bot stored is invalid for
the new bot. We therefore deliver via copyMessage(chat_id, source_chat_id,
source_message_id) — which works as long as:
  * the new bot is an ADMIN in the Database Channel, and
  * the source message still exists there.

file_id is stored only as a best-effort fallback (and is refreshed by the new
bot whenever it captures the channel_post directly).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .. import db
from ..utils import now_iso, source_link
from . import repo
from .tg import (
    copy_message,
    delete_message,
    edit_message_text,
    forward_message,
    send_audio,
    send_document,
    send_message,
    send_photo,
    send_video,
    get_me,
)

BOT_USERNAME_CACHE: str | None = None


async def get_bot_username() -> str:
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE
    me = await get_me()
    BOT_USERNAME_CACHE = me.username
    return BOT_USERNAME_CACHE


def render_caption(template: str, ctx: dict) -> str:
    text = template
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", str(v or ""))
    return text.strip()


async def get_caption_template() -> str:
    tmpl = repo.get_setting("caption_template")
    return tmpl if tmpl else "{caption}\n\n[ @bot ]"


def _make_taglink(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


async def build_button_keyboard(code: str) -> dict:
    """Return an inline keyboard with a 'Get File' button (deep link)."""
    uname = await get_bot_username()
    link = _make_taglink(uname, code)
    return {"inline_keyboard": [[{"text": "📥 Get File", "url": link}]]}


async def post_to_main_channel(post: dict, chat_id: int, caption_override: str | None = None) -> Optional[int]:
    """Deliver one post to one main channel. Returns the main message_id."""
    source_chat = int(post["source_chat_id"])
    source_msg = int(post["source_message_id"])
    caption = caption_override if caption_override is not None else (post.get("caption") or "")

    cos = caption or ""
    # Build caption with the button (deep link) appended.
    uname = await get_bot_username()
    link = _make_taglink(uname, post["code"])
    full_caption = f"{cos}\n\n📥 <a href=\"{link}\">Get This File</a>"

    media_kind = post.get("media_kind") or "text"

    try:
        if media_kind in ("photo", "video", "document", "audio"):
            # Prefer byte-identical copy from the source channel (still carries caption below).
            sent = await copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat,
                message_id=source_msg,
                caption=full_caption,
            )
        else:
            sent = await send_message(chat_id=chat_id, text=full_caption)
    except Exception:
        # Fallback: if copyMessage fails (e.g. source deleted), try stored file_id.
        fid = post.get("file_id")
        if media_kind == "photo" and fid:
            sent = await send_photo(chat_id, fid, caption=full_caption)
        elif media_kind == "video" and fid:
            sent = await send_video(chat_id, fid, caption=full_caption)
        elif media_kind == "document" and fid:
            sent = await send_document(chat_id, fid, caption=full_caption)
        elif media_kind == "audio" and fid:
            sent = await send_audio(chat_id, fid, caption=full_caption)
        else:
            sent = await send_message(chat_id=chat_id, text=full_caption)

    main_msg_id = sent.get("message_id") if isinstance(sent, dict) else None

    # Record the copy.
    db.execute(
        "INSERT INTO post_copies (post_id, target_chat_id, message_id) VALUES (?, ?, ?) "
        "ON CONFLICT(post_id, target_chat_id) DO UPDATE SET message_id = excluded.message_id",
        (post["id"], chat_id, main_msg_id),
    )
    # Mark posted.
    if not post.get("posted_at"):
        db.execute("UPDATE posts SET posted_at = ? WHERE id = ?", (now_iso(), post["id"]))
    # Back up main message id.
    if main_msg_id:
        db.execute("UPDATE posts SET main_message_id = ? WHERE id = ?", (main_msg_id, post["id"]))

    return main_msg_id


async def publish_post_to_mains(post: dict) -> int:
    """Publish to all configured main channels. Returns how many succeeded."""
    mains = repo.get_main_channels()
    sent = 0
    for ch in mains:
        try:
            await post_to_main_channel(post, int(ch["telegram_chat_id"]))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[posting] failed to {ch['telegram_chat_id']}: {exc}")
    return sent


async def deliver_file_to_user(user_id: int, post: dict) -> bool:
    """Deliver the primary media to a user in DM by copying from source."""
    source_chat = int(post["source_chat_id"])
    source_msg = int(post["source_message_id"])
    caption = post.get("caption")

    # Deliver the main media.
    await copy_message(
        chat_id=user_id,
        from_chat_id=source_chat,
        message_id=source_msg,
        caption=caption or None,
    )
    # Deliver extra files (PDFs) if any.
    extra = json.loads(post.get("extra_files") or "[]")
    for f in extra:
        fid = f.get("file_id")
        if fid:
            if f.get("kind") == "document":
                await send_document(user_id, fid)
            elif f.get("kind") == "audio":
                await send_audio(user_id, fid)
            elif f.get("kind") == "photo":
                await send_photo(user_id, fid)
            elif f.get("kind") == "video":
                await send_video(user_id, fid)
    # Track fetch counts.
    db.execute(
        "UPDATE users SET files_fetched = files_fetched + 1, "
        "files_fetched_today = CASE WHEN last_fetch_day = date('now') "
        "  THEN files_fetched_today + 1 ELSE 1 END, "
        "last_fetch_day = date('now'), last_fetch_at = ? WHERE telegram_user_id = ?",
        (now_iso(), user_id),
    )
    return True
