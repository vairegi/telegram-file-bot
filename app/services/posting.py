"""Publish covers to main channel(s) + DM delivery on Get-File tap.

Design highlights:
  * Only kind='cover' rows can be published — files/stickers are physically
    incapable of reaching the main channel (SQL WHERE kind='cover').
  * Spoilers are FORCED ON regardless of the DB-channel original, per v2 spec.
    We achieve spoiler-on-old-posts via the "forward-to-log trick":
        1. If cover.file_id is already bot-usable → sendPhoto(has_spoiler=True)
           in ONE API call (fast path — used on republishes and new-live covers)
        2. Otherwise: bot.copy_message(db → log) to obtain a bot-usable file_id,
           then bot.send_photo(main, file_id, has_spoiler=True), delete the log
           copy, and CACHE the file_id back to posts.file_id so subsequent
           reposts take the fast path.
  * Post number #N is assigned atomically at publish time via a single UPDATE
    with a subquery (see repo.mark_published) — no race, no double-scan.
  * Caption layout (main channel):
        Line 1: title (first non-empty caption line)
        Line 2: <b>#N</b>
        Line 3+: rest of caption
        (blank)
        (postcaption extra, if set)
        [📥 Get File #N button]
"""
from __future__ import annotations

import logging
from typing import List, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import repo, tg
from ..utils import esc

log = logging.getLogger("posting")

LAST_PUBLISH_ERROR: str = ""


# ============================================================================
# Settings helpers (cached in repo)
# ============================================================================
def _protect() -> bool:
    return repo.get_setting_bool("protect_content", False)


def _spoiler() -> bool:
    return repo.get_setting_bool("spoiler", True)  # default ON per v2 spec


def _paused() -> bool:
    return repo.get_setting_bool("posting_paused", False)


def _postcaption_extra() -> str:
    return (repo.get_setting("postcaption_extra") or "").strip()


def _filecaption_extra() -> str:
    return (repo.get_setting("filecaption_extra") or "").strip()


# ============================================================================
# Caption + keyboard builders
# ============================================================================
def _split_title_body(text: Optional[str]) -> tuple[str, str]:
    if not text:
        return ("", "")
    lines = text.splitlines()
    title = ""
    body_lines: list[str] = []
    for i, line in enumerate(lines):
        if not title and line.strip():
            title = line.strip()
            body_lines = lines[i + 1:]
            break
    body = "\n".join(body_lines).lstrip("\n")
    return (title, body)


def build_cover_caption(caption: Optional[str], number: int) -> str:
    title, body = _split_title_body(caption)
    parts: list[str] = []
    if title:
        parts.append(esc(title))
    parts.append(f"<b>#{number}</b>")
    if body:
        parts.append(esc(body))
    extra = _postcaption_extra()
    if extra:
        parts.append("")
        parts.append(extra)
    return "\n".join(parts).strip()


def build_file_caption(caption: Optional[str], number: int,
                       index: int, total: int) -> str:
    # Per user spec: "File #N" header on every delivered file.
    lines: list[str] = [f"<b>File #{number}</b>"]
    if total > 1:
        lines[0] += f" · {index}/{total}"
    if caption:
        lines.append(esc(caption))
    extra = _filecaption_extra()
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines).strip()


def kb_main_get_file(bot_username: str, code: str, number: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=get_{code}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"📥 Get File #{number}", url=url)]])


