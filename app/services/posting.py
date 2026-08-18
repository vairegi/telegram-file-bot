"""Posting / delivery engine.

Contract (per user spec):
- Only COVER posts are published to Main channel(s). PDFs are NEVER copied
  to Main; they're only DM'd to users when they tap "📥 Get File #N".
- Cover caption layout on Main:
    Line 1: <title>            (first non-empty line of original caption)
    Line 2: #<N>               (numbering, right below the title)
    Line 3+: <rest of caption>
    <blank>
    <postcaption extra>        (if configured via /postcaption)
- Main-channel keyboard has ONLY the "📥 Get File #N" button.
- DM delivery:
    * cover is sent first (copy_message) with NO Save/Remove buttons.
    * each attached PDF is sent (copy_message) with ❤️ Save / 🗑 Remove buttons,
      and the file caption gets any /filecaption extra appended.
- /protect 1|0 sets a global flag that forces protect_content on ALL sends
  (Main channel posts AND DM deliveries), so files cannot be forwarded.
- The queue view (/queueinfo) is always consistent because we call
  repo.mark_published() the moment the Main-channel send succeeds.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import repo, tg
from ..utils import esc

log = logging.getLogger("posting")

# Latest publish error (surfaced by /dripnow diagnostics)
LAST_PUBLISH_ERROR: str = ""

# ---------------------- settings helpers ----------------------------
def _protect() -> bool:
    return repo.get_setting_bool("protect_content", False)


def _postcaption_extra() -> str:
    return (repo.get_setting("postcaption_extra") or "").strip()


def _filecaption_extra() -> str:
    return (repo.get_setting("filecaption_extra") or "").strip()


def _paused() -> bool:
    return repo.get_setting_bool("posting_paused", False)


# ---------------------- caption builders ----------------------------
def _split_title_body(text: Optional[str]) -> tuple[str, str]:
    """Return (title, body) where title is the first non-empty line."""
    if not text:
        return ("", "")
    lines = text.splitlines()
    title = ""
    body_lines: List[str] = []
    for i, line in enumerate(lines):
        if not title and line.strip():
            title = line.strip()
            body_lines = lines[i + 1:]
            break
    body = "\n".join(body_lines).lstrip("\n")
    return (title, body)


def build_cover_caption(caption: Optional[str], number: int) -> str:
    """Assemble the Main-channel caption with #N on line 2."""
    title, body = _split_title_body(caption)
    parts: List[str] = []
    if title:
        parts.append(esc(title))
    parts.append(f"<b>#{number}</b>")
    if body:
        parts.append(esc(body))
    extra = _postcaption_extra()
    if extra:
        parts.append("")
        parts.append(extra)  # user-supplied HTML allowed
    return "\n".join(parts).strip()


def build_pdf_caption(caption: Optional[str], number: int, index: int, total: int) -> str:
    """Caption for a single PDF DM. Adds #N header, position, and filecaption extra."""
    lines: List[str] = [f"<b>#{number}</b>"]
    if total > 1:
        lines[0] += f" · file {index}/{total}"
    if caption:
        lines.append(esc(caption))
    extra = _filecaption_extra()
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines).strip()


# ---------------------- keyboards -----------------------------------
def kb_main_get_file(bot_username: str, code: str, number: int) -> InlineKeyboardMarkup:
    """Only the Get File button appears under a Main-channel cover post."""
    url = f"https://t.me/{bot_username}?start=get_{code}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"📥 Get File #{number}", url=url)]])


