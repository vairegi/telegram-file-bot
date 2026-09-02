"""MTProto userbot commands: login + backfill."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import userbot as ub
from ..utils import esc, parse_channel_id, to_int
from .setup_cmds import _reject_non_admin

log = logging.getLogger("backfill_cmds")
router = Router(name="backfill_cmds")


@router.message(Command("tgsetapi"))
async def cmd_tgsetapi(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/tgsetapi &lt;api_id&gt; &lt;api_hash&gt;</code>",
                        parse_mode="HTML")
        return
    api_id = to_int(parts[1])
    if not api_id:
        await msg.reply("❌ api_id must be numeric.")
        return
    await ub.set_api_creds(int(api_id), parts[2])
    await msg.reply("✅ Saved MTProto API credentials. Now: /tglogin +phone")


@router.message(Command("tglogin"))
async def cmd_tglogin(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/tglogin +&lt;phone&gt;</code>", parse_mode="HTML")
        return
    try:
        await ub.request_login_code(parts[1])
    except Exception as e:
        await msg.reply(f"❌ {esc(str(e))}", parse_mode="HTML")
        return
    await msg.reply("📩 Code sent. Reply with <code>/tgcode &lt;code&gt;</code>.",
                    parse_mode="HTML")


@router.message(Command("tgcode"))
async def cmd_tgcode(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/tgcode &lt;code&gt;</code>", parse_mode="HTML")
        return
    try:
        await ub.complete_login_with_code(parts[1])
    except Exception as e:
        await msg.reply(f"❌ {esc(str(e))}", parse_mode="HTML")
        return
    await msg.reply("✅ Logged in. Session saved. You can /backfill_start now.")


@router.message(Command("tgstatus"))
async def cmd_tgstatus(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    if not ub.telethon_available():
        await msg.reply("❌ telethon not installed on the server.")
        return
    try:
        info = await ub.get_me_info()
    except Exception as e:
        await msg.reply(f"⚠️ Not logged in / error: <code>{esc(str(e))}</code>",
                        parse_mode="HTML")
        return
    await msg.reply(
        f"✅ MTProto logged in\n"
        f"👤 @{info.get('username') or '-'}  "
        f"(<code>{info.get('id')}</code>)\n"
        f"📞 {info.get('phone') or '-'}",
        parse_mode="HTML",
    )


@router.message(Command("backfill_start"))
async def cmd_backfill_start(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backfill_start &lt;db_chat_id&gt; [from_id]</code>",
                        parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    from_id = to_int(parts[2]) if len(parts) > 2 else 1
    if not chan:
        await msg.reply("❌ Bad chat id.")
        return
    ok, txt = await ub.start_backfill(bot, msg.from_user.id, chan, from_id=from_id or 1)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("backfill_resume"))
async def cmd_backfill_resume(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backfill_resume &lt;db_chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    if not chan:
        await msg.reply("❌ Bad chat id.")
        return
    ok, txt = await ub.resume_backfill(bot, msg.from_user.id, chan)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("backfill_stop"))
async def cmd_backfill_stop(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    ok, txt = ub.stop_backfill()
    await msg.reply(txt)


@router.message(Command("backfill_status"))
async def cmd_backfill_status(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await msg.reply(ub.render_status(), parse_mode="HTML")


@router.message(Command("backfill_reset"))
async def cmd_backfill_reset(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await msg.reply(ub.reset_backfill_state())
