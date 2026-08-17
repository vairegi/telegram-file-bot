"""Command handlers (aiogram 3).

Command roster grouped by RULES.txt / lovable command reference.
Strict role separation:
  - Regular users see only General + Discovery commands.
  - Admin/Super-admin commands reply with '🚫 Admin only' for regular users.
"""
from __future__ import annotations

import html
import logging
import re
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import db
from ..config import settings as cfg
from ..services import posting, repo, scheduler as sched, sync as sync_svc, tg, users
from ..utils import (
    esc,
    now_iso,
    parse_channel_id,
    parse_tme_link,
    to_int,
    truncate,
)

log = logging.getLogger("commands")
router = Router(name="commands")


# ============================================================
# Guards
# ============================================================
def _is_admin(uid: int) -> bool:
    return users.is_admin(uid) or users.is_super_admin(uid)


def _is_super(uid: int) -> bool:
    return users.is_super_admin(uid)


async def _reject_non_admin(msg: Message) -> bool:
    if not _is_admin(msg.from_user.id):
        await msg.reply("🚫 Admin only.")
        return True
    return False


async def _reject_non_super(msg: Message) -> bool:
    if not _is_super(msg.from_user.id):
        await msg.reply("🚫 Super-admin only.")
        return True
    return False


async def _touch_user(msg: Message) -> None:
    u = msg.from_user
    if not u:
        return
    users.upsert_user(u.id, u.username, u.first_name, u.last_name)


# ============================================================
# /start — bootstraps super-admin, also handles deep-links (get_<code>)
# ============================================================
@router.message(CommandStart())
async def cmd_start(msg: Message, bot: Bot) -> None:
    await _touch_user(msg)
    if users.is_banned(msg.from_user.id):
        await msg.reply("🚫 You are banned from this bot.")
        return

    # Super-admin bootstrap (env SUPER_ADMIN_ID or first user).
    total_admins = db.query_scalar("SELECT COUNT(*) FROM admins") or 0
    su_env = int(getattr(cfg, "super_admin_id", 0) or 0)
    if total_admins == 0:
        if su_env and msg.from_user.id == su_env:
            users.add_admin(msg.from_user.id, msg.from_user.username,
                            msg.from_user.first_name, True, msg.from_user.id)
            await msg.reply("👑 You are now the <b>super-admin</b>.", parse_mode="HTML")

    # Deep-link get_<code> — deliver the requested cover + its PDFs
    args = (msg.text or "").split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""
    if payload.startswith("get_"):
        code = payload[4:]
        cover = repo.get_post_by_code(code)
        if not cover or cover.get("kind") != "cover":
            await msg.reply("❌ Post not found or expired.")
            return
        result = await posting.deliver_to_user(bot, msg.from_user.id, cover)
        if not result.get("ok"):
            await msg.reply("❌ Delivery failed. Try again in a moment.")
        return

    text = (
        "👋 Welcome!\n\n"
        "Use /help to see available commands.\n"
        "Tap any 📥 <b>Get File</b> button on a channel post to receive files here."
    )
    await msg.reply(text, parse_mode="HTML")


# ============================================================
# /help — different views for user vs admin
# ============================================================
USER_HELP = (
    "<b>👤 General</b>\n"
    "/start — welcome / redeem a Get-File link\n"
    "/help — this help\n"
    "/whoami — your Telegram id and role\n"
    "/favs — list your saved files\n"
    "/rfavs &lt;n&gt; [n…] — remove favorites by number\n"
    "/mystats — your fetch stats\n"
    "/streak — daily streak\n\n"
    "<b>🔎 Discovery</b>\n"
    "/random — a random post\n"
    "/recent — 10 most recent posts\n"
    "/leaderboard — top savers"
)

