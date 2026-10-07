"""v4.3: admin commands for the VPLINK shortener verification gate.

  /shortener on|off|status     toggle + inspect the gate
  /shortenerapi <url>          VPLINK api base (…?api=TOKEN&url=)
  /setverifytime <hours>       how long one verify unlocks (default 6, 1-168)
  /shortenermsg <text>         landing-page/overlay heading (clear = default)
  /shortenerbotmsg <text>      bot-DM gate text (clear = default)
  /verifymsg <text>            post-verification success text (clear = default)
  /shortenerbtn <label> | <url>   add a secondary button
  /clearshortenerbtns          remove ALL secondary buttons
"""
from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import linkguard as lg
from ..services import repo, shortener as sh
from ..utils import esc
from .setup_cmds import _reject_non_admin

log = logging.getLogger("shortener_cmds")
router = Router(name="shortener_cmds")


def _args(msg: Message) -> str:
    return (msg.text or "").partition(" ")[2].strip()


async def _set_or_clear(msg: Message, key: str, label: str) -> None:
    """Shared handler for the three text-setting commands ('clear' = default)."""
    val = _args(msg)
    if not val:
        cur = (await repo.get_setting(key)) or ""
        await msg.reply(f"Current {label}:\n" + (f"<code>{esc(cur)}</code>" if cur
                        else "<i>(default)</i>"), parse_mode="HTML")
        return
    await repo.set_setting(key, None if val.lower() == "clear" else val)
    await msg.reply(f"✅ {label} {'reset to default' if val.lower() == 'clear' else 'updated'}.")


@router.message(Command("shortener"))
async def cmd_shortener(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    arg = _args(msg).lower()
    if arg == "on":
        await repo.set_setting("shortener_enabled", "1")
        await msg.reply("✅ Shortener gate is now <b>ON</b>.", parse_mode="HTML")
        return
    if arg == "off":
        await repo.set_setting("shortener_enabled", None)
        await msg.reply("❌ Shortener gate is now <b>OFF</b> — files deliver instantly.",
                        parse_mode="HTML")
        return
    # default / "status"
    enabled = await sh.is_enabled()
    api = await sh.get_api_base()
    ttl = await sh.get_ttl_hours()
    btns = await sh.get_secondary_buttons()
    await msg.reply(
        "<b>🔗 Shortener Gate</b>\n"
        f"status: <b>{'ON ✅' if enabled else 'OFF ❌'}</b>\n"
        f"api: <b>{'✅ set' if api else '❌ not set (gate fails open)'}</b>\n"
        f"verify unlock: <b>{ttl:g}h</b>\n"
        f"secondary buttons: <b>{len(btns)}</b>\n"
        f"providers (rotation): <b>{len(await sh.get_providers())}</b> — /shorteners\n"
        f"live tokens: <b>{await sh.token_count()}</b> · "
        f"unlocked users (DB): <b>{await sh.unlocked_count()}</b>\n\n"
        "<i>/shortener on · /shortener off · /shortenerapi · /setverifytime · "
        "/shortenermsg · /shortenerbotmsg · /verifymsg · /shortenerbtn · "
        "/clearshortenerbtns</i>",
        parse_mode="HTML")


@router.message(Command("shortenerapi"))
async def cmd_shortenerapi(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    val = _args(msg)
    if not val:
        cur = await sh.get_api_base()
        await msg.reply(
            ("Current API base: "
            + ("✅ set (hidden — it contains your token)" if cur else "<i>(not set)</i>"))
            + "\n\nSet it with:\n<code>/shortenerapi https://vplink.in/api?api=YOUR_TOKEN&url=</code>\n"
              "(end with the empty <code>url=</code> param — the destination is appended automatically)",
            parse_mode="HTML")
        return
    if "url=" not in val or not val.startswith("http"):
        await msg.reply("❌ Expected a full template like "
                        "<code>https://vplink.in/api?api=TOKEN&url=</code>", parse_mode="HTML")
        return
    await repo.set_setting("shortener_api", val)

    # v5.0: re-push the shortener host into LinkGuard's finish2 referer
    # allowlist whenever the API base changes. No-op when LinkGuard is
    # off/unreachable (fail-open).
    try:
        if await lg.is_configured():
            from ..handlers.linkguard_cmds import push_ref_hosts
            await push_ref_hosts()
    except Exception:
        pass
    await msg.reply("✅ Shortener API saved. Test it with /shortener status.")


@router.message(Command("setverifytime"))
async def cmd_setverifytime(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    raw = _args(msg)
    try:
        hours = float(raw)
        if not (1 <= hours <= 168):
            raise ValueError
    except ValueError:
        await msg.reply("Usage: <code>/setverifytime 6</code> — hours, between 1 and 168.",
                        parse_mode="HTML")
        return
    await repo.set_setting("verify_ttl_hours", str(hours))
    await msg.reply(f"✅ One verification now unlocks for <b>{hours:g}h</b>.",
                    parse_mode="HTML")


@router.message(Command("shortenermsg"))
async def cmd_shortenermsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "shortener_msg", "overlay/landing text")


@router.message(Command("shortenerbotmsg"))
async def cmd_shortenerbotmsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "shortener_bot_msg", "bot-DM gate text")


@router.message(Command("verifymsg"))
async def cmd_verifymsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "verify_msg", "post-verify success text")


