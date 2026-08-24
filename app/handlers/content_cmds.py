"""Content-control commands: /spoiler /protect /postcaption /filecaption."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import repo
from ..utils import esc
from .setup_cmds import _reject_non_admin

log = logging.getLogger("content_cmds")
router = Router(name="content_cmds")


@router.message(Command("spoiler"))
async def cmd_spoiler(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = "ON" if repo.get_setting_bool("spoiler", True) else "OFF"
        await msg.reply(f"Spoiler is currently <b>{cur}</b>.\n"
                        f"Usage: <code>/spoiler 1</code> or <code>/spoiler 0</code>",
                        parse_mode="HTML")
        return
    on = parts[1] in ("1", "on", "true", "yes")
    repo.set_setting("spoiler", "1" if on else "0")
    await msg.reply(f"✅ Spoiler <b>{'ON' if on else 'OFF'}</b>.",
                    parse_mode="HTML")


@router.message(Command("protect"))
async def cmd_protect(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = "ON" if repo.get_setting_bool("protect_content") else "OFF"
        await msg.reply(f"Protect-content is <b>{cur}</b>.\n"
                        f"Usage: <code>/protect 1</code> or <code>/protect 0</code>",
                        parse_mode="HTML")
        return
    on = parts[1] in ("1", "on", "true", "yes")
    repo.set_setting("protect_content", "1" if on else "0")
    await msg.reply(f"✅ Protect-content <b>{'ON' if on else 'OFF'}</b>.",
                    parse_mode="HTML")


@router.message(Command("postcaption"))
async def cmd_postcaption(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = repo.get_setting("postcaption_extra") or "(none)"
        await msg.reply(f"Current: <code>{esc(cur)}</code>\n"
                        f"Usage: <code>/postcaption &lt;text&gt;</code> "
                        f"(use 'off' to clear)",
                        parse_mode="HTML")
        return
    txt = parts[1].strip()
    repo.set_setting("postcaption_extra", None if txt.lower() == "off" else txt)
    await msg.reply("✅ Post caption extra updated.")


@router.message(Command("filecaption"))
async def cmd_filecaption(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = repo.get_setting("filecaption_extra") or "(none)"
        await msg.reply(f"Current: <code>{esc(cur)}</code>\n"
                        f"Usage: <code>/filecaption &lt;text&gt;</code> "
                        f"(use 'off' to clear)",
                        parse_mode="HTML")
        return
    txt = parts[1].strip()
    repo.set_setting("filecaption_extra", None if txt.lower() == "off" else txt)
    await msg.reply("✅ File caption extra updated.")
