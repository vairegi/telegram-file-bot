"""v2.7: /broadcast (copy a replied post to all known users) + /favsall (top savers)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..services import repo
from ..utils import clean_caption, esc, first_line
from .setup_cmds import _reject_non_admin

log = logging.getLogger("admin_stats")
router = Router(name="admin_stats")

PAGE_SIZE = 5
TOP_TITLES = 3


# ------------------------- /broadcast -------------------------
@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot) -> None:
    """Reply to ANY message (coverpost, text, photo, document…) with /broadcast
    and it is copy_message'd to every known user. Rate-safe: 20 msg/s."""
    if await _reject_non_admin(msg):
        return
    src = msg.reply_to_message
    if not src:
        await msg.reply(
            "Reply to the message you want to broadcast with <code>/broadcast</code>.\n"
            "Works with text, coverposts, photos, documents — anything.",
            parse_mode="HTML")
        return
    rows = repo.query_all("SELECT user_id FROM user_directory")
    if not rows:
        await msg.reply("💤 No known users yet.")
        return
    await msg.reply(f"📣 Broadcasting to <b>{len(rows)}</b> known user(s)…",
                    parse_mode="HTML")

    async def _run():
        sent = failed = 0
        for row in rows:
            uid = int(row["user_id"])
            try:
                await bot.copy_message(chat_id=uid,
                                       from_chat_id=src.chat.id,
                                       message_id=src.message_id)
                sent += 1
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
def _favsall_text(page: int) -> tuple[str, int]:
    total_users = repo.savers_total()
    total_saves = repo.saves_total()
    rows = repo.top_savers(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"🏆 <b>Top savers</b> ({total_users} users, {total_saves} saves) — "
             f"page {page + 1}/{pages}", ""]
    if not rows:
        lines.append("💤 No saves yet.")
        return ("\n".join(lines), pages)

    dir_map = repo.get_directory_users([int(r["user_id"]) for r in rows])
    for rank, r in enumerate(rows, start=page * PAGE_SIZE + 1):
        uid = int(r["user_id"])
        info = dir_map.get(uid) or {}
        name = (f"@{info['username']}" if info.get("username")
                else (info.get("first_name") or f"user {uid}"))
        lines.append(f'#{rank} 👤 <a href="tg://user?id={uid}">{esc(name)}</a> '
                     f'· <b>{r["saves"]}</b> saves')
        favs = repo.favorite_covers_of_user(uid, limit=TOP_TITLES)
        total_user = repo.favorites_count_of_user(uid)
        for frow in favs:
            t = first_line(clean_caption(frow.get("caption")), 48) or "Untitled"
            lines.append(f"  • {esc(t)}")
        extra = total_user - len(favs)
        if extra > 0:
            lines.append(f"  • <i>+{extra} more</i>")
        lines.append("")
    return ("\n".join(lines).strip(), pages)


def _favsall_kb(page: int, pages: int):
    if pages <= 1:
        return None
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton(text="⬅️ Prev",
                                         callback_data=f"favsall:{page - 1}"))
    btns.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="favsall:noop"))
    if page < pages - 1:
        btns.append(InlineKeyboardButton(text="➡️ Next",
                                         callback_data=f"favsall:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


@router.message(Command("favsall"))
async def cmd_favsall(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    text, pages = _favsall_text(0)
    await msg.reply(text, parse_mode="HTML",
                    reply_markup=_favsall_kb(0, pages),
                    disable_web_page_preview=True)


@router.callback_query(lambda c: (c.data or "").startswith("favsall:"))
async def on_favsall_page(cb: CallbackQuery) -> None:
    raw = (cb.data or "").split(":", 1)[1]
    if raw == "noop":
        await cb.answer()
        return
    try:
        page = max(0, int(raw))
    except Exception:
        await cb.answer("Bad page.")
        return
    text, pages = _favsall_text(page)
    try:
        await cb.message.edit_text(text, parse_mode="HTML",
                                   reply_markup=_favsall_kb(page, pages),
                                   disable_web_page_preview=True)
    except Exception:
        pass
    await cb.answer()
