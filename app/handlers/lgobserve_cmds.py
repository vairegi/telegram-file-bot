"""v5.1.0: /lgobserve /lghosts /lgallow /lgclear — LinkGuard referer learning.

Shorteners that strip or vary their Referer header false-positive legit users
at /finish2. Observe-mode records the real exit hosts so the admin can approve
them into the ref_hosts allowlist. Every worker call fails open (report, never
crash the bot).
"""
from __future__ import annotations

import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import linkguard, repo

router = Router()


def _parts(msg: Message) -> list:
    return (msg.text or "").split()[1:]


@router.message(Command("lgobserve"))
async def cmd_lgobserve(msg: Message) -> None:
    if not await repo.is_admin(msg.from_user.id):
        return
    a = _parts(msg)
    if not a or a[0].lower() not in ("on", "off", "status"):
        await msg.answer("Usage: /lgobserve on [minutes] | off | status")
        return
    sub = a[0].lower()
    if sub == "status":
        r = await linkguard.observed_hosts()
        if r is None:
            await msg.answer("⚠️ LinkGuard unreachable or not configured.")
            return
        left = int(r.get("observe_until") or 0) - int(time.time())
        state = f"🟢 ON ({max(0, left) // 60} min left)" if left > 0 else "🔴 OFF"
        await msg.answer(f"Observe-mode: {state}\n"
                         f"Learned hosts: {len(r.get('hosts') or [])} (see /lghosts)")
        return
    if sub == "on":
        minutes = 30
        if len(a) > 1:
            try:
                minutes = max(1, min(1440, int(a[1])))
            except ValueError:
                await msg.answer("Minutes must be a number (1-1440).")
                return
        r = await linkguard.set_observe(True, minutes)
        if r is None:
            await msg.answer("⚠️ LinkGuard unreachable — observe-mode NOT enabled.")
            return
        await msg.answer(
            f"🟢 Observe-mode ON for {minutes} min.\n"
            "Now solve ONE link through the failing shortener yourself, "
            "then check /lghosts and approve each with /lgallow <host>.")
        return
    r = await linkguard.set_observe(False, 0)
    await msg.answer("🔴 Observe-mode OFF." if r is not None
                     else "⚠️ LinkGuard unreachable.")


@router.message(Command("lghosts"))
async def cmd_lghosts(msg: Message) -> None:
    if not await repo.is_admin(msg.from_user.id):
        return
    r = await linkguard.observed_hosts()
    if r is None:
        await msg.answer("⚠️ LinkGuard unreachable or not configured.")
        return
    hosts = r.get("hosts") or []
    if not hosts:
        await msg.answer("No hosts learned yet. Enable /lgobserve on and solve one link.")
        return
    lines = ["<b>Observed referer hosts</b> (newest first):"]
    for h in hosts[:30]:
        lines.append(f"• <code>{h.get('host')}</code> — {h.get('hits', 1)} hit(s)")
    lines.append("\nApprove with /lgallow &lt;host&gt; · wipe with /lgclear")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("lgallow"))
async def cmd_lgallow(msg: Message) -> None:
    if not await repo.is_admin(msg.from_user.id):
        return
    a = _parts(msg)
    if not a:
        await msg.answer("Usage: /lgallow <host-or-url>  (e.g. /lgallow links.arolinks.com)")
        return
    r = await linkguard.add_ref_host(a[0])
    if r and r.get("ok"):
        await msg.answer(f"✅ <code>{r.get('host')}</code> added to the referer allowlist.\n"
                         "Applies to all future arrivals, including already-minted links.",
                         parse_mode="HTML")
    elif r:
        await msg.answer(f"⚠️ Rejected: {r.get('error')}")
    else:
        await msg.answer("⚠️ LinkGuard unreachable — host NOT added.")


@router.message(Command("lgclear"))
async def cmd_lgclear(msg: Message) -> None:
    if not await repo.is_admin(msg.from_user.id):
        return
    ok = await linkguard.clear_observed()
    await msg.answer("🧹 Learned-host list cleared." if ok
                     else "⚠️ LinkGuard unreachable — nothing cleared.")
