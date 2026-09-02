"""v3.0: Turso → MongoDB migration commands.

/migrate_mongo          — full copy (resumable, idempotent), then auto-verify
/migrate_mongo delta    — top-up: only posts newer than the last migrated id
/migrate_mongo_status   — in-memory progress + last verification result

All three are SUPER-ADMIN only.
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import settings
from ..services import migrate as mig
from ..utils import esc
from .setup_cmds import _reject_non_super

log = logging.getLogger("migrate_cmds")
router = Router(name="migrate_cmds")


@router.message(Command("migrate_mongo"))
async def cmd_migrate(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    mode = "delta" if len(parts) > 1 and parts[1].lower() == "delta" else "full"
    ok, txt = mig.start_migration(msg.bot, msg.from_user.id, mode=mode)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("migrate_mongo_status"))
async def cmd_migrate_status(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    s = mig.mig_state()
    if not s.started_at:
        await msg.reply(
            f"💤 No migration run yet.\n"
            f"Backend: <code>{settings.db_backend}</code> | "
            f"Mongo configured: {'yes' if settings.mongodb_uri else 'no'}",
            parse_mode="HTML")
        return
    dt = time.time() - s.started_at
    flag = "🟢 running" if s.running else ("✅ done" if s.done else "⏹ stopped")
    verify = ("—" if s.verify_passed is None
              else ("✅ PASSED — identical" if s.verify_passed else "❌ FAILED"))
    await msg.reply(
        f"<b>Migration</b>: {flag} ({s.mode})\n"
        f"Table: <code>{s.table or '-'}</code> ({s.tables_done}/8)\n"
        f"Rows copied this run: <b>{s.rows_copied}</b>\n"
        f"Elapsed: {dt:.0f}s\n"
        f"Verify: {verify}\n"
        f"Last error: <code>{esc((s.last_error or '-')[:160])}</code>\n"
        f"Backend now: <code>{settings.db_backend}</code>",
        parse_mode="HTML")
