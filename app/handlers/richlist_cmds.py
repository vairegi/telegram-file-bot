"""v4.8 — /banlist, /verified_users, /banmessage."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import settings
from ..services import repo, richlists
from ..utils import esc

log = logging.getLogger("richlist_cmds")
router = Router(name="richlist_cmds")


async def _reject_non_admin(msg: Message) -> bool:
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Admin only.")
    return True


# ------------------------- /banlist -------------------------
@router.message(Command("banlist"))
async def cmd_banlist(msg: Message, bot: Bot) -> None:
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
        log.exception("banlist failed")
        await msg.reply("❌ /banlist hit an error — check logs.")


# ------------------------- /verified_users -------------------------
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


# ------------------------- /banmessage -------------------------
_BAN_HELP = (
    "Usage:\n"
    "  <code>/banmessage &lt;html&gt;</code> — set the ban DM (HTML)\n"
    "  <code>/banmessage</code> — show the current one\n"
    "  <code>/banmessage clear</code> — reset to default\n"
    "  <code>/banmessage preview</code> — DM yourself a preview\n\n"
    "Supported HTML: <b>bold</b>, <i>italic</i>, <u>underline</u>, <s>strike</s>, "
    "<code>code</code>, <pre>pre</pre>, "
    "<a href=\"https://t.me\">links</a>, <blockquote>quote</blockquote>.\n"
    "Placeholders: <code>{elapsed}</code> (seconds), <code>{user_id}</code>.")


@router.message(Command("banmessage"))
async def cmd_banmessage(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    text = (msg.text or "")
    _, _, arg = text.partition(" ")
    arg = arg.strip()

    if not arg:
        cur = await richlists.get_ban_message()
        await msg.reply(f"Current ban message:\n<code>{esc(cur)}</code>\n\n"
                        + _BAN_HELP, parse_mode="HTML",
                        disable_web_page_preview=True)
        return
    if arg.lower() == "clear":
        await repo.set_setting("ban_message", None)
        await msg.reply("✅ Ban message reset to default.")
        return
    if arg.lower() == "preview":
        rendered = richlists.render_ban_message(
            await richlists.get_ban_message(), elapsed=16.4,
            user_id=msg.from_user.id)
        await msg.reply("Preview (elapsed=16.4, user_id=you):",
                        parse_mode="HTML")
        try:
            await bot.send_message(msg.from_user.id, rendered,
                                   parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Exception:
            await msg.reply(rendered, parse_mode="HTML",
                            disable_web_page_preview=True)
        return

    # Validate: send it to the admin as a preview; if Telegram rejects the
    # HTML we surface the error and DO NOT save the broken template.
    try:
        await bot.send_message(msg.from_user.id,
                               richlists.render_ban_message(
                                   arg, elapsed=16.4,
                                   user_id=msg.from_user.id),
                               parse_mode="HTML",
                               disable_web_page_preview=True)
    except Exception as e:
        await msg.reply(f"❌ HTML rejected by Telegram — not saved.\n"
                        f"<code>{esc(str(e))}</code>", parse_mode="HTML")
        return
    await repo.set_setting("ban_message", arg)
    await msg.reply("✅ Ban message updated. Preview sent to your DM.")
