"""v5.0: admin commands for the LinkGuard (Three-Door) Cloudflare Workers gate.

  /linkguard                      status overview
  /linkguard on|off               toggle the protection
  /linkguard setup <base> <key>   set worker base URL + shared ADMIN_KEY
  /linkguard refhosts             show the finish2 referer allowlist
  /linkguard addrefhost <host>    manually allow an extra referer host
  /linkguard delrefhost <host>    remove a manually-added host
  /linkguard revoke <slug>        retire one public slug (and its grant)
  /linkguard honeypot [n]         mint n decoy slugs (default 5)
  /linkguard decoys [n]           alias of honeypot
  /linkguard logs [n]             last n denials/security events (default 15)
  /linkguard health               ping the worker's /api/health

Every bot-side failure of the worker is FAIL-OPEN.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import linkguard as lg
from ..services import repo
from ..utils import esc
from .setup_cmds import _reject_non_admin

log = logging.getLogger("linkguard_cmds")
router = Router(name="linkguard_cmds")

MAX_SLUG_LEN = 32
REF_EXTRA_KEY = "linkguard_ref_hosts_extra"


def _args(msg: Message) -> str:
    return (msg.text or "").partition(" ")[2].strip()


async def _derived_ref_hosts() -> list:
    """Hosts of ALL configured shorteners (v5.1) — any of them may be the
    finish2 referer because of per-user rotation."""
    try:
        from ..services import shortener as _sh
        hosts = await _sh.all_provider_hosts()
        if hosts:
            return hosts
    except Exception:
        pass
    api = ((await repo.get_setting("shortener_api")) or "").strip()
    host = lg.worker_host_from_url(api)
    return [host] if host else []


async def _extra_ref_hosts() -> list:
    rows = await repo.get_setting_json(REF_EXTRA_KEY, [])
    return [str(h).strip().lower() for h in (rows or []) if str(h).strip()]


async def _all_ref_hosts() -> list:
    seen, out = set(), []
    for h in (await _derived_ref_hosts()) + (await _extra_ref_hosts()):
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


async def push_ref_hosts() -> bool:
    hosts = await _all_ref_hosts()
    if not hosts:
        return False
    return await lg.push_ref_hosts(hosts)


@router.message(Command("linkguard"))
async def cmd_linkguard(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = _args(msg).split()
    cmd = (parts[0].lower() if parts else "")

    if cmd == "on":
        if not await lg.is_configured():
            await msg.reply("❌ Not configured yet. First:\n"
                            "<code>/linkguard setup &lt;worker-url&gt; &lt;admin-key&gt;</code>",
                            parse_mode="HTML")
            return
        await repo.set_setting("linkguard_enabled", "1")
        await msg.reply("✅ LinkGuard Three-Door protection is now <b>ON</b>.\n"
                        "Gate buttons now lead through the Turnstile worker.",
                        parse_mode="HTML")
        return
    if cmd == "off":
        await repo.set_setting("linkguard_enabled", None)
        await msg.reply("❌ LinkGuard is now <b>OFF</b> — shortener gate back to "
                        "direct shortener links.", parse_mode="HTML")
        return

    if cmd == "setup":
        if len(parts) < 3:
            await msg.reply("Usage: <code>/linkguard setup &lt;worker-url&gt; "
                            "&lt;admin-key&gt;</code>", parse_mode="HTML")
            return
        base = parts[1].strip().rstrip("/")
        key = parts[2].strip()
        if not base.startswith("https://"):
            await msg.reply("❌ Worker URL must start with https://")
            return
        if len(key) < 16:
            await msg.reply("❌ Admin key too short (min 16 chars).")
            return
        await repo.set_setting("linkguard_base", base)
        await repo.set_setting("linkguard_key", key)
        h = await lg.health()
        if h and h.get("ok"):
            await msg.reply(f"✅ Saved. Worker reachable — active slugs: "
                            f"<b>{h.get('slugs', '?')}</b>.\nNow run "
                            f"<code>/linkguard on</code>.", parse_mode="HTML")
        else:
            await msg.reply("⚠️ Saved, but the worker did not answer "
                            "<code>/api/health</code>. (LinkGuard stays fail-open).",
                            parse_mode="HTML")
        return

    if cmd == "refhosts":
        await msg.reply(
            "<b>🌐 finish2 referer allowlist</b>\n"
            f"auto (from /shortenerapi): <code>{esc(', '.join(await _derived_ref_hosts()) or '—')}</code>\n"
            f"manual: <code>{esc(', '.join(await _extra_ref_hosts()) or '—')}</code>\n"
            f"live on worker: <code>{esc(', '.join(await lg.get_ref_hosts()) or '—')}</code>\n\n"
            "<i>/linkguard addrefhost &lt;host&gt; · /linkguard delrefhost &lt;host&gt;</i>",
            parse_mode="HTML")
        return
    if cmd in ("addrefhost", "delrefhost"):
        if len(parts) < 2:
            await msg.reply(f"Usage: <code>/linkguard {cmd} &lt;host&gt;</code>",
                            parse_mode="HTML")
            return
        host = parts[1].strip().lower().strip(".")
        extra = await _extra_ref_hosts()
        if cmd == "addrefhost":
            if host not in extra:
                extra.append(host)
                await repo.set_setting_json(REF_EXTRA_KEY, extra)
            ok = await push_ref_hosts()
            await msg.reply(f"✅ Added <code>{esc(host)}</code>"
                            + (" and pushed to worker." if ok else
                               ". ⚠️ Push failed — retry later."), parse_mode="HTML")
        else:
            if host in extra:
                extra.remove(host)
                await repo.set_setting_json(REF_EXTRA_KEY, extra)
            ok = await push_ref_hosts()
            await msg.reply(f"🗑 Removed <code>{esc(host)}</code>"
                            + (" and pushed to worker." if ok else
                               ". ⚠️ Push failed — retry later."), parse_mode="HTML")
        return

    if cmd == "revoke":
        if len(parts) < 2 or not parts[1].strip() or len(parts[1].strip()) > MAX_SLUG_LEN:
            await msg.reply("Usage: <code>/linkguard revoke &lt;slug&gt;</code>",
                            parse_mode="HTML")
            return
        slug = parts[1].strip()
        ok = await lg.revoke(slug)
        await msg.reply(("🗑 Slug <code>%s</code> revoked." if ok else
                         "❌ Revoke failed for <code>%s</code>.") % esc(slug),
                        parse_mode="HTML")
        return

    if cmd in ("honeypot", "decoys"):
        n = 5
        if len(parts) >= 2:
            try:
                n = max(1, min(100, int(parts[1])))
            except ValueError:
                await msg.reply("❌ Count must be a number (1-100).")
                return
        created = await lg.add_decoys(n)
        if created:
            await msg.reply(f"🍯 Minted <b>{created}</b> decoy slug(s).",
                            parse_mode="HTML")
        else:
            await msg.reply("❌ Worker did not confirm decoy minting (fail-open).")
        return

    if cmd == "logs":
        n = 15
        if len(parts) >= 2:
            try:
                n = max(1, min(50, int(parts[1])))
            except ValueError:
                pass
        rows = await lg.get_logs(limit=n)
        if not rows:
            await msg.reply("💤 No security events (or worker unreachable).")
            return
        lines = [f"<b>🛡 LinkGuard events (last {len(rows)})</b>"]
        for r in rows:
            lines.append(
                f"<code>{esc(str(r.get('ts', ''))[:16])}</code> "
                f"<b>{esc(str(r.get('event', '')))}</b> "
                f"<code>{esc(str(r.get('reason', '')))}</code> "
                f"slug=<code>{esc(str(r.get('slug', ''))[:12])}</code> "
                f"ip=<code>{esc(str(r.get('ip', '')))}</code>")
        await msg.reply("\n".join(lines), parse_mode="HTML")
        return

    if cmd == "health":
        h = await lg.health()
        if h and h.get("ok"):
            await msg.reply(f"🟢 Worker OK — active slugs: <b>{h.get('slugs', '?')}</b>",
                            parse_mode="HTML")
        else:
            await msg.reply("🔴 Worker unreachable (or /linkguard setup missing). "
                            "Gate is failing open.")
        return

    enabled = await lg.enabled()
    configured = await lg.is_configured()
    slugs = await lg.list_active() if (enabled and configured) else []
    await msg.reply(
        "<b>🛡 LinkGuard — Three-Door Security</b>\n"
        f"status: <b>{'ON ✅' if enabled else 'OFF ❌'}</b>\n"
        f"worker: <code>{esc(await lg.base_url()) or '❌ not set'}</code>\n"
        f"admin key: <b>{'✅ set' if (await lg.admin_key()) else '❌ not set'}</b>\n"
        f"active slugs: <b>{len(slugs)}</b>\n"
        f"referer hosts: auto=<code>{esc(', '.join(await _derived_ref_hosts()) or '—')}</code> "
        f"manual=<code>{esc(', '.join(await _extra_ref_hosts()) or '—')}</code>\n\n"
        "<i>/linkguard on|off · setup &lt;url&gt; &lt;key&gt; · refhosts · "
        "addrefhost · delrefhost · revoke &lt;slug&gt; · honeypot [n] · "
        "decoys [n] · logs [n] · health</i>",
        parse_mode="HTML")
