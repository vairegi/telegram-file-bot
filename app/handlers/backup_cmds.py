"""v2.9: backup channel mirroring — the 11 backup commands."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import backup as bk
from ..services import repo
from ..utils import esc, parse_channel_id
from .setup_cmds import _reject_non_admin

log = logging.getLogger("backup_cmds")
router = Router(name="backup_cmds")


# ------------------------- /addbackup /removebackup /listbackup -------------
@router.message(Command("addbackup"))
async def cmd_addbackup(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/addbackup &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    title = None
    try:
        chat = await bot.get_chat(cid)
        title = getattr(chat, "title", None)
    except Exception as e:
        await msg.reply(f"⚠️ Could not read channel info: <code>{esc(str(e))}</code>\n"
                        f"Make sure the bot is ADMIN in <code>{cid}</code>, then retry.",
                        parse_mode="HTML")
        return
    repo.add_channel(cid, "backup", title=title)
    await msg.reply(
        f"✅ Added backup channel <code>{cid}</code>"
        + (f" ({esc(title)})" if title else "")
        + f"\nStart it with <code>/backup {cid}</code> or wait for the auto-loop.",
        parse_mode="HTML")


@router.message(Command("removebackup"))
async def cmd_removebackup(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/removebackup &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    row = repo.get_channel(cid)
    if not row or row.get("role") != "backup":
        await msg.reply("❌ That is not a registered backup channel.")
        return
    repo.remove_channel(cid)
    await msg.reply(f"🗑 Removed backup channel <code>{cid}</code>. "
                    f"Progress rows kept (use /dltbackup to wipe them too).",
                    parse_mode="HTML")


@router.message(Command("listbackup"))
async def cmd_listbackup(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    rows = repo.get_backup_channels()
    if not rows:
        await msg.reply("💤 No backup channels registered.")
        return
    total = len(repo.all_db_source_messages())
    lines = ["💾 <b>Backup channels</b>"]
    for r in rows:
        cid = int(r["chat_id"])
        title = (r.get("title") or "").strip()
        if not title:
            try:
                chat = await bot.get_chat(cid)
                title = getattr(chat, "title", "") or ""
                if title:
                    repo.update_channel_title(cid, title)
            except Exception:
                title = ""
        if not title:
            title = str(cid)
        # Real invite link (cached) if the bot can mint one; else view fallback.
        ck = f"invite:{cid}"
        link = repo.get_setting(ck)
        if not link:
            try:
                link = await bot.export_chat_invite_link(cid)
                repo.set_setting(ck, link)
            except Exception:
                try:
                    chat2 = await bot.get_chat(cid)
                    uname = getattr(chat2, "username", None)
                    if uname:
                        link = f"https://t.me/{uname}"
                        repo.set_setting(ck, link)
                except Exception:
                    pass
        if not link:
            bare = str(cid).replace("-100", "", 1) if str(cid).startswith("-100") else str(cid)
            link = f"https://t.me/c/{bare}"
        done = repo.backup_mirrored_count(cid)
        remaining = max(0, total - done)
        lines.append(
            f'• <a href="{link}">{esc(title)}</a> <code>{cid}</code> — '
            f'<b>{done}</b> mirrored, <b>{remaining}</b> remaining')
    await msg.reply("\n".join(lines), parse_mode="HTML",
                    disable_web_page_preview=True)


# ------------------------- /backup /backup10 --------------------------------
@router.message(Command("backup"))
async def cmd_backup(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backup &lt;backup_chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    row = repo.get_channel(cid)
    if not row or row.get("role") != "backup":
        await msg.reply("❌ That chat id is not a registered backup channel. "
                        "Use /addbackup first.")
        return
    if repo.backup_is_paused():
        await msg.reply("⏸ Backup is paused. /resumebackup first.")
        return
    if bk.is_running(cid):
        await msg.reply("⚠️ A backup pass is already running for that channel.")
        return

    await msg.reply(f"🚀 Backup run started for <code>{cid}</code>… "
                    f"progress updates every 50 messages.",
                    parse_mode="HTML")

    async def _run():
        res = await bk.run_backup(bot, cid, limit=0, admin_chat_id=msg.from_user.id)
        try:
            if not res.get("ok"):
                await bot.send_message(msg.from_user.id,
                                       f"❌ Backup aborted: {esc(str(res.get('error') or '-'))}",
                                       parse_mode="HTML")
                return
            await bot.send_message(
                msg.from_user.id,
                f"✅ Backup pass finished for <code>{cid}</code>\n"
                f"Mirrored: <b>{res.get('mirrored', 0)}</b>  "
                f"Errors: <b>{res.get('errors', 0)}</b>\n"
                f"Last error: <code>{esc((res.get('last_error') or '-')[:120])}</code>",
                parse_mode="HTML")
        except Exception:
            pass

    asyncio.create_task(_run())


@router.message(Command("backup10"))
async def cmd_backup10(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backup10 &lt;backup_chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    row = repo.get_channel(cid)
    if not row or row.get("role") != "backup":
        await msg.reply("❌ That chat id is not a registered backup channel.")
        return
    if repo.backup_is_paused():
        await msg.reply("⏸ Backup is paused. /resumebackup first.")
        return
    if bk.is_running(cid):
        await msg.reply("⚠️ Backup pass already running for that channel.")
        return
    res = await bk.run_backup(bot, cid, limit=10)
    if not res.get("ok"):
        await msg.reply(f"❌ {esc(str(res.get('error') or '-'))}", parse_mode="HTML")
        return
    await msg.reply(
        f"✅ /backup10 done for <code>{cid}</code>\n"
        f"Mirrored: <b>{res.get('mirrored', 0)}</b>  "
        f"Errors: <b>{res.get('errors', 0)}</b>",
        parse_mode="HTML")


# ------------------------- /resetbackup /undoresetbackup /dltbackup --------
@router.message(Command("resetbackup"))
async def cmd_resetbackup(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    cids: list[int] = []
    if len(parts) < 2:
        cids = [int(c["chat_id"]) for c in repo.get_backup_channels()]
    else:
        cid = parse_channel_id(parts[1])
        if not cid:
            await msg.reply("❌ Bad chat id.")
            return
        cids = [cid]
    if not cids:
        await msg.reply("💤 No backup channels to reset.")
        return
    total = 0
    for cid in cids:
        total += repo.backup_reset(cid)
    await msg.reply(
        f"🧹 Reset <b>{total}</b> progress row(s) across {len(cids)} channel(s). "
        f"Next /backup re-mirrors from message #1. "
        f"Undo with <code>/undoresetbackup &lt;chat_id&gt;</code>.",
        parse_mode="HTML")


@router.message(Command("undoresetbackup"))
async def cmd_undoresetbackup(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/undoresetbackup &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    n = repo.backup_undo_reset(cid)
    if n == 0:
        await msg.reply("💤 No reset history found for that channel.")
    else:
        await msg.reply(f"♻️ Restored <b>{n}</b> progress row(s) for "
                        f"<code>{cid}</code>.", parse_mode="HTML")


@router.message(Command("dltbackup"))
async def cmd_dltbackup(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/dltbackup &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    n = repo.backup_delete_all_progress(cid)
    await msg.reply(
        f"🗑 Deleted <b>{n}</b> progress row(s) for <code>{cid}</code>. "
        f"Next /backup re-mirrors from message #1 (no undo).",
        parse_mode="HTML")


# ------------------------- /pausebackup /resumebackup /backupstatus --------
@router.message(Command("pausebackup"))
async def cmd_pausebackup(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_backup_paused(True)
    bk.stop_all()
    await msg.reply(
        "⏸ <b>Auto-backup paused.</b>\n\n"
        "New posts will NOT be mirrored to backup channels from Database channel.\n"
        "Use /resumebackup to catch up and resume.",
        parse_mode="HTML")


@router.message(Command("resumebackup"))
async def cmd_resumebackup(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_backup_paused(False)
    await msg.reply(
        "▶️ <b>Auto-backup resumed.</b>\n"
        "Catching up each backup channel now (progress rows pick up where they left off).",
        parse_mode="HTML")
    for ch in repo.get_backup_channels():
        cid = int(ch["chat_id"])
        if not bk.is_running(cid):
            asyncio.create_task(bk.run_backup(bot, cid, limit=0,
                                              admin_chat_id=msg.from_user.id))


@router.message(Command("backupstatus"))
async def cmd_backupstatus(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    paused = repo.backup_is_paused()
    total = len(repo.all_db_source_messages())
    header = ("⏸ Auto-backup is PAUSED." if paused
              else "▶️ Auto-backup is RUNNING.")
    rows = repo.get_backup_channels()
    if not rows:
        await msg.reply(f"{header}\n\n💤 No backup channels registered.",
                        parse_mode="HTML")
        return
    lines = [header, "", "<b>Backup progress</b>"]
    for r in rows:
        cid = int(r["chat_id"])
        title = (r.get("title") or "").strip() or str(cid)
        done = repo.backup_mirrored_count(cid)
        remaining = max(0, total - done)
        pct = (100.0 * done / total) if total else 0.0
        running = "  🟢 running" if bk.is_running(cid) else ""
        lines.append(
            f"• {esc(title)} (<code>{cid}</code>): "
            f"<b>{done}</b>/<b>{total}</b> ({pct:.0f}%) — "
            f"<b>{remaining}</b> pending{running}")
    await msg.reply("\n".join(lines), parse_mode="HTML")
