"""v4.9 — token-system commands.

This router is included BEFORE setup_cmds / shortener_cmds, so the handlers
here SHADOW the old ones (same pattern that makes /banlist work):
    /mystats          -> rich table incl. token balance (user-facing)
    /tokenpersolve N  -> tokens granted per shortener solve (admin)
    /setverifytime    -> DEPRECATED notice (old 6-hour unlock is gone)
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import settings
from ..services import browse, repo, richlists, tokens

log = logging.getLogger("token_cmds")
router = Router(name="token_cmds")


async def _reject_non_admin(msg: Message) -> bool:
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Admin only.")
    return True


# ---------------------------------------------------------------------------
# /mystats  (user-facing, now a rich table with the token wallet)
# ---------------------------------------------------------------------------
@router.message(Command("mystats"))
async def cmd_mystats(msg: Message, bot: Bot) -> None:
    uid = int(msg.from_user.id)
    try:
        favs = await repo.list_favorites(uid)
    except Exception:
        favs = []
    try:
        rank, week_cnt = await repo.fetch_rank_week(uid)
    except Exception:
        rank, week_cnt = 0, 0
    try:
        sim_on = await repo.get_similar_pref(uid)
    except Exception:
        sim_on = True

    w = await tokens.get_wallet(uid)
    now = __import__("time").time()
    live = w["tokens"] > 0 and w["expiry"] > now
    tok_line = (f"{w['tokens']} — expire {tokens.expiry_label(w['expiry'])}"
                if live else "0 — solve the shortener to get more")
    rank_txt = f"#{rank}" if rank else "—"

    rows = [
        ("❤️ Saved favorites", str(len(favs))),
        ("📥 Files this week", str(week_cnt)),
        ("🏆 Weekly rank", rank_txt),
        ("📚 Similar recommendations", "ON ✅" if sim_on else "OFF ❌"),
        ("🔑 Tokens", tok_line),
        ("🔄 Tokens per solve", str(await tokens.per_solve())),
    ]
    headers = ["📊 Your stats", "Value"]
    cells = [[richlists._cell(h, header=True) for h in headers]] + [
        [richlists._cell(k), richlists._cell(v)] for k, v in rows]
    rm = browse.rich_message([
        browse.block_heading("📊 Your Stats", size=2),
        {"type": "table", "cells": cells, "is_striped": True},
        browse.block_paragraph("Weekly counters reset Monday 1:00 AM IST · "
                              "tokens reset daily at 4:00 AM IST."),
    ])
    plain = ("📊 <b>Your Stats</b>\n\n"
             + "\n".join(f"{k}: <b>{v}</b>" for k, v in rows))
    try:
        await richlists.send_rich_or_plain(bot, msg.chat.id, rm, plain,
                                           reply_to=getattr(msg, "message_id", 0))
    except Exception:
        log.exception("mystats table failed")
        await msg.reply(plain, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /tokenpersolve  (admin)
# ---------------------------------------------------------------------------
@router.message(Command("tokenpersolve"))
async def cmd_tokenpersolve(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    cur = await tokens.per_solve()
    if len(parts) < 2:
        await msg.reply(
            f"🔑 /tokenpersolve is currently <b>{cur}</b> tokens per solve.\n"
            f"Usage: <code>/tokenpersolve N</code> (1-1000)",
            parse_mode="HTML")
        return
    try:
        n = int(parts[1])
    except ValueError:
        await msg.reply("❌ N must be a number, e.g. /tokenpersolve 11")
        return
    if not 1 <= n <= 1000:
        await msg.reply("❌ N must be between 1 and 1000.")
        return
    await repo.set_setting("tokens_per_solve", str(n))
    await msg.reply(
        f"✅ Users now get <b>{n}</b> token(s) per shortener solve "
        f"(1 token = 1 post). Tokens expire daily at <b>4:00 AM IST</b>.",
        parse_mode="HTML")


# ---------------------------------------------------------------------------
# /setverifytime  (deprecated — shadows the old handler)
# ---------------------------------------------------------------------------
@router.message(Command("setverifytime"))
async def cmd_setverifytime_deprecated(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await msg.reply(
        "⚠️ <b>/setverifytime is deprecated.</b>\n\n"
        "The old “unlocked for N hours” model is gone — access is now a daily "
        "token wallet:\n"
        "• 1 solve = <b>{n}</b> tokens (change with <code>/tokenpersolve N</code>)\n"
        "• 1 post = 1 token\n"
        "• unused tokens expire at <b>4:00 AM IST</b>".format(
            n=await tokens.per_solve()),
        parse_mode="HTML")
