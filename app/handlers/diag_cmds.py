"""Diagnostics: /debug /stats. /debug reads mostly from in-memory cache."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import posting, repo, scheduler as sched, userbot as ub
from ..utils import esc
from .setup_cmds import _reject_non_admin

log = logging.getLogger("diag_cmds")
router = Router(name="diag_cmds")


@router.message(Command("debug"))
async def cmd_debug(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    s = ub.backfill_state()
    ms = ub.mass_delete_state()
    cfg = sched.get_schedule()
    last_err = posting.LAST_PUBLISH_ERROR or "-"
    lines = [
        "<b>🔧 Debug</b>",
        f"spoiler: <b>{'ON' if repo.get_setting_bool('spoiler', True) else 'OFF'}</b>",
        f"protect: <b>{'ON' if repo.get_setting_bool('protect_content') else 'OFF'}</b>",
        f"paused: <b>{'YES' if posting._paused() else 'no'}</b>",
        f"schedule: <code>{cfg or 'off'}</code>",
        f"postcaption: <code>{esc(repo.get_setting('postcaption_extra') or '-')}</code>",
        f"filecaption: <code>{esc(repo.get_setting('filecaption_extra') or '-')}</code>",
        f"backfill running: {s.running} | current mid: {s.current_mid} / head {s.head_mid}",
        f"backfill counts: 🖼{s.covers_ingested} 📄{s.files_ingested} 🎨{s.stickers_ingested}",
        f"massdlt running: {ms.running} | deleted: {ms.deleted}",
        f"last publish error: <code>{esc(last_err[:160])}</code>",
        f"cache entries: {repo.cache_stats()['entries']}",
        f"telethon available: {ub.telethon_available()}",
    ]
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    covers = repo.total_cover_count()
    files = repo.total_file_count()
    published = repo.published_cover_count()
    pending = repo.queued_cover_count()
    await msg.reply(
        f"📊 <b>Stats</b>\n"
        f"🖼 Covers: {covers}\n"
        f"📄 Files: {files}\n"
        f"✅ Published: {published}\n"
        f"⏳ Pending: {pending}",
        parse_mode="HTML",
    )
