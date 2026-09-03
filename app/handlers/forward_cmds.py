"""MTProto userbot bulk forward: /forward /forward_status /forward_stop /forward_resume."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import userbot as ub
from ..utils import esc, parse_channel_id, parse_tme_link
from .setup_cmds import _reject_non_admin

log = logging.getLogger("forward_cmds")
router = Router(name="forward_cmds")


@router.message(Command("forward"))
async def cmd_forward(msg: Message, bot: Bot) -> None:
    """Usage: /forward <dest_chat_id[,dest2,…] | dest1 dest2 …> <start_link> <end_link>

    The two links point into the SOURCE channel; every message id in that
    range is forwarded (a real forward — the "Forwarded from" tag is kept)
    into the destination channel(s) by the MTProto userbot, with automatic
    rate-limit rests and FloodWait handling.
    """
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()

    # Destinations: every leading token that is NOT a t.me link.
    # Both forms work (v3.2.1):
    #   /forward -100111,-100222 <start> <end>
    #   /forward -100111 -100222 <start> <end>
    dest_refs: list[int] = []
    i = 1
    while i < len(parts) and "t.me/" not in parts[i]:
        for tok in parts[i].split(","):
            tok = tok.strip()
            if not tok:
                continue
            cid = parse_channel_id(tok)
            if cid is None:
                await msg.reply(f"❌ Bad destination id: <code>{esc(tok)}</code>",
                                parse_mode="HTML")
                return
            dest_refs.append(int(cid))
        i += 1
    if not dest_refs or len(parts) - i < 2:
        await msg.reply(
            "Usage: <code>/forward &lt;dest_id[,dest2,…] | dest1 dest2 …&gt; "
            "&lt;start_link&gt; &lt;end_link&gt;</code>\n"
            "Example: <code>/forward -1001234567890 "
            "https://t.me/c/2298797194/50 https://t.me/c/2298797194/900</code>\n"
            "Multiple destinations: <code>/forward -100111 -100222 "
            "&lt;start&gt; &lt;end&gt;</code> (spaces or commas both work)",
            parse_mode="HTML")
        return

    p1 = parse_tme_link(parts[i])
    p2 = parse_tme_link(parts[i + 1])
    if not p1 or not p2:
        await msg.reply("❌ Could not parse one or both t.me links.")
        return
    cid1, uname1, mid1 = p1
    cid2, uname2, mid2 = p2
    if cid1 and cid2 and cid1 != cid2:
        await msg.reply("❌ The two links belong to different channels.")
        return

    source_ref = cid1 or cid2
    if not source_ref:
        uname = uname1 or uname2
        source_ref = await ub.resolve_channel_ref(uname)
        if not source_ref:
            await msg.reply(
                "❌ Could not resolve the source channel. Use "
                "https://t.me/c/… links, or make sure the userbot account "
                "is a member of the source channel.")
            return

    if mid1 > mid2:
        mid1, mid2 = mid2, mid1
    ok, txt = await ub.forward_start(bot, msg.from_user.id, source_ref,
                                     dest_refs, mid1, mid2)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("forward_status"))
async def cmd_forward_status(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    s = ub.forward_state()
    if not s.running and not s.started_at:
        await msg.reply("💤 /forward is idle.")
        return
    import time as _t
    elapsed = _t.time() - (s.started_at or _t.time())
    total = (s.end_id - s.start_id + 1) if s.end_id else 0
    flag = "🟢 running" if s.running else f"⏹ {s.end_reason or 'stopped'}"
    await msg.reply(
        f"{flag}\n"
        f"Range: <code>{s.start_id}</code>..<code>{s.end_id}</code> ({total} ids)\n"
        f"Next id: <code>{s.current_id}</code>\n"
        f"Destinations: <b>{len(s.dest_refs)}</b>\n"
        f"Forwarded: <b>{s.forwarded}</b>   Errors: <b>{s.errors}</b>\n"
        f"Elapsed: {elapsed:.1f}s\n"
        f"Last error: <code>{esc(s.last_error or '-')}</code>",
        parse_mode="HTML")


@router.message(Command("forward_stop"))
async def cmd_forward_stop(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    ok, txt = ub.forward_stop()
    await msg.reply(txt)


@router.message(Command("forward_resume"))
async def cmd_forward_resume(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    ok, txt = await ub.forward_resume(bot, msg.from_user.id)
    await msg.reply(txt, parse_mode="HTML")
