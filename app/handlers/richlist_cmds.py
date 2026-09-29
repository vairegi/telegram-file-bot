"""v4.7 — /ban list (rich table) and /verified_users (daily IST log).

Both render as InputRichBlockTable Rich Messages (Bot API 10.1+), with an
automatic plain-HTML fallback if Telegram rejects the rich table.
Admin-only.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..config import settings
from ..services import repo, richlists

log = logging.getLogger("richlist_cmds")
router = Router(name="richlist_cmds")


async def _reject_non_admin(msg: Message) -> bool:
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Admin only.")
    return True


@router.message(Command("ban"))
async def cmd_ban_or_list(msg: Message, bot: Bot, command: CommandObject) -> None:
    """`/ban list` -> rich ban table. Anything else keeps the old behaviour
    (delegate to shortener_cmds' /ban by doing nothing here when not 'list')."""
    args = (command.args or "").strip().lower()
    if args != "list":
        return  # let the existing /ban handler in shortener_cmds take it
    if await _reject_non_admin(msg):
        return
    try:
        rm, plain, total = await richlists.build_ban_list()
        if total == 0:
            await msg.reply("✅ No banned users.")
            return
        await richlists.send_rich_or_plain(
            bot, msg.chat.id, rm, plain,
            reply_to=getattr(msg, "message_id", 0))
    except Exception:
        log.exception("ban list failed")
        await msg.reply("❌ /ban list hit an error — check logs.")


@router.message(Command("verified_users"))
async def cmd_verified_users(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    try:
        rm, plain, users, verifs = await richlists.build_verified_list()
        if users == 0:
            await msg.reply(
                f"✅ Verified users — {richlists._ist_day()} (since midnight "
                f"IST): 0 users · 0 verifications")
            return
        await richlists.send_rich_or_plain(
            bot, msg.chat.id, rm, plain,
            reply_to=getattr(msg, "message_id", 0))
    except Exception:
        log.exception("verified_users failed")
        await msg.reply("❌ /verified_users hit an error — check logs.")