def kb_pdf_save(post_id: int, saved: bool) -> InlineKeyboardMarkup:
    """Save/Remove toggle shown ONLY on PDF DMs (not on covers)."""
    if saved:
        btn = InlineKeyboardButton(text="🗑 Remove Save", callback_data=f"unsave:{post_id}")
    else:
        btn = InlineKeyboardButton(text="❤️ Save", callback_data=f"save:{post_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ---------------------- bot username cache --------------------------
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


# ---------------------- Main-channel publish ------------------------
async def publish_cover_to_mains(bot: Bot, cover: dict) -> List[dict]:
    """Publish one cover post to every registered Main channel.

    The permanent #N is assigned AT PUBLISH TIME (mark_published), so queue order
    == true channel order and numbering can never go out of sequence.
    """
    mains = repo.get_main_channels()
    if not mains:
        log.warning("no main channels configured; skipping publish of id=%s", cover.get("id"))
        return []

    # Number to display: existing (repost) or predicted (first publish)
    if cover.get("post_number"):
        number = int(cover["post_number"])
    else:
        number = repo.predicted_number(int(cover["id"]))
    code = cover["code"]
    src_chat = cover["source_chat_id"]
    src_msg = cover["source_message_id"]
    caption = build_cover_caption(cover.get("caption"), number)
    protect = _protect()
    username = await get_bot_username(bot)
    kb = kb_main_get_file(username, code, number)

    results: List[dict] = []
    marked = False
    for m in mains:
        try:
            res = await tg.copy_message(
                bot, chat_id=m["chat_id"], from_chat_id=src_chat, message_id=src_msg,
                caption=caption, reply_markup=kb, protect_content=protect)
            mid = getattr(res, "message_id", None) or getattr(res, "id", None)
            results.append({"chat_id": m["chat_id"], "message_id": mid, "ok": True})
            if not marked and mid is not None:
                # Assign permanent #N at publish time — queue stays in sync.
                repo.mark_published(int(cover["id"]), int(m["chat_id"]), int(mid))
                marked = True
        except Exception as e:
            log.exception("publish to %s failed", m["chat_id"])
            global LAST_PUBLISH_ERROR
            LAST_PUBLISH_ERROR = f"chat={m['chat_id']}: {type(e).__name__}: {e}"
            results.append({"chat_id": m["chat_id"], "ok": False, "error": str(e)})
    return results


async def publish_next(bot: Bot, min_number: int = 1) -> Optional[dict]:
    """Publish the single next queued cover. Respects the paused flag."""
    if _paused():
        return None
    cover = repo.next_queued_cover(min_number)
    if not cover:
        return None
    await publish_cover_to_mains(bot, cover)
    return cover


async def publish_batch(bot: Bot, n: int, min_number: int = 1) -> List[dict]:
    """Publish up to n queued covers (used by /dripnow N and scheduled slots)."""
    published: List[dict] = []
    for _ in range(max(1, int(n))):
        if _paused():
            break
        cover = repo.next_queued_cover(min_number)
        if not cover:
            break
        await publish_cover_to_mains(bot, cover)
        published.append(cover)
    return published


# ---------------------- DM delivery (Get File) ----------------------
async def deliver_to_user(bot: Bot, user_id: int, cover: dict) -> dict:
    """Send the cover + all its attached PDFs to a user in DM.

    Cover has no Save button. Each PDF has ❤️ Save / 🗑 Remove.
    protect_content is respected.
    """
    from .users import list_favorites as _favs  # local import to avoid cycles

    protect = _protect()
    if cover.get("post_number"):
        number = int(cover["post_number"])
    else:
        number = repo.predicted_number(int(cover["id"]))

    # 1) send cover copy (no keyboard)
    cover_caption = build_cover_caption(cover.get("caption"), number)
    try:
        await tg.copy_message(
            bot, chat_id=user_id, from_chat_id=cover["source_chat_id"],
            message_id=cover["source_message_id"], caption=cover_caption,
            protect_content=protect)
    except Exception as e:
        log.exception("deliver cover failed for user %s", user_id)
        return {"ok": False, "error": str(e), "delivered": 0}

    # 2) send each PDF with Save/Remove
    pdfs = repo.pdfs_of_cover(int(cover["source_message_id"]), int(cover["source_chat_id"]))
    saved_ids = {int(pid) for pid in _favs(user_id)}
    total = len(pdfs)
    delivered = 0
    for i, pdf in enumerate(pdfs, start=1):
        cap = build_pdf_caption(pdf.get("caption"), number, i, total)
        kb = kb_pdf_save(int(pdf["id"]), saved=int(pdf["id"]) in saved_ids)
        try:
            await tg.copy_message(
                bot, chat_id=user_id, from_chat_id=pdf["source_chat_id"],
                message_id=pdf["source_message_id"], caption=cap,
                reply_markup=kb, protect_content=protect)
            delivered += 1
        except Exception as e:
            log.exception("deliver pdf %s failed", pdf.get("id"))
    return {"ok": True, "delivered": delivered, "total": total}


# ---------------------- legacy aliases (backfill/extras) -------------
async def post_to_main_channel(bot, cover, *args, **kwargs):
    """Back-compat alias for older modules."""
    return await publish_cover_to_mains(bot, cover)


async def publish_post_to_mains(bot, cover):
    return await publish_cover_to_mains(bot, cover)


async def deliver_file_to_user(bot, user_id, cover):
    return await deliver_to_user(bot, user_id, cover)


def render_caption(caption, number=0):
    return build_cover_caption(caption, int(number or 0))
