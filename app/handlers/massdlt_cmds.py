"""MTProto bulk delete: /massdlt /massdlt_stop /massdlt_status."""
from __future__ import annotations

import logging
import re
from typing import Optional

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import userbot as ub
from ..utils import esc, parse_channel_id
from .setup_cmds import _reject_non_admin

log = logging.getLogger("massdlt_cmds")
router = Router(name="massdlt_cmds")


@router.message(Command("massdlt"))
async def cmd_massdlt(msg: Message, bot: Bot) -> None:
    """Usage: /massdlt <chat_id> <start_link> <end_link>
       or:     /massdlt <start_link> <end_link>"""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply(
            "Usage: <code>/massdlt &lt;chat_id&gt; &lt;start_link&gt; &lt;end_link&gt;</code>\n"
            "  or:  <code>/massdlt &lt;start_link&gt; &lt;end_link&gt;</code>",
            parse_mode="HTML")
        return

    chat_id: Optional[int] = None
    start_link = end_link = ""

    if len(parts) >= 4:
        chat_id = parse_channel_id(parts[1])
        start_link, end_link = parts[2], parts[3]
    else:
        start_link, end_link = parts[1], parts[2]

    p1 = ub.parse_massdlt_link(start_link)
    p2 = ub.parse_massdlt_link(end_link)
    if not p1 or not p2:
        await msg.reply("❌ Could not parse one or both t.me links.")
        return
    (cid1, mid1) = p1
    (cid2, mid2) = p2

    if chat_id is None:
        if cid1 and cid2 and cid1 != cid2:
            await msg.reply("❌ The two links belong to different chats.")
            return
        chat_id = cid1 or cid2
    if not chat_id:
        m = re.search(r"t\.me/([A-Za-z0-9_]+)/", start_link)
        if not m:
            await msg.reply("❌ Cannot determine chat id.")
            return
        chat_id = await ub._resolve_public_username(m.group(1))
        if not chat_id:
            await msg.reply("❌ Failed to resolve username; pass explicit chat_id.")
            return

    if mid1 > mid2:
        mid1, mid2 = mid2, mid1
    span = mid2 - mid1 + 1

    await msg.reply(
        f"🧹 <b>/massdlt preview</b>\n"
        f"Chat: <code>{chat_id}</code>\n"
        f"Range: <code>{mid1}</code> → <code>{mid2}</code>\n"
        f"IDs to delete: <b>{span}</b>\n"
        f"Batch: 100 · Between batches: 2s · Long pause: 20s every 200 IDs\n"
        f"Starting now…",
        parse_mode="HTML")

    ok, txt = await ub.mass_delete_start(bot, msg.from_user.id,
                                         chat_id, mid1, mid2)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("massdlt_stop"))
async def cmd_massdlt_stop(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    ok, txt = ub.mass_delete_stop()
    await msg.reply(txt)


@router.message(Command("massdlt_status"))
async def cmd_massdlt_status(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    s = ub.mass_delete_state()
    if not s.running and not s.started_at:
        await msg.reply("💤 /massdlt is idle.")
        return
    import time as _t
    elapsed = _t.time() - (s.started_at or _t.time())
    total = (s.end_id - s.start_id + 1) if s.end_id else 0
    flag = "🟢 running" if s.running else "⏹ stopped"
    await msg.reply(
        f"{flag}\n"
        f"Chat: <code>{s.chat_id}</code>\n"
        f"Range: <code>{s.start_id}</code>..<code>{s.end_id}</code> ({total})\n"
        f"Deleted: <b>{s.deleted}</b>  errors=<b>{s.errors}</b>\n"
        f"Elapsed: {elapsed:.1f}s\n"
        f"Last error: <code>{esc(s.last_error or '-')}</code>",
        parse_mode="HTML",
    )
