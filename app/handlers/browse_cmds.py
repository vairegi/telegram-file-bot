"""v4.6 — /browse command + brw: callbacks + pin admin commands.

User-facing:
    /browse            open the tag browser (rich message menu)

Admin-facing:
    /browse_pin #tag [section]     pin a tag to the top (all sections if omitted)
    /browse_unpin #tag [section]   unpin
    /browse_pins                   list current pins
    /numberoftags [N]              show/set tags per page in /browse (5-50,
                                   default 20; stored as browse_page_size)

Navigation happens INSIDE one rich message:
  * DM     -> sendRichMessage then editMessageText(rich_message=...)
  * Groups -> ephemeral sendRichMessage then editEphemeralMessageText(...)
              so only the requesting user sees the menu.
The menu deletes itself after 3 minutes without interaction, and a fresh
/browse deletes the user's previous menu (one menu per user).

Items are deep-link URL buttons to /start get_<code> — delivery keeps using
the existing ban → shortener → fsub gates untouched.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..services import browse, repo

log = logging.getLogger("browse_cmds")
router = Router(name="browse_cmds")

_bot_name: str = ""


async def _name(bot: Bot) -> str:
    global _bot_name
    if not _bot_name:
        try:
            _bot_name = (await bot.get_me()).username or ""
        except Exception:
            _bot_name = ""
    return _bot_name


async def _reject_non_admin(msg: Message) -> bool:
    from ..config import settings
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Admin only.")
    return True


def _norm_tag(text: str) -> str:
    return text.strip().lstrip("#").lower()


# ------------------------- /browse -------------------------
@router.message(Command("browse"))
async def cmd_browse(msg: Message, bot: Bot) -> None:
    try:
        by_section, _ = await browse.get_index()
        pins = await browse._pinned()
        rm = browse.render_sections(by_section, pins)
        if not any(b.get("type") == "buttons" for b in rm["blocks"][:-1]):
            await msg.reply("📖 No tags found yet — publish some covers first.")
            return
        await browse.send_browse(bot, msg, rm)
    except browse.BrowseError as e:
        # Surface the REAL Telegram error verbatim.
        await msg.reply(f"❌ /browse failed:\n<code>{e}</code>",
                        parse_mode="HTML")
    except Exception:
        log.exception("browse command error")
        await msg.reply("❌ /browse hit an unexpected error — check logs.")


# ------------------------- callbacks -------------------------
@router.callback_query(F.data.startswith("brw:"))
async def on_browse_nav(cb: CallbackQuery, bot: Bot) -> None:
    try:
        rm = await browse.window_for(cb.data, bot_name=await _name(bot))
        if rm is None:
            await cb.answer("This menu is stale — send /browse again.",
                            show_alert=True)
            return
        await browse.edit_browse(bot, cb, rm)
        await cb.answer()
    except browse.BrowseError as e:
        log.error("browse nav failed: %s", e)
        await cb.answer(str(e)[:190], show_alert=True)
    except Exception:
        log.exception("browse callback error")
        await cb.answer("Unexpected error — check bot logs.", show_alert=True)


# ------------------------- /numberoftags -------------------------
@router.message(Command("numberoftags"))
async def cmd_numberoftags(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = await browse.get_page_size()
        await msg.reply(
            f"🏷 /browse currently shows <b>{cur}</b> tags per page.\n"
            f"Usage: <code>/numberoftags N</code> "
            f"({browse.MIN_PAGE_SIZE}-{browse.MAX_PAGE_SIZE})",
            parse_mode="HTML")
        return
    try:
        n = int(parts[1])
    except ValueError:
        await msg.reply("❌ N must be a number, e.g. /numberoftags 30")
        return
    clamped = max(browse.MIN_PAGE_SIZE, min(browse.MAX_PAGE_SIZE, n))
    await repo.set_setting("browse_page_size", str(clamped))
    note = "" if clamped == n else f" (clamped to {clamped})"
    await msg.reply(f"✅ /browse will now show <b>{clamped}</b> tags per "
                    f"page{note}.", parse_mode="HTML")


# ------------------------- pin admin commands -------------------------
@router.message(Command("browse_pin"))
async def cmd_browse_pin(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/browse_pin #tag [section]</code>\n"
                        "Sections: parodies, characters, artists, groups, tags.",
                        parse_mode="HTML")
        return
    tag = _norm_tag(parts[1])
    by_section, _ = await browse.get_index()
    if len(parts) >= 3:
        sections = [parts[2].lower()]
    else:
        sections = [s for s in browse.BROWSE_SECTIONS
                    if tag in by_section.get(s, {})]
    if not sections or sections == [""]:
        await msg.reply(f"❌ #{tag} not found in any published caption.")
        return
    pins = await browse._pinned()
    added = []
    for s in sections:
        if tag not in by_section.get(s, {}):
            await msg.reply(f"⚠️ #{tag} has no covers in section "
                            f"<b>{s}</b> — skipped.", parse_mode="HTML")
            continue
        lst = pins.setdefault(s, [])
        if tag not in lst:
            lst.append(tag)
            added.append(s)
    await repo.set_setting_json("browse_pins", pins)
    if added:
        await msg.reply(f"⭐ Pinned #{tag} in: {', '.join(added)}")
    else:
        await msg.reply(f"#{tag} was already pinned.")


@router.message(Command("browse_unpin"))
async def cmd_browse_unpin(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/browse_unpin #tag [section]</code>",
                        parse_mode="HTML")
        return
    tag = _norm_tag(parts[1])
    pins = await browse._pinned()
    removed = []
    for s in ([parts[2].lower()] if len(parts) >= 3 else list(pins.keys())):
        if tag in pins.get(s, []):
            pins[s] = [t for t in pins[s] if t != tag]
            removed.append(s)
    await repo.set_setting_json("browse_pins", pins)
    await msg.reply(f"🗑 Unpinned #{tag} from: {', '.join(removed)}"
                    if removed else f"#{tag} was not pinned.")


@router.message(Command("browse_pins"))
async def cmd_browse_pins(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    pins = await browse._pinned()
    if not pins:
        await msg.reply("No pinned tags. Use /browse_pin #tag [section].")
        return
    lines = ["⭐ <b>Pinned tags</b>"]
    for s, tags in pins.items():
        if tags:
            lines.append(f"• {s}: " + " ".join(f"#{t}" for t in tags))
    await msg.reply("\n".join(lines), parse_mode="HTML")
