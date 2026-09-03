"""Setup commands: /start /help /whoami /addchannel /removechannel /listchannels
/setlog /setcursor /addadmin /removeadmin /listadmins /favs /rfavs —
plus deep-link Get-File redemption."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from ..config import settings
from ..services import posting, repo
from ..utils import esc, parse_channel_id, parse_tme_link, to_int

log = logging.getLogger("setup_cmds")
router = Router(name="setup_cmds")


# ------------------------- guards -------------------------
async def _reject_non_admin(msg: Message) -> bool:
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Admin only.")
    return True


async def _reject_non_super(msg: Message) -> bool:
    uid = msg.from_user.id
    if (await repo.is_super_admin(uid)) or uid == settings.super_admin_id:
        return False
    await msg.reply("🚫 Super-admin only.")
    return True


async def _bootstrap_super(uid: int) -> None:
    """Auto-add SUPER_ADMIN_ID from env on first contact."""
    if uid and uid == settings.super_admin_id and not (await repo.is_admin(uid)):
        await repo.add_admin(uid, is_super=True)


async def _track_user(msg: Message) -> None:
    """v2.7: remember who uses the bot so /favsall can show real names."""
    u = msg.from_user
    if not u:
        return
    try:
        await repo.upsert_directory_user(u.id, u.username, u.first_name)
    except Exception:
        pass


# ------------------------- /start -------------------------
@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(msg: Message, bot: Bot, command) -> None:
    await _bootstrap_super(msg.from_user.id)
    await _track_user(msg)
    args = (command.args or "").strip()
    if args.startswith("get_"):
        code = args[4:]
        cover = await repo.get_post_by_code(code)
        if not cover or cover.get("kind") != "cover":
            await msg.reply("❌ Unknown or invalid code.")
            return
        await msg.reply(f"📥 Delivering <b>#{cover.get('post_number') or '?'}</b>…",
                        parse_mode="HTML")
        res = await posting.deliver_to_user(bot, msg.from_user.id, cover)
        if not res.get("ok"):
            await msg.reply(f"❌ Delivery failed: <code>{esc(res.get('error',''))}</code>",
                            parse_mode="HTML")
        else:
            await msg.reply(f"✅ Delivered {res.get('delivered')} / {res.get('total')} files.")
        return
    await cmd_start_plain(msg)


@router.message(CommandStart())
async def cmd_start_plain(msg: Message) -> None:
    await _bootstrap_super(msg.from_user.id)
    await _track_user(msg)
    await msg.reply(
        "👋 <b>Welcome!</b>\n\n"
        "Tap 📥 <b>Get File #N</b> on any post in the main channel to receive it here.\n"
        "Use /help to see what you can do.",
        parse_mode="HTML",
    )


# ------------------------- /help -------------------------
_USER_HELP = (
    "<b>User commands</b>\n"
    "/start — welcome\n"
    "/help — this help\n"
    "/whoami — your id + role\n"
    "/favs — saved files\n"
    "/rfavs — remove saved files\n"
)

_ADMIN_HELP = (
    "<b>Admin commands</b>\n\n"
    "<b>🏗 Setup</b>\n"
    "/addchannel &lt;id&gt; &lt;database|main|log|backup&gt;\n"
    "/removechannel &lt;id&gt;\n"
    "/listchannels\n"
    "/setlog &lt;id&gt;\n"
    "/setcursor &lt;chan&gt; &lt;t.me/c/link&gt;\n"
    "/addadmin &lt;id&gt;  /addsuperadmin &lt;id&gt;  /removeadmin &lt;id&gt;  /listadmins\n"
    "/add &lt;chat_id&gt; @user1 @user2 … — bulk-add members (userbot; bots become admins)\n"
    "/broadcast — reply to any post to send it to all users (keeps the forward tag when the post has one)\n"
    "/favsall — top savers leaderboard\n\n"
    "<b>💾 Backups</b>\n"
    "/addbackup &lt;id&gt;  /removebackup &lt;id&gt;  /listbackup\n"
    "/backup &lt;id&gt;  /backup10 &lt;id&gt;\n"
    "/resetbackup [id]  /undoresetbackup &lt;id&gt;  /dltbackup &lt;id&gt;\n"
    "/pausebackup  /resumebackup  /backupstatus\n\n"
    "<b>💻 MTProto userbot</b>\n"
    "/tgsetapi &lt;api_id&gt; &lt;api_hash&gt;\n"
    "/tglogin &lt;+phone&gt;   /tgcode &lt;code&gt;   /tgstatus\n"
    "/backfill_start &lt;chan&gt; [from_id]\n"
    "/backfill_resume &lt;chan&gt;   /backfill_stop   /backfill_status   /backfill_reset\n\n"
    "<b>⏱ Queue &amp; drip</b>\n"
    "/queue  /queueinfo  /peek [N]  /whereami  /find &lt;text&gt;\n"
    "/dripnow [N]   /dripstop\n"
    "/setschedule 07:00,19:00 15   /scheduleoff\n"
    "/pauseposting  /resumeposting\n"
    "/skip #N   /skip &lt;link&gt;   /skip_range #A-#B\n"
    "/unskip #N   /jumpto #N   /queue_reset CONFIRM\n"
    "/repost #N   /preview #N   /deletepost #N\n\n"
    "<b>🛡 Content controls</b>\n"
    "/spoiler &lt;1|0&gt;   /protect &lt;1|0&gt;\n"
    "/autodelete &lt;8h|2m|1day|off&gt; — delivered files self-destruct\n"
    "/fsub &lt;chat_id&gt; &lt;link&gt; — join-gate   /fsublist   /fsubremove\n"
    "/fsub_sync &lt;chat_id&gt; — import pending join-requests (userbot)\n"
    "/postcaption &lt;text&gt;   /filecaption &lt;text&gt;\n\n"
    "<b>🧹 MTProto cleanup</b>\n"
    "/massdlt &lt;chat_id&gt; &lt;start_link&gt; &lt;end_link&gt;\n"
    "/massdlt_status   /massdlt_stop\n\n"
    "<b>📨 MTProto forward</b>\n"
    "/forward &lt;dest_id[,dest2,…]&gt; &lt;start_link&gt; &lt;end_link&gt; — forward a whole range (tag kept, auto rate-rests)\n"
    "/forward_status   /forward_stop   /forward_resume\n\n"
    "<b>🩺 Diagnostics</b>\n"
    "/debug   /stats\n"
)


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await _bootstrap_super(msg.from_user.id)
    uid = msg.from_user.id
    if (await repo.is_admin(uid)) or uid == settings.super_admin_id:
        await msg.reply(_USER_HELP + "\n" + _ADMIN_HELP, parse_mode="HTML")
    else:
        await msg.reply(_USER_HELP, parse_mode="HTML")


# ------------------------- /whoami -------------------------
@router.message(Command("whoami"))
async def cmd_whoami(msg: Message) -> None:
    await _bootstrap_super(msg.from_user.id)
    uid = msg.from_user.id
    role = ("super-admin" if (await repo.is_super_admin(uid)) or uid == settings.super_admin_id
            else "admin" if (await repo.is_admin(uid))
            else "user")
    await msg.reply(f"👤 <code>{uid}</code>\nRole: <b>{role}</b>", parse_mode="HTML")


# ------------------------- channels -------------------------
_ROLE_ALIASES = {"database": "database", "db": "database",
                 "main": "main", "log": "log", "backup": "backup"}


@router.message(Command("addchannel"))
async def cmd_addchannel(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/addchannel &lt;chat_id&gt; "
                        "&lt;database|main|log|backup&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    role = _ROLE_ALIASES.get(parts[2].lower())
    if not cid or not role:
        await msg.reply("❌ Bad chat id or role.")
        return
    title = None
    try:
        chat = await msg.bot.get_chat(cid)
        title = getattr(chat, "title", None)
    except Exception:
        pass
    await repo.add_channel(cid, role, title=title)
    await msg.reply(f"✅ Added <code>{cid}</code> as <b>{role}</b>"
                    + (f" ({esc(title)})" if title else ""),
                    parse_mode="HTML")


@router.message(Command("removechannel"))
async def cmd_removechannel(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/removechannel &lt;chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    await repo.remove_channel(cid)
    await msg.reply(f"🗑 Removed <code>{cid}</code>.", parse_mode="HTML")


@router.message(Command("listchannels"))
async def cmd_listchannels(msg: Message, bot: Bot) -> None:
    """v2.5: each channel shows its TITLE as a clickable link to the channel.
    Titles are refreshed live via getChat and cached back to the DB."""
    if await _reject_non_admin(msg):
        return
    rows = await repo.list_all_channels()
    if not rows:
        await msg.reply("💤 No channels registered.")
        return
    lines = ["<b>Registered channels</b>"]
    for r in rows:
        cid = int(r["chat_id"])
        title = (r.get("title") or "").strip()
        # Lazily resolve titles via getChat once, cache in DB.
        if not title:
            try:
                chat = await bot.get_chat(cid)
                title = getattr(chat, "title", "") or ""
                if title:
                    await repo.update_channel_title(cid, title)
            except Exception:
                title = ""
        if not title:
            title = str(cid)
        # v2.6: prefer the channel's real invite link (bot is admin, so
        # export_chat_invite_link works). Cached in settings so we only call
        # the API once per channel. Falls back to t.me/c/<id> when the bot
        # lacks permission or the channel is public without a link.
        ck = f"invite:{cid}"
        link = await repo.get_setting(ck)
        if not link:
            try:
                link = await bot.export_chat_invite_link(cid)
                await repo.set_setting(ck, link)
            except Exception:
                uname_link = None
                try:
                    chat2 = await bot.get_chat(cid)
                    uname = getattr(chat2, "username", None)
                    if uname:
                        uname_link = f"https://t.me/{uname}"
                except Exception:
                    pass
                if uname_link:
                    link = uname_link
                    await repo.set_setting(ck, link)
                else:
                    bare = (str(cid).replace("-100", "", 1)
                            if str(cid).startswith("-100") else str(cid))
                    link = f"https://t.me/c/{bare}"
        lines.append(f'• <a href="{link}">{esc(title)}</a> '
                     f'<code>{cid}</code> — {r["role"]}')
    await msg.reply("\n".join(lines), parse_mode="HTML",
                    disable_web_page_preview=True)


@router.message(Command("setlog"))
async def cmd_setlog(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/setlog &lt;chat_id&gt;</code>", parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    if not cid:
        await msg.reply("❌ Bad chat id.")
        return
    # Remove any existing log channels first, then add the new one.
    existing = await repo.get_log_channel()
    if existing:
        await repo.remove_channel(int(existing["chat_id"]))
    await repo.add_channel(cid, "log", title=None)
    await msg.reply(f"✅ Log channel set to <code>{cid}</code>. "
                    f"Spoiler-forward trick will use this channel.",
                    parse_mode="HTML")


@router.message(Command("setcursor"))
async def cmd_setcursor(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/setcursor &lt;chat_id&gt; "
                        "&lt;t.me/c/link&gt;</code>", parse_mode="HTML")
        return
    cid = parse_channel_id(parts[1])
    link = parse_tme_link(parts[2])
    if not cid or not link:
        await msg.reply("❌ Bad chat id or link.")
        return
    _lcid, _uname, mid = link
    # Set cursor to mid-1 so next capture is mid.
    await repo.set_cursor(cid, max(0, int(mid) - 1))
    await msg.reply(f"✅ Cursor for <code>{cid}</code> set to "
                    f"<code>{mid - 1}</code>. Next capture: msg <b>{mid}</b>.",
                    parse_mode="HTML")


# ------------------------- admins -------------------------
@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/addadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    uid = to_int(parts[1])
    if not uid:
        await msg.reply("❌ Bad user id.")
        return
    await repo.add_admin(int(uid), is_super=False)
    await msg.reply(f"✅ Added admin <code>{uid}</code>.", parse_mode="HTML")


@router.message(Command("addsuperadmin"))
async def cmd_addsuperadmin(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/addsuperadmin &lt;user_id&gt;</code>",
                        parse_mode="HTML")
        return
    uid = to_int(parts[1])
    if not uid:
        await msg.reply("❌ Bad user id.")
        return
    await repo.add_admin(int(uid), is_super=True)
    await msg.reply(f"⭐ Added super-admin <code>{uid}</code>.", parse_mode="HTML")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    uid = to_int(parts[1])
    if not uid:
        await msg.reply("❌ Bad user id.")
        return
    await repo.remove_admin(int(uid))
    await msg.reply(f"🗑 Removed admin <code>{uid}</code>.", parse_mode="HTML")


@router.message(Command("listadmins"))
async def cmd_listadmins(msg: Message, bot: Bot) -> None:
    """v2.6: username/first-name with an embedded profile link (tg://user?id=)."""
    if await _reject_non_admin(msg):
        return
    rows = await repo.list_admins()
    if not rows:
        await msg.reply("💤 No admins registered.")
        return
    lines = ["<b>Admins</b>"]
    for r in rows:
        uid = int(r["user_id"])
        tag = "⭐ super" if int(r.get("is_super") or 0) else "admin"
        name = ""
        try:
            chat = await bot.get_chat(uid)
            uname = getattr(chat, "username", None)
            fname = getattr(chat, "first_name", "") or ""
            name = f"@{uname}" if uname else (fname or str(uid))
        except Exception:
            name = str(uid)
        lines.append(f'• <a href="tg://user?id={uid}">{esc(name)}</a> — {tag}')
    await msg.reply("\n".join(lines), parse_mode="HTML")


# ------------------------- favorites -------------------------
@router.message(Command("favs"))
async def cmd_favs(msg: Message, bot: Bot) -> None:
    """List saved files with the cover TITLE as a clickable deep link.
    Tapping a title re-delivers that post's files (t.me/<bot>?start=get_<code>)."""
    await _bootstrap_super(msg.from_user.id)
    await _track_user(msg)
    rows = await repo.list_favorites(msg.from_user.id)
    if not rows:
        await msg.reply("💤 No saved files.")
        return
    from ..utils import first_line, clean_caption
    try:
        me = await bot.get_me()
        bot_name = me.username or ""
    except Exception:
        bot_name = ""
    lines = ["<b>❤️ Your saved files</b>"]
    for i, r in enumerate(rows[:50], start=1):
        title = first_line(clean_caption(r.get("caption")), 60) or "Untitled"
        code = r.get("code") or ""
        if bot_name and code:
            link = f"https://t.me/{bot_name}?start=get_{code}"
            lines.append(f'{i}. <a href="{link}">{esc(title)}</a>')
        else:
            lines.append(f"{i}. {esc(title)}")
    await msg.reply("\n".join(lines), parse_mode="HTML",
                    disable_web_page_preview=True)


@router.message(Command("rfavs"))
async def cmd_rfavs(msg: Message) -> None:
    await _bootstrap_super(msg.from_user.id)
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/rfavs &lt;#N&gt;</code>\n"
                        "Removes the given post from your favorites.",
                        parse_mode="HTML")
        return
    from ..utils import parse_hash_number
    n = parse_hash_number(parts[1])
    if not n:
        await msg.reply("❌ Bad #N.")
        return
    row = await repo.get_post_by_number(int(n))
    if not row:
        await msg.reply(f"❌ No post #{n}.")
        return
    removed = await repo.remove_favorites_for_cover(
        msg.from_user.id, int(row["source_chat_id"]), int(row["source_message_id"]))
    await msg.reply(f"🗑 Removed #{n} from favorites ({removed} file(s)).")