ADMIN_HELP = (
    USER_HELP + "\n\n"
    "<b>🛡 Admin management</b>\n"
    "/addadmin &lt;user_id&gt;  /removeadmin &lt;user_id&gt;\n"
    "/listadmins  /genimporttoken\n\n"
    "<b>📡 Channels</b>\n"
    "/addchannel &lt;chat_id&gt; &lt;role&gt; — roles: database | main | log | backup | forcesub\n"
    "/removechannel &lt;chat_id&gt;\n"
    "/listchannels\n"
    "/setlog &lt;chat_id&gt;\n\n"
    "<b>📝 Posting</b>\n"
    "/setcaption &lt;template&gt;\n"
    "/postcaption &lt;text&gt; — append text below cover-post captions\n"
    "/filecaption &lt;text&gt; — append text below delivered PDF captions\n"
    "/pauseposting  /resumeposting\n"
    "/repost &lt;code|#N&gt;\n"
    "/mpost &lt;link&gt;… — publish arbitrary links to main channel(s)\n"
    "/deletepost &lt;code|#N&gt;  /undelete &lt;code|#N&gt;  /deletedposts\n\n"
    "<b>⏱ Queue &amp; drip</b>\n"
    "/queue  /queueinfo — upcoming posts &amp; state\n"
    "/setschedule 07:00,19:00 15 — IST slots × batch per slot\n"
    "/scheduleoff — clear schedule\n"
    "/dripnow [N] — post next N covers now (default 1)\n"
    "/setcursor &lt;chat_id&gt; &lt;t.me/c/link&gt; — resume posting from that link\n\n"
    "<b>💾 Backups</b>\n"
    "/addbackup  /removebackup  /listbackup\n"
    "/backup  /backup10  /scandatabase  /resetbackup  /undoresetbackup\n"
    "/dltbackup  /pausebackup  /resumebackup  /backupstatus\n\n"
    "<b>🛡 Content controls</b>\n"
    "/protect &lt;1|0&gt; — enable/disable copy protection on all sends\n"
    "/spoiler &lt;1|0&gt;  /autodelete &lt;seconds|off&gt;\n"
    "/fsub  /fsublist  /fsubremove &lt;chat_id&gt;\n\n"
    "<b>👥 Users &amp; moderation</b>\n"
    "/stats  /duplicates  /doctor  /broadcast &lt;text&gt;\n"
    "/ban &lt;user_id&gt; [reason]  /unban &lt;user_id&gt;  /banlist  /unbanall\n"
    "/warn &lt;user_id&gt; [reason]  /warns &lt;user_id&gt;  /unwarn &lt;user_id&gt;\n"
    "/activity [n]  /health  /audit [n]\n\n"
    "<b>🌐 Web admin</b>\n"
    "/linkweb  /setweburl &lt;url&gt;"
)


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await _touch_user(msg)
    if _is_admin(msg.from_user.id):
        await msg.reply(ADMIN_HELP, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await msg.reply(USER_HELP, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("whoami"))
async def cmd_whoami(msg: Message) -> None:
    await _touch_user(msg)
    uid = msg.from_user.id
    role = "super-admin" if _is_super(uid) else ("admin" if _is_admin(uid) else "user")
    await msg.reply(f"🆔 <code>{uid}</code>\n🎭 <b>{role}</b>", parse_mode="HTML")


# ============================================================
# /favs and /rfavs
# ============================================================
def _cover_of_pdf(pdf_row: dict) -> Optional[dict]:
    parent_msg = pdf_row.get("parent_source_message_id")
    if not parent_msg:
        return None
    return repo.get_post_by_source(int(pdf_row["source_chat_id"]), int(parent_msg))


async def _bot_username(bot: Bot) -> str:
    return await posting.get_bot_username(bot)


@router.message(Command("favs"))
async def cmd_favs(msg: Message, bot: Bot) -> None:
    await _touch_user(msg)
    favs = users.list_favorites(msg.from_user.id, limit=50)
    if not favs:
        await msg.reply("💔 No favorites yet.\nTap the ❤️ Save button under a delivered PDF to add one.")
        return
    uname = await _bot_username(bot)
    lines = ["<b>❤️ Your saved files</b>"]
    for i, pdf in enumerate(favs, start=1):
        cover = _cover_of_pdf(pdf)
        if not cover:
            title = pdf.get("file_name") or "(untitled)"
            code = None
        else:
            title = (cover.get("caption") or "").splitlines()[0].strip() if cover.get("caption") else \
                    (pdf.get("file_name") or "(untitled)")
            code = cover.get("code")
        title = truncate(title, 80)
        if code:
            link = f"https://t.me/{uname}?start=get_{code}"
            lines.append(f"{i}. <a href=\"{link}\">{esc(title)}</a>")
        else:
            lines.append(f"{i}. {esc(title)}")
    lines.append("")
    lines.append("Remove with <code>/rfavs 1 2 3</code>")
    await msg.reply("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


def _parse_indices(args: str) -> List[int]:
    out: List[int] = []
    if not args:
        return out
    for tok in re.split(r"[\s,]+", args.strip()):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            try:
                lo, hi = int(a), int(b)
                if lo <= hi:
                    out.extend(range(lo, hi + 1))
            except Exception:
                continue
        else:
            try:
                out.append(int(tok))
            except Exception:
                continue
    seen = set()
    result: List[int] = []
    for n in out:
        if n > 0 and n not in seen:
            seen.add(n)
            result.append(n)
    return result


@router.message(Command("rfavs"))
async def cmd_rfavs(msg: Message) -> None:
    await _touch_user(msg)
    parts = (msg.text or "").split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""
    idxs = _parse_indices(args)
    if not idxs:
        await msg.reply("Usage: <code>/rfavs 1</code>  or  <code>/rfavs 1 2 3</code>  or  <code>/rfavs 1-5</code>",
                        parse_mode="HTML")
        return
    favs = users.list_favorites(msg.from_user.id, limit=100)
    removed = 0
    for i in idxs:
        if 1 <= i <= len(favs):
            users.remove_favorite(msg.from_user.id, int(favs[i - 1]["id"]))
            removed += 1
    await msg.reply(f"🗑 Removed {removed} favorite(s).")


@router.message(Command("mystats"))
async def cmd_mystats(msg: Message) -> None:
    await _touch_user(msg)
    row = db.query_one(
        "SELECT files_fetched, files_fetched_today FROM users WHERE telegram_user_id=?",
        (msg.from_user.id,)) or {}
    fav_n = db.query_scalar("SELECT COUNT(*) FROM favorites WHERE user_id=?", (msg.from_user.id,)) or 0
    st = users.get_streak(msg.from_user.id)
    await msg.reply(
        f"📊 <b>Your stats</b>\n"
        f"• Fetched total: {int(row.get('files_fetched') or 0)}\n"
        f"• Fetched today: {int(row.get('files_fetched_today') or 0)}\n"
        f"• Favorites: {fav_n}\n"
        f"• Streak: {st.get('current',0)} 🔥 (longest {st.get('longest',0)})",
        parse_mode="HTML")


@router.message(Command("streak"))
async def cmd_streak(msg: Message) -> None:
    await _touch_user(msg)
    st = users.get_streak(msg.from_user.id)
    await msg.reply(f"🔥 Streak: <b>{st.get('current',0)}</b>  (longest {st.get('longest',0)})",
                    parse_mode="HTML")


# ============================================================
# Discovery
# ============================================================
@router.message(Command("random"))
async def cmd_random(msg: Message, bot: Bot) -> None:
    await _touch_user(msg)
    row = db.query_one(
        "SELECT * FROM posts WHERE kind='cover' AND post_number IS NOT NULL ORDER BY RANDOM() LIMIT 1")
    if not row:
        await msg.reply("No posts yet.")
        return
    await posting.deliver_to_user(bot, msg.from_user.id, row)


@router.message(Command("recent"))
async def cmd_recent(msg: Message, bot: Bot) -> None:
    await _touch_user(msg)
    rows = db.query_all(
        "SELECT code, post_number, caption FROM posts WHERE kind='cover' "
        "AND post_number IS NOT NULL ORDER BY post_number DESC LIMIT 10")
    if not rows:
        await msg.reply("No posts yet.")
        return
    uname = await _bot_username(bot)
    lines = ["<b>🕒 Recent</b>"]
    for r in rows:
        title = (r.get("caption") or "").splitlines()[0].strip() if r.get("caption") else "(untitled)"
        title = truncate(title, 60)
        link = f"https://t.me/{uname}?start=get_{r['code']}"
        lines.append(f"#{r['post_number']} · <a href=\"{link}\">{esc(title)}</a>")
    await msg.reply("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("leaderboard"))
async def cmd_leaderboard(msg: Message) -> None:
    await _touch_user(msg)
    rows = db.query_all(
        "SELECT user_id, COUNT(*) AS n FROM favorites GROUP BY user_id ORDER BY n DESC LIMIT 10")
    if not rows:
        await msg.reply("No leaderboard yet.")
        return
    lines = ["<b>🏆 Top savers</b>"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. <code>{r['user_id']}</code> — {r['n']} saved")
    await msg.reply("\n".join(lines), parse_mode="HTML")


# ============================================================
# Admin management
# ============================================================
@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    uid = to_int(parts[1]) if len(parts) > 1 else None
    if not uid:
        await msg.reply("Usage: <code>/addadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    users.add_admin(uid, None, None, False, msg.from_user.id)
    await msg.reply(f"✅ Admin added: <code>{uid}</code>", parse_mode="HTML")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    uid = to_int(parts[1]) if len(parts) > 1 else None
    if not uid:
        await msg.reply("Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    users.remove_admin(uid)
    await msg.reply(f"✅ Admin removed: <code>{uid}</code>", parse_mode="HTML")


@router.message(Command("listadmins"))
async def cmd_listadmins(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    rows = users.list_admins()
    if not rows:
        await msg.reply("No admins yet.")
        return
    lines = ["<b>🛡 Admins</b>"]
    for r in rows:
        tag = "👑" if int(r.get("is_super_admin") or 0) else "🛡"
        lines.append(f"{tag} <code>{r['telegram_user_id']}</code>  {esc(r.get('first_name') or '')}")
    await msg.reply("\n".join(lines), parse_mode="HTML")


# ============================================================
# Channels
# ============================================================
@router.message(Command("addchannel"))
async def cmd_addchannel(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/addchannel &lt;chat_id&gt; &lt;role&gt;</code>\nRoles: "
                        "database | main | log | backup | forcesub", parse_mode="HTML")
        return
    chat_id = parse_channel_id(parts[1])
    role = parts[2].strip().lower()
    if not chat_id or role not in repo.CHANNEL_ROLES:
        await msg.reply("❌ Invalid chat_id or role.")
        return
    repo.add_channel(chat_id, role, added_by=msg.from_user.id)
    await msg.reply(f"✅ Channel <code>{chat_id}</code> registered as <b>{role}</b>.", parse_mode="HTML")


@router.message(Command("removechannel"))
async def cmd_removechannel(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    chat_id = parse_channel_id(parts[1]) if len(parts) > 1 else None
    if not chat_id:
        await msg.reply("Usage: <code>/removechannel &lt;chat_id&gt;</code>", parse_mode="HTML")
        return
    repo.remove_channel(chat_id)
    await msg.reply(f"✅ Removed <code>{chat_id}</code>.", parse_mode="HTML")


@router.message(Command("listchannels"))
async def cmd_listchannels(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    rows = repo.list_all_channels()
    if not rows:
        await msg.reply("No channels registered.")
        return
    lines = ["<b>📡 Channels</b>"]
    for r in rows:
        flags = []
        if r.get("also_post"): flags.append("also-main")
        if r.get("also_backup"): flags.append("also-backup")
        if r.get("also_fsub"): flags.append("also-fsub")
        flags_s = f"  [{','.join(flags)}]" if flags else ""
        title = esc(r.get("title") or "")
        lines.append(f"• <code>{r['chat_id']}</code> — <b>{r['role']}</b>{flags_s}  {title}")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("setlog"))
async def cmd_setlog(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    chat_id = parse_channel_id(parts[1]) if len(parts) > 1 else None
    if not chat_id:
        await msg.reply("Usage: <code>/setlog &lt;chat_id&gt;</code>", parse_mode="HTML")
        return
    repo.add_channel(chat_id, "log", added_by=msg.from_user.id)
    await msg.reply(f"✅ Log channel set to <code>{chat_id}</code>.", parse_mode="HTML")


# ============================================================
# Posting: /postcaption, /filecaption, /pauseposting, /resumeposting,
# /repost, /mpost, /deletepost
# ============================================================
def _rest_of(msg: Message) -> str:
    parts = (msg.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@router.message(Command("setcaption"))
async def cmd_setcaption(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    tmpl = _rest_of(msg)
    if not tmpl:
        cur = repo.get_setting("caption_template") or "(none)"
        await msg.reply(f"Current template:\n<pre>{esc(cur)}</pre>", parse_mode="HTML")
        return
    repo.set_setting("caption_template", tmpl)
    await msg.reply("✅ Caption template saved.")


@router.message(Command("postcaption"))
async def cmd_postcaption(msg: Message) -> None:
    """Text appended BELOW every cover-post caption on the main channel."""
    if await _reject_non_admin(msg):
        return
    text = _rest_of(msg)
    if text.lower() in ("off", "none", "clear"):
        repo.set_setting("postcaption_extra", None)
        await msg.reply("✅ Post caption extra cleared.")
        return
    if not text:
        cur = repo.get_setting("postcaption_extra") or "(none)"
        await msg.reply(f"Current post-caption extra:\n<pre>{esc(cur)}</pre>\n"
                        f"Set with <code>/postcaption &lt;text&gt;</code> or "
                        f"<code>/postcaption off</code>.", parse_mode="HTML")
        return
    repo.set_setting("postcaption_extra", text)
    await msg.reply("✅ Post caption extra saved (added below cover posts).")


@router.message(Command("filecaption"))
async def cmd_filecaption(msg: Message) -> None:
    """Text appended BELOW every PDF DM caption."""
    if await _reject_non_admin(msg):
        return
    text = _rest_of(msg)
    if text.lower() in ("off", "none", "clear"):
        repo.set_setting("filecaption_extra", None)
        await msg.reply("✅ File caption extra cleared.")
        return
    if not text:
        cur = repo.get_setting("filecaption_extra") or "(none)"
        await msg.reply(f"Current file-caption extra:\n<pre>{esc(cur)}</pre>\n"
                        f"Set with <code>/filecaption &lt;text&gt;</code> or "
                        f"<code>/filecaption off</code>.", parse_mode="HTML")
        return
    repo.set_setting("filecaption_extra", text)
    await msg.reply("✅ File caption extra saved (added below delivered PDFs).")


@router.message(Command("pauseposting"))
async def cmd_pauseposting(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_setting("posting_paused", "1")
    await msg.reply("⏸ Posting paused. Use /resumeposting to resume.")


@router.message(Command("resumeposting"))
async def cmd_resumeposting(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_setting("posting_paused", "0")
    await msg.reply("▶️ Posting resumed.")


@router.message(Command("repost"))
async def cmd_repost(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    arg = _rest_of(msg)
    if not arg:
        await msg.reply("Usage: <code>/repost &lt;code|#N&gt;</code>", parse_mode="HTML")
        return
    cover = None
    if arg.startswith("#"):
        n = to_int(arg[1:])
        if n:
            cover = repo.get_post_by_number(n)
    else:
        cover = repo.get_post_by_code(arg)
    if not cover or cover.get("kind") != "cover":
        await msg.reply("❌ Cover not found.")
        return
    # Un-mark and republish
    repo.unpublish(int(cover["id"]))
    await posting.publish_cover_to_mains(bot, cover)
    await msg.reply(f"♻️ Re-posted <b>#{cover.get('post_number')}</b>.", parse_mode="HTML")


@router.message(Command("mpost"))
async def cmd_mpost(msg: Message, bot: Bot) -> None:
    """Publish arbitrary t.me links to Main channels (bulk copy)."""
    if await _reject_non_admin(msg):
        return
    text = _rest_of(msg)
    if not text:
        await msg.reply("Usage: <code>/mpost https://t.me/c/&lt;cid&gt;/&lt;mid&gt; …</code>",
                        parse_mode="HTML")
        return
    mains = repo.get_main_channels()
    if not mains:
        await msg.reply("❌ No Main channels configured. Use /addchannel first.")
        return
    posted = 0
    for token in text.split():
        parsed = parse_tme_link(token)
        if not parsed:
            continue
        chat_id, _uname, mid = parsed
        if chat_id is None:
            continue
        for m in mains:
            try:
                await tg.copy_message(bot, m["chat_id"], chat_id, mid,
                                      protect_content=repo.get_setting_bool("protect_content"))
                posted += 1
            except Exception:
                log.exception("mpost failed for %s -> %s", token, m["chat_id"])
    await msg.reply(f"✅ Published {posted} copy operation(s).")


@router.message(Command("deletepost"))
async def cmd_deletepost(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    arg = _rest_of(msg)
    if not arg:
        await msg.reply("Usage: <code>/deletepost &lt;code|#N&gt;</code>", parse_mode="HTML")
        return
    cover = None
    if arg.startswith("#"):
        n = to_int(arg[1:])
        if n:
            cover = repo.get_post_by_number(n)
    else:
        cover = repo.get_post_by_code(arg)
    if not cover:
        await msg.reply("❌ Not found.")
        return
    db.execute("UPDATE posts SET is_deleted=1 WHERE id=?", (int(cover["id"]),))
    await msg.reply(f"🗑 Marked deleted: <b>#{cover.get('post_number')}</b>", parse_mode="HTML")


# ============================================================
# Queue & drip: /queue, /queueinfo, /setschedule, /scheduleoff, /dripnow, /setcursor
# ============================================================
def _queue_lines(n: int = 10) -> List[str]:
    rows = repo.next_queued_covers(limit=n)
    if not rows:
        return ["(queue is empty)"]
    out = []
    for r in rows:
        title = (r.get("caption") or "").splitlines()[0].strip() if r.get("caption") else "(untitled)"
        out.append(f"#{r['post_number']} · {esc(truncate(title, 60))}")
    return out


@router.message(Command("queue"))
async def cmd_queue(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    lines = ["<b>📦 Queue (next 10)</b>"] + _queue_lines(10)
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("queueinfo"))
async def cmd_queueinfo(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    total = repo.total_covers()
    pending = repo.queued_covers_count()
    published = repo.published_covers_count()
    nextc = repo.next_queued_cover()
    next_line = f"posting resumes at #{nextc['post_number']}" if nextc else "queue empty"
    header = (f"<b>📦 Queue</b> — <b>{pending}</b> pending · "
              f"{published} published · {total} total ({next_line})")
    sched_cfg = sched.get_schedule()
    if sched_cfg:
        slots = ", ".join(f"{s['time']}×{s['batch']}" for s in sched_cfg.get("slots", []))
        sched_line = f"⏱ Schedule (IST): {slots}"
    else:
        sched_line = "⏱ Schedule: not set (use /setschedule 07:00,19:00 15)"
    paused = "⏸ paused" if repo.get_setting_bool("posting_paused") else "▶️ live"
    protect = "🛡 protect ON" if repo.get_setting_bool("protect_content") else "🛡 protect off"
    body = _queue_lines(10)
    text = "\n".join([header, sched_line, f"{paused} · {protect}", "", "<b>Next 10:</b>"] + body)
    await msg.reply(text, parse_mode="HTML")


@router.message(Command("setschedule"))
async def cmd_setschedule(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    args = _rest_of(msg)
    if not args:
        await msg.reply("Usage: <code>/setschedule 07:00,19:00 15</code>\n"
                        "(times in <b>IST</b>; N = posts per slot)", parse_mode="HTML")
        return
    cfg2 = sched.parse_setschedule(args)
    if not cfg2:
        await msg.reply("❌ Invalid format. Use HH:MM times (24h), e.g. "
                        "<code>/setschedule 07:00,19:00 15</code>", parse_mode="HTML")
        return
    sched.set_schedule(cfg2)
    await msg.reply(sched.format_setschedule_reply(cfg2))


@router.message(Command("scheduleoff"))
async def cmd_scheduleoff(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    sched.clear_schedule()
    await msg.reply("✅ Schedule cleared.")


@router.message(Command("dripnow"))
async def cmd_dripnow(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    n = to_int(parts[1]) if len(parts) > 1 else 1
    n = max(1, int(n or 1))
    published = await posting.publish_batch(bot, n)
    if not published:
        await msg.reply("⚠️ Nothing to publish (queue empty or posting paused).")
        return
    nums = ", ".join(f"#{p.get('post_number')}" for p in published)
    remaining = repo.queued_covers_count()
    nxt = repo.next_queued_cover()
    tail = f"\nNext up: #{nxt['post_number']}" if nxt else "\nQueue now empty."
    await msg.reply(f"🚀 Published {len(published)} post(s): {nums}\n"
                    f"📦 Remaining in queue: {remaining}{tail}")


@router.message(Command("setcursor"))
async def cmd_setcursor(msg: Message) -> None:
    """/setcursor <db_chat_id> <t.me link>

    Sets the DB-channel cursor so publishing resumes FROM the linked post
    (inclusive). If the link points at a PDF, we rewind to the nearest
    cover above it. Optional third arg = main-chat-id (for per-main cursor).
    """
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/setcursor &lt;db_chat_id&gt; &lt;t.me/c/... link&gt; "
                        "[main_chat_id]</code>", parse_mode="HTML")
        return
    db_chat = parse_channel_id(parts[1])
    parsed = parse_tme_link(parts[2])
    main_chat = parse_channel_id(parts[3]) if len(parts) > 3 else None
    if not db_chat or not parsed:
        await msg.reply("❌ Invalid channel id or link.")
        return
    link_cid, _uname, mid = parsed
    if link_cid is not None and link_cid != db_chat:
        await msg.reply(f"❌ Link belongs to <code>{link_cid}</code>, not <code>{db_chat}</code>.",
                        parse_mode="HTML")
        return
    ch = repo.get_channel(db_chat)
    if not ch or ch.get("role") != "database":
        await msg.reply("❌ That channel is not registered as a <b>database</b> channel. "
                        "Use /addchannel first.", parse_mode="HTML")
        return
    result = await sync_svc.set_cursor_from_link(db_chat, mid, main_chat)
    scope = f" (for main {main_chat})" if main_chat else ""
    await msg.reply(
        f"✅ Cursor set on <code>{db_chat}</code>{scope}\n"
        f"Next post captured: <b>message {result['next']}</b>\n"
        f"({result['note']})",
        parse_mode="HTML")


# ============================================================
# Content controls: /protect, /spoiler, /autodelete
# ============================================================
@router.message(Command("protect"))
async def cmd_protect(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or parts[1] not in ("0", "1"):
        state = "ON" if repo.get_setting_bool("protect_content") else "OFF"
        await msg.reply(f"Copy-protection is currently <b>{state}</b>.\n"
                        f"Usage: <code>/protect 1</code> to enable, <code>/protect 0</code> to disable.",
                        parse_mode="HTML")
        return
    repo.set_setting("protect_content", parts[1])
    state = "ON" if parts[1] == "1" else "OFF"
    await msg.reply(f"🛡 Copy-protection is now <b>{state}</b> "
                    f"(applies to all Main-channel posts and DM deliveries).",
                    parse_mode="HTML")


@router.message(Command("spoiler"))
async def cmd_spoiler(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or parts[1] not in ("0", "1"):
        await msg.reply("Usage: <code>/spoiler 1</code> or <code>/spoiler 0</code>", parse_mode="HTML")
        return
    repo.set_setting("spoiler", parts[1])
    await msg.reply(f"✅ Spoiler set to <b>{parts[1]}</b>.", parse_mode="HTML")


@router.message(Command("autodelete"))
async def cmd_autodelete(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        cur = repo.get_setting("autodelete_seconds") or "off"
        await msg.reply(f"Auto-delete: <b>{cur}</b>\n"
                        f"Usage: <code>/autodelete 60</code> or <code>/autodelete off</code>",
                        parse_mode="HTML")
        return
    v = parts[1].lower()
    if v == "off":
        repo.set_setting("autodelete_seconds", None)
        await msg.reply("✅ Auto-delete disabled.")
        return
    n = to_int(v)
    if not n or n <= 0:
        await msg.reply("❌ Give a positive number of seconds or 'off'.")
        return
    repo.set_setting("autodelete_seconds", str(n))
    await msg.reply(f"✅ Auto-delete set to {n}s.")


# ============================================================
# Moderation: /ban /unban /banlist
# ============================================================
@router.message(Command("ban"))
async def cmd_ban(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=2)
    uid = to_int(parts[1]) if len(parts) > 1 else None
    reason = parts[2] if len(parts) > 2 else None
    if not uid:
        await msg.reply("Usage: <code>/ban &lt;user_id&gt; [reason]</code>", parse_mode="HTML")
        return
    users.set_ban(uid, True, reason)
    await msg.reply(f"🚫 Banned <code>{uid}</code>", parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    uid = to_int(parts[1]) if len(parts) > 1 else None
    if not uid:
        await msg.reply("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    users.set_ban(uid, False, None)
    await msg.reply(f"✅ Unbanned <code>{uid}</code>", parse_mode="HTML")


@router.message(Command("banlist"))
async def cmd_banlist(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    rows = users.list_banned()
    if not rows:
        await msg.reply("No bans.")
        return
    lines = ["<b>🚫 Banned</b>"]
    for r in rows:
        lines.append(f"• <code>{r['telegram_user_id']}</code>  {esc(r.get('ban_reason') or '')}")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    users_n = users.user_count()
    banned_n = users.banned_count()
    covers = repo.total_covers()
    pending = repo.queued_covers_count()
    dbs = len(repo.get_database_channels())
    mains = len(repo.get_main_channels())
    await msg.reply(
        f"📊 <b>Stats</b>\n"
        f"👥 users: {users_n}  🚫 banned: {banned_n}\n"
        f"📦 covers: {covers}  · pending: {pending}\n"
        f"📡 db-channels: {dbs}  · main-channels: {mains}",
        parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    text = _rest_of(msg)
    if not text:
        await msg.reply("Usage: <code>/broadcast &lt;text&gt;</code>", parse_mode="HTML")
        return
    rows = db.query_all("SELECT telegram_user_id FROM users WHERE is_banned=0")
    sent = 0
    for r in rows:
        try:
            await tg.send_message(bot, r["telegram_user_id"], text)
            sent += 1
        except Exception:
            pass
    await msg.reply(f"📢 Broadcast delivered to {sent} user(s).")


# ============================================================
# setMyCommands scope by role
# ============================================================
USER_MENU = [
    BotCommand(command="start", description="Welcome / redeem file"),
    BotCommand(command="help", description="Show help"),
    BotCommand(command="whoami", description="Your id and role"),
    BotCommand(command="favs", description="Saved files"),
    BotCommand(command="rfavs", description="Remove favorite(s)"),
    BotCommand(command="mystats", description="Your stats"),
    BotCommand(command="streak", description="Daily streak"),
    BotCommand(command="random", description="Random post"),
    BotCommand(command="recent", description="Recent posts"),
]

ADMIN_MENU = USER_MENU + [
    BotCommand(command="queueinfo", description="Queue overview"),
    BotCommand(command="dripnow", description="Post next N covers now"),
    BotCommand(command="setschedule", description="Set IST slot schedule"),
    BotCommand(command="setcursor", description="Set cursor: <chan> <link>"),
    BotCommand(command="addchannel", description="Register a channel"),
    BotCommand(command="listchannels", description="List channels"),
    BotCommand(command="postcaption", description="Extra text below covers"),
    BotCommand(command="filecaption", description="Extra text below PDFs"),
    BotCommand(command="protect", description="Copy-protection 1/0"),
    BotCommand(command="pauseposting", description="Pause posting"),
    BotCommand(command="resumeposting", description="Resume posting"),
    BotCommand(command="stats", description="Bot stats"),
    BotCommand(command="broadcast", description="Broadcast a message"),
]


async def register_menu_commands(bot: Bot) -> None:
    """Push per-scope command menus to Telegram."""
    try:
        await tg.set_my_commands(bot, USER_MENU, scope=BotCommandScopeAllPrivateChats())
    except Exception:
        log.exception("failed to set user menu")
    # Push admin menu into each admin's private chat
    try:
        for a in users.list_admins():
            uid = int(a["telegram_user_id"])
            try:
                await tg.set_my_commands(bot, ADMIN_MENU, scope=BotCommandScopeChat(chat_id=uid))
            except Exception:
                pass
    except Exception:
        pass
