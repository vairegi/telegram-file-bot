"""v2.7: /broadcast (copy a replied post to all known users) + /favsall (top savers)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..services import repo
from ..utils import clean_caption, esc, first_line
from .setup_cmds import _reject_non_admin

log = logging.getLogger("admin_stats")
router = Router(name="admin_stats")

TOP_TITLES = 3
_MSG_LIMIT = 3900        # stay under Telegram's 4096-char cap
_MIN_PER_PAGE = 5        # never show fewer than this
_MAX_PER_PAGE = 30       # hard cap per page


# ------------------------- /broadcast -------------------------
@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot) -> None:
    """Reply to ANY message (coverpost, text, photo, document…) with /broadcast.

    Tag rule (v3.2): if the replied message is itself a forward (carries a
    "Forwarded from …" tag), we FORWARD it so the tag is preserved. Otherwise
    we copy_message it (clean, no tag). Rate-safe: ~20 msg/s + RetryAfter.
    """
    if await _reject_non_admin(msg):
        return
    src = msg.reply_to_message
    if not src:
        await msg.reply(
            "Reply to the message you want to broadcast with <code>/broadcast</code>.\n"
            "Works with text, coverposts, photos, documents — anything.",
            parse_mode="HTML")
        return
    uids = await repo.all_user_ids()
    if not uids:
        await msg.reply("💤 No known users yet.")
        return
    has_tag = bool(
        getattr(src, "forward_origin", None)
        or getattr(src, "forward_from", None)
        or getattr(src, "forward_from_chat", None)
        or getattr(src, "forward_sender_name", None))
    mode = "forward (tag kept)" if has_tag else "copy (no tag)"
    await msg.reply(f"📣 Broadcasting to <b>{len(uids)}</b> known user(s)… "
                    f"mode: <b>{mode}</b>", parse_mode="HTML")

    async def _run():
        async def _deliver(uid: int) -> None:
            if has_tag:
                await bot.forward_message(chat_id=uid,
                                          from_chat_id=src.chat.id,
                                          message_id=src.message_id)
            else:
                await bot.copy_message(chat_id=uid,
                                       from_chat_id=src.chat.id,
                                       message_id=src.message_id)

        sent = failed = 0
        for uid in uids:
            uid = int(uid)
            try:
                await _deliver(uid)
                sent += 1
            except TelegramRetryAfter as ra:
                # Telegram told us exactly how long to rest — honor it, retry once.
                await asyncio.sleep(float(getattr(ra, "retry_after", 5)) + 1)
                try:
                    await _deliver(uid)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # ~20/s, flood-safe
        try:
            await bot.send_message(
                msg.chat.id,
                f"📣 <b>Broadcast complete</b> — sent: <b>{sent}</b>, failed: <b>{failed}</b>",
                parse_mode="HTML")
        except Exception:
            pass

    asyncio.create_task(_run())


# ------------------------- /favsall (paged) -------------------------


async def _resolve_names(bot, user_ids: list[int]) -> dict:
    """Directory first; for unknown ids do a live getChat and cache the result
    so later pages are free."""
    out = await repo.get_directory_users(user_ids)
    for uid in user_ids:
        if uid not in out:
            try:
                chat = await bot.get_chat(uid)
                uname = getattr(chat, "username", None)
                fname = getattr(chat, "first_name", "") or ""
                await repo.upsert_directory_user(uid, uname, fname)
                out[uid] = {"user_id": uid, "username": uname, "first_name": fname}
            except Exception:
                pass
    return out

async def _favsall_text(bot, page: int) -> tuple[str, int, int]:
    """Pack as MANY savers per page as fit under Telegram's 4096-char limit.
    Returns (text, pages, page_size_used)."""
    total_users = await repo.savers_total()
    total_saves = await repo.saves_total()

    # Pull a generous window of savers, then pack entries until we hit the
    # message size budget. Page size is therefore dynamic (usually 15–30).
    window = await repo.top_savers(limit=_MAX_PER_PAGE, offset=page * _MAX_PER_PAGE)
    if not window and page > 0:
        page = 0
        window = await repo.top_savers(limit=_MAX_PER_PAGE, offset=0)
    dir_map = await _resolve_names(bot, [int(r["user_id"]) for r in window])

    header = ""
    lines: list[str] = []
    used = 0
    for r in window:
        uid = int(r["user_id"])
        info = dir_map.get(uid) or {}
        name = (f"@{info['username']}" if info.get("username")
                else (info.get("first_name") or f"user {uid}"))
        entry_lines = [f'#{page * _MAX_PER_PAGE + used + 1} 👤 '
                       f'<a href="tg://user?id={uid}">{esc(name)}</a> '
                       f'· <b>{r["saves"]}</b> saves']
        favs = await repo.favorite_covers_of_user(uid, limit=TOP_TITLES)
        total_user = await repo.favorites_count_of_user(uid)
        for frow in favs:
            t = first_line(clean_caption(frow.get("caption")), 48) or "Untitled"
            entry_lines.append(f"  • {esc(t)}")
        extra = total_user - len(favs)
        if extra > 0:
            entry_lines.append(f"  • <i>+{extra} more</i>")
        entry_lines.append("")
        entry_len = sum(len(x) for x in entry_lines) + 2
        if used >= _MIN_PER_PAGE and (len(header) + entry_len) > _MSG_LIMIT:
            break
        lines.extend(entry_lines)
        used += 1

    page_size = max(used, 1)
    pages = max(1, (total_users + page_size - 1) // page_size)
    header = (f"🏆 <b>Top savers</b> ({total_users} users, {total_saves} saves) — "
              f"page {page + 1}/{pages}\n\n")
    if not lines:
        lines = ["💤 No saves yet."]
    return (header + "\n".join(lines).strip(), pages, page_size)


def _favsall_kb(page: int, pages: int):
    if pages <= 1:
        return None
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton(text="⬅️ Prev",
                                         callback_data=f"favsall:{page - 1}"))
    btns.append(InlineKeyboardButton(text=f"{page + 1}/{pages}",
                                     callback_data="favsall:noop"))
    if page < pages - 1:
        btns.append(InlineKeyboardButton(text="➡️ Next",
                                         callback_data=f"favsall:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


@router.message(Command("favsall"))
async def cmd_favsall(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    text, pages, _ = await _favsall_text(bot, 0)
    await msg.reply(text, parse_mode="HTML",
                    reply_markup=_favsall_kb(0, pages),
                    disable_web_page_preview=True)


@router.callback_query(lambda c: (c.data or "").startswith("favsall:"))
async def on_favsall_page(cb: CallbackQuery, bot: Bot) -> None:
    raw = (cb.data or "").split(":", 1)[1]
    if raw == "noop":
        await cb.answer()
        return
    try:
        page = max(0, int(raw))
    except Exception:
        await cb.answer("Bad page.")
        return
    text, pages, _ = await _favsall_text(bot, page)
    try:
        await cb.message.edit_text(text, parse_mode="HTML",
                                   reply_markup=_favsall_kb(page, pages),
                                   disable_web_page_preview=True)
    except Exception:
        pass
    await cb.answer()
