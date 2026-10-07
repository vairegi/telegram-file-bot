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
        cur = "ON" if (await repo.get_setting_bool("spoiler", True)) else "OFF"
        await msg.reply(f"Spoiler is currently <b>{cur}</b>.\n"
                        f"Usage: <code>/spoiler 1</code> or <code>/spoiler 0</code>",
                        parse_mode="HTML")
        return
    on = parts[1] in ("1", "on", "true", "yes")
    await repo.set_setting("spoiler", "1" if on else "0")
    await msg.reply(f"✅ Spoiler <b>{'ON' if on else 'OFF'}</b>.",
                    parse_mode="HTML")


@router.message(Command("protect"))
async def cmd_protect(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = "ON" if (await repo.get_setting_bool("protect_content")) else "OFF"
        await msg.reply(f"Protect-content is <b>{cur}</b>.\n"
                        f"Usage: <code>/protect 1</code> or <code>/protect 0</code>",
                        parse_mode="HTML")
        return
    on = parts[1] in ("1", "on", "true", "yes")
    await repo.set_setting("protect_content", "1" if on else "0")
    await msg.reply(f"✅ Protect-content <b>{'ON' if on else 'OFF'}</b>.",
                    parse_mode="HTML")


@router.message(Command("postcaption"))
async def cmd_postcaption(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = (await repo.get_setting("postcaption_extra")) or "(none)"
        await msg.reply(f"Current: <code>{esc(cur)}</code>\n"
                        f"Usage: <code>/postcaption &lt;text&gt;</code> "
                        f"(use 'off' to clear)",
                        parse_mode="HTML")
        return
    txt = parts[1].strip()
    await repo.set_setting("postcaption_extra", None if txt.lower() == "off" else txt)
    await msg.reply("✅ Post caption extra updated.")


@router.message(Command("filecaption"))
async def cmd_filecaption(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = (await repo.get_setting("filecaption_extra")) or "(none)"
        await msg.reply(f"Current: <code>{esc(cur)}</code>\n"
                        f"Usage: <code>/filecaption &lt;text&gt;</code> "
                        f"(use 'off' to clear)",
                        parse_mode="HTML")
        return
    txt = parts[1].strip()
    await repo.set_setting("filecaption_extra", None if txt.lower() == "off" else txt)
    await msg.reply("✅ File caption extra updated.")


# ---------------------------------------------------------------------------
# v5.2: extra post buttons (/addbuttontopost & friends)
# ---------------------------------------------------------------------------
from ..services import posting as _posting


def _args2(msg: Message) -> str:
    return (msg.text or "").partition(" ")[2].strip()


@router.message(Command("addbuttontopost"))
async def cmd_addbuttontopost(msg: Message) -> None:
    """/addbuttontopost <label> - <url> [- red|green|blue]"""
    if await _reject_non_admin(msg):
        return
    raw = _args2(msg)
    parts = [p.strip() for p in raw.split(" - ")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await msg.reply("Usage: <code>/addbuttontopost BACKUP - https://t.me/… - red</code>\n"
                        "(color optional: red · green · blue; default = app style)",
                        parse_mode="HTML")
        return
    label, url = parts[0][:60], parts[1]
    color = parts[2] if len(parts) >= 3 else ""
    style = _posting._style_of(color)
    if color and not style:
        await msg.reply("❌ Unknown color. Use: <code>red</code>, <code>green</code> "
                        "or <code>blue</code>.", parse_mode="HTML")
        return
    if not url.startswith("http"):
        await msg.reply("❌ URL must start with http(s)://")
        return
    extras = await _posting._extra_post_buttons()
    extras.append({"label": label, "url": url, "style": style})
    await repo.set_setting_json(_posting.EXTRA_BUTTONS_KEY, extras)
    where = ("beside DOWNLOAD (half width)" if len(extras) == 1
             else f"full-width row #{len(extras)}")
    await msg.reply(f"✅ Button <b>{esc(label)}</b> added — {where}."
                    + (f" Color: {color}." if color else ""), parse_mode="HTML")


@router.message(Command("removebuttonfrompost"))
async def cmd_removebuttonfrompost(msg: Message) -> None:
    """/removebuttonfrompost <index>  (1-based, see /listpostbuttons)"""
    if await _reject_non_admin(msg):
        return
    raw = _args2(msg)
    try:
        i = int(raw) - 1
    except (ValueError, IndexError):
        await msg.reply("Usage: <code>/removebuttonfrompost &lt;index&gt;</code>",
                        parse_mode="HTML")
        return
    extras = await _posting._extra_post_buttons()
    if not (0 <= i < len(extras)):
        await msg.reply(f"❌ No button #{i + 1}. See /listpostbuttons.")
        return
    removed = extras.pop(i)
    await repo.set_setting_json(_posting.EXTRA_BUTTONS_KEY, extras)
    await msg.reply(f"🗑 Removed <b>{esc(removed['label'])}</b>.",
                    parse_mode="HTML")


@router.message(Command("listpostbuttons"))
async def cmd_listpostbuttons(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    extras = await _posting._extra_post_buttons()
    if not extras:
        await msg.reply("💤 No extra post buttons. Add one with /addbuttontopost.")
        return
    lines = ["<b>🔘 Extra post buttons</b>"]
    for i, e in enumerate(extras):
        lines.append(f"<b>{i + 1}.</b> {esc(e['label'])} → <code>{esc(e['url'])}</code>"
                     + (f" · {e['style']}" if e.get("style") else ""))
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("clearpostbuttons"))
async def cmd_clearpostbuttons(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await repo.set_setting_json(_posting.EXTRA_BUTTONS_KEY, [])
    await msg.reply("🗑 All extra post buttons removed (DOWNLOAD stays).")