def kb_file_save(post_id: int, saved: bool) -> InlineKeyboardMarkup:
    if saved:
        btn = InlineKeyboardButton(text="🗑 Remove Save", callback_data=f"unsave:{post_id}")
    else:
        btn = InlineKeyboardButton(text="❤️ Save", callback_data=f"save:{post_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ============================================================================
# Bot username cache
# ============================================================================
_bot_username_cache: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        me = await tg.get_me(bot)
        _bot_username_cache = me.username or ""
    except Exception:
        _bot_username_cache = ""
    return _bot_username_cache


# ============================================================================
# The spoiler-forward trick (improved: single-shot on cache hit)
# ============================================================================
async def _obtain_bot_file_id(bot: Bot, cover: dict) -> Optional[str]:
    """Return a file_id the bot can send. If cached in the row, use it.
    Otherwise round-trip via the log channel to mint a fresh one, then
    cache it back to posts.file_id so we never pay this cost again for
    this cover.

    Returns None if no log channel is configured OR the trick fails —
    caller falls back to copy_message (no spoiler).
    """
    cached = (cover.get("file_id") or "").strip()
    if cached:
        return cached

    log_ch = repo.get_log_channel()
    if not log_ch:
        return None

    try:
        # Copy DB→log to obtain a bot-owned copy.
        res = await bot.copy_message(
            chat_id=int(log_ch["chat_id"]),
            from_chat_id=int(cover["source_chat_id"]),
            message_id=int(cover["source_message_id"]),
        )
        log_mid = getattr(res, "message_id", None) or getattr(res, "id", None)
        if not log_mid:
            return None

        # Fetch the copy so we can read its photo/document file_id.
        # aiogram doesn't have get_message, so we use forward_message trick:
        # instead, we ask the bot to look at the log channel's chat history
        # via the copy return… actually the copy result is a MessageId only.
        # Workaround: send a fresh copy to log channel with a specific tag,
        # then look up chat via get_chat + last message? — not reliable.
        #
        # Cleanest approach: use bot.forward_message back from log → log to
        # get a full Message object, or use get_chat + get_history (Bot API
        # doesn't expose get_history). aiogram-3: bot(GetHistory) NOT
        # available for bots.
        #
        # Reliable path: copy DB→log with a fresh caption we control, then
        # immediately forward log→log to get the full Message.
        fwd = await bot.forward_message(
            chat_id=int(log_ch["chat_id"]),
            from_chat_id=int(log_ch["chat_id"]),
            message_id=int(log_mid),
        )
        file_id = None
        if getattr(fwd, "photo", None):
            biggest = fwd.photo[-1]
            file_id = getattr(biggest, "file_id", None)
        elif getattr(fwd, "video", None):
            file_id = getattr(fwd.video, "file_id", None)
        elif getattr(fwd, "document", None):
            file_id = getattr(fwd.document, "file_id", None)

        # Clean up both log copies.
        await tg.delete_message(bot, chat_id=int(log_ch["chat_id"]),
                                message_id=int(log_mid))
        fwd_mid = getattr(fwd, "message_id", None)
        if fwd_mid:
            await tg.delete_message(bot, chat_id=int(log_ch["chat_id"]),
                                    message_id=int(fwd_mid))

        if file_id:
            try:
                repo.update_file_id(int(cover["id"]), file_id)
            except Exception:
                pass
        return file_id
    except Exception as e:
        log.warning("spoiler-forward trick failed for cover %s: %s",
                    cover.get("id"), e)
        return None


# ============================================================================
# Publish
# ============================================================================
async def publish_cover_to_mains(bot: Bot, cover: dict) -> List[dict]:
    """Publish ONE cover to every registered main channel."""
    global LAST_PUBLISH_ERROR
    mains = repo.get_main_channels()
    if not mains:
        LAST_PUBLISH_ERROR = "no main channels configured"
        return []

    # Only covers can be published.
    if (cover.get("kind") or "") != "cover":
        LAST_PUBLISH_ERROR = f"post id={cover.get('id')} kind={cover.get('kind')} is not a cover"
        return []

    # Compute #N: predicted for now, atomically finalized in mark_published.
    number = int(cover.get("post_number") or 0) or (repo.highest_post_number() + 1)
    code = cover["code"]
    caption = build_cover_caption(cover.get("caption"), number)
    protect = _protect()
    spoiler_on = _spoiler()
    media_kind = (cover.get("media_kind") or "").lower()
    username = await get_bot_username(bot)
    kb = kb_main_get_file(username, code, number)

    # Decide send strategy per media kind.
    file_id: Optional[str] = None
    if media_kind in ("photo", "video"):
        if spoiler_on:
            file_id = await _obtain_bot_file_id(bot, cover)
        else:
            file_id = (cover.get("file_id") or "").strip() or None

    async def _send(main_chat_id: int):
        # Spoiler-capable path: sendPhoto / sendVideo with has_spoiler
        if media_kind == "photo" and file_id:
            return await tg.send_photo(
                bot, chat_id=main_chat_id, photo=file_id, caption=caption,
                reply_markup=kb, protect_content=protect,
                has_spoiler=spoiler_on,
            )
        if media_kind == "video" and file_id:
            return await tg.send_video(
                bot, chat_id=main_chat_id, video=file_id, caption=caption,
                reply_markup=kb, protect_content=protect,
                has_spoiler=spoiler_on,
            )
        # Fallback: copyMessage (no spoiler possible).
        return await tg.copy_message(
            bot, chat_id=main_chat_id,
            from_chat_id=int(cover["source_chat_id"]),
            message_id=int(cover["source_message_id"]),
            caption=caption, reply_markup=kb, protect_content=protect,
        )

    results: list[dict] = []
    finalised = False
    for m in mains:
        try:
            res = await _send(int(m["chat_id"]))
            mid = getattr(res, "message_id", None) or getattr(res, "id", None)
            results.append({"chat_id": int(m["chat_id"]), "message_id": mid, "ok": True})
            if not finalised and mid is not None:
                # Atomic #N assignment + cache file_id for future reuse.
                actual_n = repo.mark_published(
                    int(cover["id"]), int(m["chat_id"]), int(mid),
                    file_id=file_id or None,
                )
                if actual_n:
                    number = actual_n
                finalised = True
        except Exception as e:
            log.exception("publish to %s failed", m["chat_id"])
            LAST_PUBLISH_ERROR = f"chat={m['chat_id']}: {type(e).__name__}: {e}"
            results.append({"chat_id": int(m["chat_id"]), "ok": False, "error": str(e)})
    return results


async def publish_next(bot: Bot) -> Optional[dict]:
    if _paused():
        return None
    cover = repo.next_queued_cover()
    if not cover:
        return None
    results = await publish_cover_to_mains(bot, cover)
    return cover if any(r.get("ok") for r in results) else None


async def publish_batch(bot: Bot, n: int) -> list[dict]:
    """Publish up to N queued covers. Stops on total send failure."""
    published: list[dict] = []
    for _ in range(max(1, int(n))):
        if _paused():
            break
        cover = repo.next_queued_cover()
        if not cover:
            break
        results = await publish_cover_to_mains(bot, cover)
        if any(r.get("ok") for r in results):
            fresh = repo.get_post_by_id(int(cover["id"])) or cover
            published.append(fresh)
        else:
            break
    return published


# ============================================================================
# DM delivery — user tapped 📥 Get File
# ============================================================================
async def deliver_to_user(bot: Bot, user_id: int, cover: dict) -> dict:
    """DM the cover (spoiler if ON) + each attached file to a user."""
    protect = _protect()
    spoiler_on = _spoiler()
    number = int(cover.get("post_number") or 0)
    cover_caption = build_cover_caption(cover.get("caption"), number)
    media_kind = (cover.get("media_kind") or "").lower()

    # Cover — spoiler if we have (or can mint) a bot file_id.
    try:
        fid = (cover.get("file_id") or "").strip() or None
        if spoiler_on and media_kind == "photo" and not fid:
            fid = await _obtain_bot_file_id(bot, cover)
        if media_kind == "photo" and fid:
            await tg.send_photo(
                bot, chat_id=user_id, photo=fid, caption=cover_caption,
                protect_content=protect, has_spoiler=spoiler_on,
            )
        elif media_kind == "video" and fid:
            await tg.send_video(
                bot, chat_id=user_id, video=fid, caption=cover_caption,
                protect_content=protect, has_spoiler=spoiler_on,
            )
        else:
            await tg.copy_message(
                bot, chat_id=user_id,
                from_chat_id=int(cover["source_chat_id"]),
                message_id=int(cover["source_message_id"]),
                caption=cover_caption, protect_content=protect,
            )
    except Exception as e:
        log.exception("deliver cover failed for user %s", user_id)
        return {"ok": False, "error": str(e), "delivered": 0}

    files = repo.files_of_cover(
        int(cover["source_chat_id"]), int(cover["source_message_id"]))
    fav_ids = {int(f["id"]) for f in repo.list_favorites(user_id)}
    total = len(files)
    delivered = 0
    for i, fpost in enumerate(files, start=1):
        cap = build_file_caption(fpost.get("caption"), number, i, total)
        fmk = (fpost.get("media_kind") or "").lower()
        try:
            if fmk == "sticker":
                # Stickers don't accept captions or Save buttons.
                await tg.copy_message(
                    bot, chat_id=user_id,
                    from_chat_id=int(fpost["source_chat_id"]),
                    message_id=int(fpost["source_message_id"]),
                    protect_content=protect,
                )
            else:
                kb = kb_file_save(int(fpost["id"]),
                                  saved=int(fpost["id"]) in fav_ids)
                await tg.copy_message(
                    bot, chat_id=user_id,
                    from_chat_id=int(fpost["source_chat_id"]),
                    message_id=int(fpost["source_message_id"]),
                    caption=cap, reply_markup=kb, protect_content=protect,
                )
            delivered += 1
        except Exception:
            log.exception("deliver file %s failed", fpost.get("id"))
    return {"ok": True, "delivered": delivered, "total": total}