@router.message(Command("shortenerbtn"))
async def cmd_shortenerbtn(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    args = _args(msg)
    if args.lower() == "clear" or "|" not in args:
        await msg.reply("Usage: <code>/shortenerbtn Button Label | https://…</code>\n"
                        "(<code>/clearshortenerbtns</code> removes all)",
                        parse_mode="HTML")
        return
    label, url = (p.strip() for p in args.split("|", 1))
    if not label or not url.startswith(("http://", "https://", "tg://")):
        await msg.reply("❌ Need a label and a link: <code>Label | https://…</code>",
                        parse_mode="HTML")
        return
    rows = await sh.get_secondary_buttons()
    rows.append([label[:60], url])
    await repo.set_setting_json("shortener_buttons", rows)
    await msg.reply(f"✅ Secondary button added ({len(rows)} total).")


@router.message(Command("clearshortenerbtns"))
async def cmd_clearshortenerbtns(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await repo.set_setting_json("shortener_buttons", [])
    await msg.reply("✅ All secondary shortener buttons removed.")


# ---------------- v4.4: ban management ----------------
@router.message(Command("ban"))
async def cmd_ban(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    raw = _args(msg)
    try:
        uid = int(raw.split()[0])
    except (ValueError, IndexError):
        await msg.reply("Usage: <code>/ban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    reason = raw.partition(" ")[2].strip()
    await repo.ban_user(uid, reason)
    await msg.reply(f"🚫 User <code>{uid}</code> banned."
                    + (f" Reason: {esc(reason)}" if reason else ""), parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    raw = _args(msg)
    try:
        uid = int(raw.split()[0])
    except (ValueError, IndexError):
        await msg.reply("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    await repo.unban_user(uid)
    await repo.strikes_reset(uid)
    await msg.reply(f"✅ User <code>{uid}</code> unbanned — strikes reset to 0.",
                    parse_mode="HTML")


# v4.8: /banlist is now handled by handlers/richlist_cmds.py (rich table).
# This stub stays defined so old aliases keep matching, but the new router
# is registered FIRST in main.py, so this handler never actually runs.
async def _cmd_banlist_v47_stub(msg: Message) -> None:  # noqa: E501 (kept for compat)
    return


# ---------------------------------------------------------------------------
# v5.1: multi-shortener rotation admin commands
# ---------------------------------------------------------------------------
@router.message(Command("shorteners"))
async def cmd_shorteners(msg: Message) -> None:
    """List the rotation: index, name, api status, declared redirect hosts."""
    if await _reject_non_admin(msg):
        return
    providers = await sh.get_providers()
    lines = ["<b>🔗 Shortener rotation</b> (users cycle through these, one per solve)"]
    for i, p in enumerate(providers):
        hosts = ", ".join(p.get("hosts") or []) or "—"
        lines.append(f"<b>{i}.</b> {esc(p['name'])} — api: "
                     f"{'✅ set' if p['api'] else '❌ NOT SET'} · "
                     f"hosts: <code>{esc(hosts)}</code>")
    lines.append("\n<i>/addshortener &lt;name&gt; | &lt;api…&amp;url=&gt; "
                 "[| host1,host2] · /delshortener &lt;index|name&gt;</i>")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("addshortener"))
async def cmd_addshortener(msg: Message) -> None:
    """Add a provider to the rotation (vplink stays #0)."""
    if await _reject_non_admin(msg):
        return
    raw = _args(msg)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await msg.reply("Usage: <code>/addshortener arolinks | "
                        "https://arolinks.com/api?api=KEY&amp;url= | links.arolinks.com</code>\n"
                        "(3rd part = its redirect hosts for the LinkGuard allowlist — "
                        "comma/space separated, optional)", parse_mode="HTML")
        return
    name, api = parts[0][:40], parts[1]
    hosts = []
    if len(parts) >= 3:
        hosts = [h.strip().lower() for h in re.split(r"[,\s]+", parts[2])
                 if h.strip()]
    if not api.startswith("http") or "url=" not in api:
        await msg.reply("❌ API must be a full template like "
                        "<code>https://arolinks.com/api?api=KEY&amp;url=</code> "
                        "(must contain <code>url=</code>)", parse_mode="HTML")
        return
    extras = await sh.get_extra_providers()
    if name.lower() == "vplink" or any(p["name"].lower() == name.lower()
                                       for p in extras):
        await msg.reply(f"❌ A provider named <b>{esc(name)}</b> already exists.",
                        parse_mode="HTML")
        return
    extras.append({"name": name, "api": api, "hosts": hosts})
    await repo.set_setting_json("shortener_providers", extras)
    try:
        if await lg.is_configured():
            from ..handlers.linkguard_cmds import push_ref_hosts
            await push_ref_hosts()
    except Exception:
        pass
    await msg.reply(f"✅ Added <b>{esc(name)}</b> as shortener #{len(extras)} in "
                    f"the rotation. Users finishing their current shortener get "
                    f"this one next (then it wraps back to vplink).",
                    parse_mode="HTML")


@router.message(Command("delshortener"))
async def cmd_delshortener(msg: Message) -> None:
    """Remove an extra provider by rotation index or name."""
    if await _reject_non_admin(msg):
        return
    arg = _args(msg)
    if not arg:
        await msg.reply("Usage: <code>/delshortener &lt;index|name&gt;</code> — "
                        "see /shorteners", parse_mode="HTML")
        return
    if arg == "0" or arg.lower() == "vplink":
        await msg.reply("❌ Provider 0 is the legacy /shortenerapi base — change "
                        "it with /shortenerapi; it can't be removed.",
                        parse_mode="HTML")
        return
    extras = await sh.get_extra_providers()
    target = None
    if arg.isdigit():
        i = int(arg) - 1
        if 0 <= i < len(extras):
            target = i
    else:
        for i, p in enumerate(extras):
            if p["name"].lower() == arg.lower():
                target = i
                break
    if target is None:
        await msg.reply(f"❌ No shortener <b>{esc(arg)}</b>. See /shorteners.",
                        parse_mode="HTML")
        return
    removed = extras.pop(target)
    await repo.set_setting_json("shortener_providers", extras)
    try:
        if await lg.is_configured():
            from ..handlers.linkguard_cmds import push_ref_hosts
            await push_ref_hosts()
    except Exception:
        pass
    await msg.reply(f"🗑 Removed <b>{esc(removed['name'])}</b> from the rotation. "
                    f"Users mid-rotation fall back to vplink automatically.",
                    parse_mode="HTML")
