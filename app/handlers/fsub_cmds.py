"""v2.5 commands: /autodelete + force-subscribe gate (/fsub /fsublist /fsubremove)
plus the 🔄 Retry callback."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatJoinRequest, Message

from ..services import autodelete as ad
from ..services import fsub, posting, repo
from ..utils import esc, parse_channel_id
from .setup_cmds import _reject_non_admin

log = logging.getLogger("fsub_cmds")
router = Router(name="fsub_cmds")


# ------------------------- /autodelete -------------------------
@router.message(Command("autodelete"))
async def cmd_autodelete(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = ad.humanize(await ad.get_ms())
        await msg.reply(
            f"🕒 Auto-delete is currently <b>{cur}</b>.\n"
            f"Usage: <code>/autodelete 8h</code> · <code>12h</code> · <code>2m</code> · "
            f"<code>30s</code> · <code>1day</code> · <code>off</code>\n"
            f"Delivered DM content self-destructs after this duration.",
            parse_mode="HTML")
        return
    arg = parts[1].lower()
    if arg == "off":
        await ad.set_ms(0)
        await msg.reply("✅ Auto-delete disabled.")
        return
    ms = ad.parse_duration_ms(arg)
    if not ms or ms < 1000:
        await msg.reply("❌ Bad duration. Examples: 8h, 12h, 2m, 30s, 1day, off")
        return
    if ms > 7 * 86_400_000:
        await msg.reply("❌ Max is 7 days.")
        return
    await ad.set_ms(ms)
    await msg.reply(f"✅ Auto-delete set to <b>{ad.humanize(ms)}</b>. "
                    f"Everything delivered via Get File will vanish after that.",
                    parse_mode="HTML")


# ------------------------- /fsub -------------------------
@router.message(Command("fsub"))
async def cmd_fsub(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply(
            "Usage: <code>/fsub &lt;chat_id&gt; &lt;invite_link&gt;</code>\n"
            "Example: <code>/fsub -1001234567890 https://t.me/+AbCdEfGh</code>\n"
            "The bot must be an ADMIN in that channel (needed for member checks).",
            parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    link = parts[2].strip()
    if not cid or not (link.startswith("https://t.me/") or link.startswith("http://t.me/")):
        await msg.reply("❌ Bad chat id or invite link (must be a t.me link).")
        return
    # Best-effort: fetch the real channel title now so /fsublist is pretty.
    title = ""
    try:
        chat = await bot.get_chat(cid)
        title = getattr(chat, "title", "") or ""
    except Exception as e:
        await msg.reply(
            f"⚠️ Could not fetch channel info: <code>{esc(str(e))}</code>\n"
            f"Make sure the bot is ADMIN in <code>{cid}</code>, then re-add.",
            parse_mode="HTML")
        return
    await fsub.add_fsub(cid, link, title)
    await msg.reply(f"✅ Join-gate added: <b>{esc(title or str(cid))}</b>\n{link}",
                    parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("fsublist"))
async def cmd_fsublist(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    rows = await fsub.list_fsub()
    if not rows:
        await msg.reply("💤 No join-gate channels. Users get files directly.")
        return
    lines = ["<b>🔒 Join-gate channels</b>"]
    for r in rows:
        title = r.get("title") or str(r.get("chat_id"))
        link = r.get("link") or ""
        if link:
            lines.append(f'• <a href="{link}">{esc(title)}</a> '
                         f'(<code>{r["chat_id"]}</code>)')
        else:
            lines.append(f'• {esc(title)} (<code>{r["chat_id"]}</code>)')
    await msg.reply("\n".join(lines), parse_mode="HTML",
                    disable_web_page_preview=True)


@router.message(Command("fsubremove"))
async def cmd_fsubremove(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/fsubremove &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    if await fsub.remove_fsub(cid):
        await msg.reply(f"🗑 Removed <code>{cid}</code> from join-gate.",
                        parse_mode="HTML")
    else:
        await msg.reply("❌ That channel is not in the join-gate list.")


# ------------------------- 🔄 Retry callback -------------------------
# ------------------------- join-request tracking (v3.3.1) -------------------------
@router.chat_join_request()
async def on_fsub_join_request(ev: ChatJoinRequest) -> None:
    """Record join requests for fsub channels — the gate treats a recorded
    request as satisfying the join requirement (private approval channels)."""
    try:
        rows = await fsub.list_fsub()
        if any(int(r.get("chat_id") or 0) == int(ev.chat.id) for r in rows):
            await repo.add_fsub_request(int(ev.chat.id), int(ev.from_user.id))
    except Exception as e:
        log.warning("join-request record failed: %s", e)


@router.message(Command("fsub_sync"))
async def cmd_fsub_sync(msg: Message) -> None:
    """Import already-pending join requests via the MTProto userbot (covers
    requests sent BEFORE the recorder was deployed). Userbot must be ADMIN."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply(
            "Usage: <code>/fsub_sync &lt;chat_id&gt;</code>\n"
            "Imports pending join requests for that fsub channel. "
            "The userbot must be ADMIN there.",
            parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    from ..services import userbot as ub
    try:
        uids = await ub.fetch_join_requests(cid)
    except Exception as e:
        await msg.reply(f"❌ Sync failed: <code>{esc(str(e))}</code>\n"
                        "Check /tgstatus and make sure the userbot is ADMIN "
                        "in that channel.",
                        parse_mode="HTML")
        return
    n = 0
    for uid in uids:
        try:
            await repo.add_fsub_request(cid, int(uid))
            n += 1
        except Exception:
            pass
    await msg.reply(f"✅ Imported <b>{n}</b> pending join request(s) for "
                    f"<code>{cid}</code>.\nThose users now pass the gate with "
                    f"🔄 Retry — no approval needed.",
                    parse_mode="HTML")


@router.callback_query(lambda c: (c.data or "").startswith("fsub_retry:"))
async def on_fsub_retry(cb: CallbackQuery, bot: Bot) -> None:
    code = (cb.data or "").split(":", 1)[1]
    missing = await fsub.unjoined_channels(bot, cb.from_user.id)
    if missing:
        names = ", ".join(esc(c.get("title") or str(c.get("chat_id"))) for c in missing)
        await cb.answer(f"❌ Still not joined: {names}", show_alert=True)
        return
    await cb.answer("✅ Verified! Delivering…")
    cover = await repo.get_post_by_code(code)
    if not cover or cover.get("kind") != "cover":
        try:
            await cb.message.reply("❌ That post is no longer available.")
        except Exception:
            pass
        return
    res = await posting.deliver_to_user(bot, cb.from_user.id, cover)
    if res.get("ok"):
        # Verified + delivered — drop recorded join requests so none linger.
        try:
            await repo.remove_fsub_requests_for_user(cb.from_user.id)
        except Exception:
            pass
    try:
        if res.get("ok"):
            await cb.message.reply(f"✅ Delivered {res.get('delivered')} / {res.get('total')} files.")
        else:
            await cb.message.reply(f"❌ Delivery failed: <code>{esc(res.get('error',''))}</code>",
                                   parse_mode="HTML")
    except Exception:
        pass
