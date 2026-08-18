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
    "/setlog &lt;chat_id&gt;\n"
    "/import &lt;from&gt; &lt;to&gt; [chan] \u2014 backfill via Bot API (forwards to your DM)\n"
    "/importone &lt;link&gt; \u2014 import one message by link\n"
    "<b>\U0001F4BB MTProto backfill (userbot)</b>\n"
    "/tgsetapi &lt;api_id&gt; &lt;api_hash&gt; \u2014 set MTProto creds (my.telegram.org)\n"
    "/tglogin &lt;+phone&gt; \u2014 send login code\n"
    "/tgcode &lt;code&gt; \u2014 complete login\n"
    "/tgstatus \u2014 show MTProto login state\n"
    "/backfill_start &lt;chan&gt; [from_id] \u2014 full-history backfill (background)\n"
    "/backfill_resume &lt;chan&gt; \u2014 resume from highest imported id\n"
    "/backfill_status \u2014 live progress (emoji bar)\n"
    "/backfill_stop \u2014 stop gracefully\n"
    "/backfill_reset \u2014 clear backfill state (keeps posts)\n\n"
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
    "/setcursor &lt;chat_id&gt; &lt;t.me/c/link&gt; — resume posting from that link\n"
    "/fixnumbers — re-align pending queue to channel order\n\n"
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
        "SELECT code, post_number, caption, source_message_id FROM posts WHERE kind='cover' "
        "AND is_deleted=0 ORDER BY COALESCE(post_number, 999999) ASC, source_message_id DESC LIMIT 10")
    if not rows:
        await msg.reply("No posts yet.")
        return
    uname = await _bot_username(bot)
    lines = ["<b>🕒 Recent</b>"]
    for r in rows:
        title = (r.get("caption") or "").splitlines()[0].strip() if r.get("caption") else "(untitled)"
        title = truncate(title, 60)
        link = f"https://t.me/{uname}?start=get_{r['code']}"
        num = r.get("post_number") or "…"
        lines.append(f"#{num} · <a href=\"{link}\">{esc(title)}</a>")
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
    try:
        rows = repo.list_all_channels()
    except Exception as e:
        await msg.reply(f"🛑 DB error listing channels: <code>{esc(str(e))[:200]}</code>",
                        parse_mode="HTML"); return
    if not rows:
        await msg.reply("No channels registered. Add one with "
                        "<code>/addchannel &lt;chat_id&gt; &lt;role&gt;</code>",
                        parse_mode="HTML"); return

    def _row_line(r: dict) -> str:
        flags = []
        if r.get("also_post"):   flags.append("also-main")
        if r.get("also_backup"): flags.append("also-backup")
        if r.get("also_fsub"):   flags.append("also-fsub")
        flags_s = f"  [{','.join(flags)}]" if flags else ""
        title_raw = r.get("title") or ""
        title = esc(title_raw)
        cid = r.get("chat_id")
        # Build a channel link for the emoji-embedded style the user wants
        try:
            cid_short = str(cid).replace("-100", "", 1) if str(cid).startswith("-100") else str(cid)
            link = f'https://t.me/c/{cid_short}/1'
            title_html = f'<a href="{link}">{title or "(untitled)"}</a>'
        except Exception:
            title_html = title or "(untitled)"
        role = esc(str(r.get("role") or "?"))
        return f"• <code>{esc(str(cid))}</code> — <b>{role}</b>{flags_s}  {title_html}"

    header = f"<b>📡 Channels</b> ({len(rows)})"
    lines = [header]
    for r in rows:
        try:
            lines.append(_row_line(r))
        except Exception:
            lines.append(f"• <code>{esc(str(r.get('chat_id')))}</code> — (render error)")
    text_html = "\n".join(lines)

    # First try HTML. If Telegram rejects (bad entities), fall back to plaintext.
    try:
        await msg.reply(text_html, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e1:
        plain_lines = [f"[{len(rows)} channels]"]
        for r in rows:
            plain_lines.append(f"- {r.get('chat_id')}  {r.get('role')}  {r.get('title') or ''}")
        try:
            await msg.reply("\n".join(plain_lines))
        except Exception as e2:
            await msg.reply(f"🛑 render error: {str(e1)[:120]} / {str(e2)[:80]}")


@router.message(Command("listchannels_raw"))
async def cmd_listchannels_raw(msg: Message) -> None:
    """Plain-text channel dump — always works even if HTML formatting breaks."""
    if await _reject_non_admin(msg):
        return
    try:
        rows = repo.list_all_channels()
    except Exception as e:
        await msg.reply(f"DB error: {str(e)[:200]}"); return
    if not rows:
        await msg.reply("(no channels)"); return
    lines = [f"CHANNELS ({len(rows)}):"]
    for r in rows:
        lines.append(f"  {r.get('chat_id')}  role={r.get('role')}  "
                     f"also_post={r.get('also_post')} also_backup={r.get('also_backup')} "
                     f"also_fsub={r.get('also_fsub')}  title={r.get('title') or '-'}")
    await msg.reply("\n".join(lines))


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



def _parse_repost_tokens(text: str):
    """Accepts: #7  #7 #12  #3-#5 (or 3-5)  abc123  comma/space separated."""
    numbers, codes, bad = [], [], []
    import re as _re
    for tok in [t for t in _re.split(r"[\s,]+", (text or "").strip()) if t]:
        raw = tok.strip().lstrip("#")
        if not raw:
            continue
        if "-" in raw:
            a2, _, b2 = raw.partition("-")
            try:
                lo, hi = int(a2.lstrip("#")), int(b2.lstrip("#"))
                if lo > hi:
                    lo, hi = hi, lo
                if hi - lo > 500:
                    bad.append(f"{tok} (range>500)")
                    continue
                numbers.extend(range(lo, hi + 1))
            except Exception:
                bad.append(tok)
            continue
        try:
            numbers.append(int(raw))
        except Exception:
            if raw.isalnum():
                codes.append(raw)
            else:
                bad.append(tok)
    seen, seen2 = set(), set()
    numbers = [n for n in numbers if not (n in seen or seen.add(n))]
    codes = [c for c in codes if not (c in seen2 or seen2.add(c))]
    return numbers, codes, bad


@router.message(Command("repost"))
async def cmd_repost(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    arg = _rest_of(msg)
    if not arg:
        await msg.reply(
            "Usage: <code>/repost #7</code> · <code>/repost #7 #12 abc123</code> · "
            "<code>/repost #7-#12</code>", parse_mode="HTML")
        return
    numbers, codes, bad = _parse_repost_tokens(arg)
    covers, not_found = [], []
    for n in numbers:
        c = repo.get_post_by_number(n)
        if c and c.get("kind") == "cover":
            covers.append(c)
        else:
            not_found.append(f"#{n}")
    for code in codes:
        c = repo.get_post_by_code(code)
        if c and c.get("kind") == "cover":
            covers.append(c)
        else:
            not_found.append(code)
    if not covers:
        await msg.reply(f"❌ None found. Missing: <code>{esc(', '.join(not_found) or '-')}</code>",
                        parse_mode="HTML")
        return
    if len(covers) > 100:
        await msg.reply(f"❌ Too many ({len(covers)}). Max 100 per /repost.")
        return
    await msg.reply(f"♻️ Re-posting <b>{len(covers)}</b> cover(s)… (rate-limited, spoiler applies)",
                    parse_mode="HTML")
    posting.LAST_PUBLISH_ERROR = ""
    reposted, failed = [], []
    for cover in covers:
        try:
            results = await posting.publish_cover_to_mains(bot, cover)
            if any(r.get("ok") for r in results):
                reposted.append(int(cover.get("post_number") or 0))
            else:
                failed.append((int(cover.get("post_number") or 0),
                               (getattr(posting, "LAST_PUBLISH_ERROR", "") or "?")[:120]))
        except Exception as e:
            failed.append((int(cover.get("post_number") or 0), f"{type(e).__name__}: {e}"[:120]))
    lines = [f"✅ Re-posted: {len(reposted)} / {len(covers)}"]
    if reposted:
        lines.append("OK: " + ", ".join(f"#{n}" for n in reposted[:20]))
    if not_found:
        lines.append(f"⚠️ Not found: <code>{esc(', '.join(not_found))}</code>")
    if bad:
        lines.append(f"⚠️ Bad tokens: <code>{esc(', '.join(bad))}</code>")
    if failed:
        lines.append("🛑 Failed: " + ", ".join(f"#{n}" for n, _ in failed[:10]))
        lines.append(f"Last error: <code>{esc(failed[-1][1])}</code>")
    await msg.reply("\n".join(lines), parse_mode="HTML")

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
    """Upcoming posts with PREDICTED numbers — exactly what /dripnow will post next."""
    rows = repo.next_queued_covers(limit=n)
    if not rows:
        return ["(queue is empty)"]
    base = repo.next_post_number()
    out = []
    for i, r in enumerate(rows):
        num = r.get("post_number") or (base + i)
        title = (r.get("caption") or "").splitlines()[0].strip() if r.get("caption") else "(untitled)"
        out.append(f"#{num} · {esc(truncate(title, 60))}")
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
    if nextc:
        npn = nextc.get("post_number") or repo.next_post_number()
        next_line = f"posting resumes at #{npn}"
    else:
        next_line = "queue empty"
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



DRIPNOW_MAX = 200
_DRIP_TASK = None


@router.message(Command("dripnow"))
async def cmd_dripnow(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    global _DRIP_TASK
    if _DRIP_TASK and not _DRIP_TASK.done():
        await msg.reply("⚠️ A /dripnow batch is already running. Use /dripstop to cancel it.")
        return
    parts = (msg.text or "").split()
    n_req = to_int(parts[1]) if len(parts) > 1 else 1
    n_req = max(1, int(n_req or 1))
    capped = min(n_req, DRIPNOW_MAX)
    if repo.get_setting_bool("posting_paused"):
        await msg.reply("⏸ Posting is paused. Run /resumeposting first.")
        return
    queue_n = repo.queued_covers_count()
    mains = repo.get_main_channels()
    if queue_n == 0:
        await msg.reply("💤 Queue is empty. Nothing pending.")
        return
    if not mains:
        await msg.reply("❌ No main channel configured. Use /addchannel <chat_id> main")
        return
    flags = []
    if repo.get_setting_bool("spoiler"):
        flags.append("🌫 spoiler")
    if repo.get_setting_bool("protect_content"):
        flags.append("🛡 protect")
    flags_s = ("  [" + "  ".join(flags) + "]") if flags else ""
    cap_note = ""
    if capped < n_req:
        cap_note = f"\n⚠️ Capped at {DRIPNOW_MAX}/call (Telegram rate limits). Re-run for the rest."
    await msg.reply(
        f"🚀 Publishing up to <b>{capped}</b> post(s) in background{flags_s}\n"
        f"({queue_n} pending → {len(mains)} main channel(s), ~28 sends/sec)\n"
        f"Use /dripstop to cancel.{cap_note}",
        parse_mode="HTML")

    async def _run_batch(chat_id: int):
        try:
            posting.CANCEL_BATCH = False
            published = await posting.publish_batch(bot, capped)
        except Exception as e:
            await tg.send_message(bot, chat_id, f"🛑 publish crashed: {esc(str(e))[:250]}")
            return
        was_cancelled = getattr(posting, "CANCEL_BATCH", False)
        if not published:
            last_err = getattr(posting, "LAST_PUBLISH_ERROR", "") or "(none captured)"
            await tg.send_message(bot, chat_id,
                f"⚠️ 0 posts published.\n🛑 last publish error: <code>{esc(last_err)[:400]}</code>")
            return
        first_num = published[0].get("post_number", "?")
        last_num = published[-1].get("post_number", "?")
        remaining = repo.queued_covers_count()
        nxt = repo.next_queued_cover()
        if nxt:
            npn2 = nxt.get("post_number") or repo.next_post_number()
            tail = f"\nNext up: #{npn2}"
        else:
            tail = "\nQueue now empty."
        status = "🛑 cancelled" if was_cancelled else "✅ done"
        await tg.send_message(bot, chat_id,
            f"{status} — published <b>{len(published)}</b> post(s) (#{first_num}..#{last_num})\n"
            f"📦 Remaining in queue: {remaining}{tail}",
            parse_mode="HTML")

    import asyncio as _a
    _DRIP_TASK = _a.create_task(_run_batch(msg.chat.id))


@router.message(Command("dripstop"))
async def cmd_dripstop(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    if _DRIP_TASK and not _DRIP_TASK.done():
        posting.CANCEL_BATCH = True
        await msg.reply("🛑 Cancel requested — batch stops after the current post; summary follows.")
    else:
        await msg.reply("💤 No /dripnow batch is running.")

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
    BotCommand(command="backfill_start", description="Start MTProto backfill"),
    BotCommand(command="backfill_status", description="Live progress"),
    BotCommand(command="backfill_check", description="Gap analysis for a channel"),
    BotCommand(command="backfill_stop", description="Stop backfill"),
    BotCommand(command="backfill_resume", description="Resume backfill"),
    BotCommand(command="tgstatus", description="MTProto login status"),
    BotCommand(command="fixnumbers", description="Re-align queue to channel order"),
    BotCommand(command="dripnow", description="Post next N covers now"),
    BotCommand(command="dripstop", description="Cancel running dripnow"),
    BotCommand(command="setschedule", description="Set IST slot schedule"),
    BotCommand(command="setcursor", description="Set cursor: <chan> <link>"),
    BotCommand(command="addchannel", description="Register a channel"),
    BotCommand(command="listchannels", description="List channels"),
    BotCommand(command="import", description="Backfill history: from to"),
    BotCommand(command="postcaption", description="Extra text below covers"),
    BotCommand(command="filecaption", description="Extra text below PDFs"),
    BotCommand(command="protect", description="Copy-protection 1/0"),
    BotCommand(command="pauseposting", description="Pause posting"),
    BotCommand(command="resumeposting", description="Resume posting"),
    BotCommand(command="stats", description="Bot stats"),
    BotCommand(command="broadcast", description="Broadcast a message"),
    BotCommand(command="debug", description="Full state dump"),
    BotCommand(command="listchannels_raw", description="Plain-text channel list"),
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


# ============================================================
# /import <from_msg_id> <to_msg_id> [db_chat_id]
# /importone <t.me link>
#
# Historical backfill via forward_message: the bot must be admin in the DB channel.
# Each message is forwarded to the admin's DM (temporarily), the forwarded copy is
# inspected to extract media, then stored as a cover or PDF. This works around the
# Bot API limitation that bots cannot fetch old channel messages directly.
# ============================================================

async def _ingest_one(bot: Bot, db_chat_id: int, msg_id: int, admin_id: int) -> Optional[dict]:
    """Forward one channel message to admin DM, classify it, store it, then delete
    the forwarded copy. Returns the ingestion result or None if skipped."""
    if repo.post_exists(db_chat_id, msg_id):
        return {"skipped": "duplicate", "msg_id": msg_id}
    try:
        forwarded = await tg.forward_message(bot, admin_id, db_chat_id, msg_id)
    except Exception as e:
        return {"error": str(e), "msg_id": msg_id}

    # Build a synthetic "channel_post"-shape object from the forwarded copy but with
    # source_chat_id / source_message_id set to the ORIGINAL values.
    class _Src:
        pass
    src = _Src()
    src.chat = type("C", (), {"id": db_chat_id})()
    src.message_id = msg_id
    src.caption = forwarded.caption
    src.text = forwarded.text
    src.document = forwarded.document
    src.photo = forwarded.photo
    src.video = forwarded.video
    src.audio = forwarded.audio

    from ..services.sync import handle_channel_post
    # Temporarily lift the cursor so ingestion is allowed
    prev_cursor = repo.get_cursor(db_chat_id)
    repo.set_cursor(db_chat_id, msg_id - 1)
    try:
        result = await handle_channel_post(src)
    finally:
        # After ingestion, restore cursor to the highest processed msg_id
        if prev_cursor > msg_id:
            repo.set_cursor(db_chat_id, prev_cursor)

    # Clean up the DM copy
    try:
        await tg.delete_message(bot, admin_id, forwarded.message_id)
    except Exception:
        pass

    return result or {"stored": True, "msg_id": msg_id}


@router.message(Command("import"))
async def cmd_import(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply(
            "Usage: <code>/import &lt;from_msg_id&gt; &lt;to_msg_id&gt; [db_chat_id]</code>\n\n"
            "Backfills channel history by forwarding each message to your DM briefly,\n"
            "classifying it, and storing it. Bot must be admin in the DB channel.\n\n"
            "Example: <code>/import 1 100</code> (uses your first database channel)",
            parse_mode="HTML")
        return
    a = to_int(parts[1]); b = to_int(parts[2])
    if not a or not b or a > b:
        await msg.reply("❌ Bad range. from ≤ to, both positive integers.")
        return
    if b - a > 500:
        await msg.reply("❌ Max 500 messages per /import call. Split into smaller ranges.")
        return

    if len(parts) > 3:
        db_chat = parse_channel_id(parts[3])
    else:
        dbs = repo.get_database_channels()
        if not dbs:
            await msg.reply("❌ No database channel registered.")
            return
        db_chat = dbs[0]["chat_id"]

    ch = repo.get_channel(db_chat)
    if not ch or ch.get("role") != "database":
        await msg.reply(f"❌ <code>{db_chat}</code> is not a database channel.", parse_mode="HTML")
        return

    await msg.reply(f"⏳ Importing messages {a}…{b} from <code>{db_chat}</code>. "
                    f"This forwards each message to you briefly.", parse_mode="HTML")

    covers = pdfs = skipped = errors = 0
    err_samples: List[str] = []
    for mid in range(a, b + 1):
        r = await _ingest_one(bot, db_chat, mid, msg.from_user.id)
        if not r:
            skipped += 1
        elif r.get("error"):
            errors += 1
            if len(err_samples) < 3:
                err_samples.append(f"#{mid}: {r['error'][:60]}")
        elif r.get("skipped"):
            skipped += 1
        elif r.get("kind") == "cover":
            covers += 1
        elif r.get("kind") == "pdf":
            pdfs += 1
        # gentle throttling to avoid Telegram flood
        import asyncio as _a
        await _a.sleep(0.15)

    total = repo.total_covers()
    queued = repo.queued_covers_count()
    text = (f"✅ Import done.\n"
            f"• Covers stored: {covers}\n"
            f"• PDFs stored: {pdfs}\n"
            f"• Skipped/dup: {skipped}\n"
            f"• Errors: {errors}\n\n"
            f"📦 Total covers now: {total}  ·  pending: {queued}")
    if err_samples:
        text += "\n\nSample errors:\n" + "\n".join(err_samples)
    await msg.reply(text)


@router.message(Command("importone"))
async def cmd_importone(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/importone &lt;t.me/c/... link&gt;</code>", parse_mode="HTML")
        return
    parsed = parse_tme_link(parts[1])
    if not parsed:
        await msg.reply("❌ Bad link.")
        return
    link_cid, _, mid = parsed
    if link_cid is None:
        await msg.reply("❌ Public-channel links not supported here (need t.me/c/... form).")
        return
    r = await _ingest_one(bot, link_cid, mid, msg.from_user.id)
    await msg.reply(f"Result: <code>{esc(str(r))}</code>", parse_mode="HTML")


@router.message(Command("fixnumbers"))
async def cmd_fixnumbers(msg: Message) -> None:
    """One-time repair: clears all existing #N and published flags, then re-runs
    numbering in TRUE channel order (source_chat_id, source_message_id) so old
    imported posts get correct sequence. Published posts keep their #N first,
    then unpublished covers are ordered by their source message ids.

    NOTE: only published covers have a permanent #N. This command does NOT
    renumber those; it only reorders the pending queue view so /queueinfo shows
    the posts in the same order they'll actually be posted."""
    if await _reject_non_super(msg):
        return
    # This command is mostly a no-op under the new publish-time numbering scheme:
    # queue ordering is already by (source_chat_id, source_message_id), and
    # numbers are assigned at publish. It exists as an admin sanity tool.
    rows = db.query_all(
        "SELECT id, source_chat_id, source_message_id FROM posts "
        "WHERE kind='cover' AND published_at IS NULL AND is_deleted=0 "
        "ORDER BY source_chat_id ASC, source_message_id ASC")
    n = len(rows)
    await msg.reply(
        f"✅ Queue re-aligned to channel order: {n} pending cover(s).\n"
        f"Numbers will be assigned 1..N as each one is published.\n"
        f"Use /queueinfo to preview the next 10.")


# ============================================================
# MTProto userbot: login + backfill controls
# ============================================================
from ..services import userbot as ub


@router.message(Command("tgsetapi"))
async def cmd_tgsetapi(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply(
            "Usage: <code>/tgsetapi &lt;api_id&gt; &lt;api_hash&gt;</code>\n"
            "Get these from https://my.telegram.org → API development tools.",
            parse_mode="HTML")
        return
    try:
        api_id = int(parts[1])
    except Exception:
        await msg.reply("❌ api_id must be a number.")
        return
    ub.set_api_creds(api_id, parts[2].strip())
    await msg.reply("✅ MTProto API credentials saved.")


@router.message(Command("tglogin"))
async def cmd_tglogin(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    if not ub.telethon_available():
        await msg.reply("❌ telethon is not installed on the server.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Usage: <code>/tglogin &lt;+1234567890&gt;</code>",
                        parse_mode="HTML")
        return
    phone = parts[1].strip()
    try:
        await ub.request_login_code(phone)
        await msg.reply(
            "📲 Code sent to your Telegram account. Reply with:\n"
            "<code>/tgcode 1 2 3 4 5</code>  (any spacing is fine)",
            parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"❌ Login failed: <code>{esc(str(e))}</code>", parse_mode="HTML")


@router.message(Command("tgcode"))
async def cmd_tgcode(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Usage: <code>/tgcode &lt;1 2 3 4 5&gt;</code>", parse_mode="HTML")
        return
    code = parts[1].strip()
    try:
        session_str = await ub.complete_login_with_code(code)
        await msg.reply(
            "✅ <b>Logged in</b>. MTProto session saved.\n"
            "You can now run /backfill_start or /backfill_resume.",
            parse_mode="HTML")
        log_s = session_str[:16] + "…" if session_str else "(empty)"
        await msg.reply(f"🔑 Session string (keep private): <code>{esc(log_s)}</code>",
                        parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"❌ Code rejected: <code>{esc(str(e))}</code>", parse_mode="HTML")


@router.message(Command("tgstatus"))
async def cmd_tgstatus(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    if not ub.telethon_available():
        await msg.reply("❌ telethon not installed.")
        return
    api_id = ub.get_api_id()
    has_sess = bool(ub.get_session_string())
    if not api_id:
        await msg.reply("❌ No API creds. Use /tgsetapi first.")
        return
    if not has_sess:
        await msg.reply("⚠️ API creds set, but no session. Use /tglogin.")
        return
    try:
        me = await ub.get_me_info()
        await msg.reply(
            f"🟢 <b>MTProto logged in</b>\n"
            f"• id: <code>{me.get('id')}</code>\n"
            f"• name: {esc(me.get('first_name',''))}\n"
            f"• username: @{esc(me.get('username','') or '—')}\n"
            f"• phone: {esc(me.get('phone','') or '—')}",
            parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"🔴 Session error: <code>{esc(str(e))}</code>\n"
                        f"Try /tglogin to re-authenticate.", parse_mode="HTML")


@router.message(Command("backfill_start"))
async def cmd_backfill_start(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backfill_start &lt;db_chat_id&gt; [from_id]</code>",
                        parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    from_id = to_int(parts[2]) if len(parts) > 2 else 1
    if not chan:
        await msg.reply("❌ Bad chat id.")
        return
    ok, txt = ub.start_backfill(bot, msg.from_user.id, chan, from_id=from_id or 1)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("backfill_resume"))
async def cmd_backfill_resume(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backfill_resume &lt;db_chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    if not chan:
        await msg.reply("❌ Bad chat id.")
        return
    ok, txt = ub.resume_backfill(bot, msg.from_user.id, chan)
    await msg.reply(txt, parse_mode="HTML")


@router.message(Command("backfill_stop"))
async def cmd_backfill_stop(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    ok, txt = ub.stop_backfill()
    await msg.reply(txt)


@router.message(Command("backfill_status"))
async def cmd_backfill_status(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await msg.reply(ub.render_status(), parse_mode="HTML")


@router.message(Command("backfill_reset"))
async def cmd_backfill_reset(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    txt = ub.reset_backfill_state()
    await msg.reply(txt)


@router.message(Command("backfill_check"))
async def cmd_backfill_check(msg: Message) -> None:
    """Gap analysis: compares DB rows against the channel's real message-id range
    to find messages the PC script missed (due to its Turso crashes)."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/backfill_check &lt;db_chat_id&gt;</code>",
                        parse_mode="HTML")
        return
    chan = parse_channel_id(parts[1])
    if not chan:
        await msg.reply("❌ Bad chat id.")
        return
    ch = repo.get_channel(chan)
    if not ch or ch.get("role") != "database":
        await msg.reply(f"❌ <code>{chan}</code> is not a database channel.", parse_mode="HTML")
        return

    mn = db.query_scalar(
        "SELECT MIN(source_message_id) FROM posts WHERE source_chat_id=?", (chan,)) or 0
    mx = db.query_scalar(
        "SELECT MAX(source_message_id) FROM posts WHERE source_chat_id=?", (chan,)) or 0
    total = db.query_scalar(
        "SELECT COUNT(*) FROM posts WHERE source_chat_id=?", (chan,)) or 0
    covers = db.query_scalar(
        "SELECT COUNT(*) FROM posts WHERE source_chat_id=? AND kind='cover'", (chan,)) or 0
    pdfs = db.query_scalar(
        "SELECT COUNT(*) FROM posts WHERE source_chat_id=? AND kind='pdf'", (chan,)) or 0

    if not mx:
        await msg.reply("Nothing imported for this channel yet.")
        return

    # Find gaps: message-ids that exist in channel but not in DB.
    # We approximate by checking which ids in [mn..mx] are missing.
    rows = db.fetch_all(
        "SELECT source_message_id FROM posts WHERE source_chat_id=? ORDER BY source_message_id",
        (chan,))
    have = {int(r[0]) for r in rows}
    missing = [i for i in range(int(mn), int(mx) + 1) if i not in have]
    # Collapse to ranges for readability
    ranges: list = []
    if missing:
        start = prev = missing[0]
        for x in missing[1:]:
            if x == prev + 1:
                prev = x
            else:
                ranges.append((start, prev))
                start = prev = x
        ranges.append((start, prev))

    range_strs = [f"{a}" if a == b else f"{a}..{b}" for a, b in ranges[:15]]
    more = f" (+{len(ranges)-15} more ranges)" if len(ranges) > 15 else ""

    await msg.reply(
        f"🔍 <b>Gap check for <code>{chan}</code></b>\n\n"
        f"• Imported: <b>{total}</b> rows ({covers} covers, {pdfs} pdfs)\n"
        f"• id range in DB: <code>{mn}</code> .. <code>{mx}</code>\n"
        f"• Missing ids in that range: <b>{len(missing)}</b>\n"
        f"• Missing ranges: <code>{', '.join(range_strs) or 'none'}</code>{more}\n\n"
        f"Note: missing ids can be deleted messages / service posts — not all gaps are real posts.\n"
        f"To fill the gaps: <code>/backfill_start {chan} {mn}</code> (full rescan, dup-safe).",
        parse_mode="HTML")


@router.message(Command("debug"))
async def cmd_debug(msg: Message) -> None:
    """Full state dump for triage."""
    if await _reject_non_admin(msg):
        return
    try:
        rows_ch = repo.list_all_channels()
    except Exception as e:
        rows_ch = []
    try:
        pending = repo.queued_covers_count()
        pub = repo.published_covers_count()
        total = repo.total_covers()
        total_all = repo.total_posts()
    except Exception:
        pending = pub = total = total_all = -1
    paused = repo.get_setting_bool("posting_paused")
    protect = repo.get_setting_bool("protect_content")
    postcap = (repo.get_setting("postcaption_extra") or "")[:60]
    filecap = (repo.get_setting("filecaption_extra") or "")[:60]
    # cursors
    try:
        cursors = repo.all_cursor_keys()
    except Exception:
        cursors = []
    dbs = [r for r in rows_ch if r.get("role") == "database"]
    mains = [r for r in rows_ch if r.get("role") == "main" or r.get("also_post")]
    backups = [r for r in rows_ch if r.get("role") == "backup" or r.get("also_backup")]
    logs = [r for r in rows_ch if r.get("role") == "log"]
    fsubs = [r for r in rows_ch if r.get("role") == "forcesub" or r.get("also_fsub")]

    lines = [
        "🔧 <b>Debug — full state</b>",
        f"• channels total: <b>{len(rows_ch)}</b>",
        f"  📥 database: {len(dbs)}  ·  📤 main: {len(mains)}  ·  💾 backup: {len(backups)}",
        f"  📝 log: {len(logs)}  ·  🔒 forcesub: {len(fsubs)}",
        "",
        f"• posts total: <b>{total_all}</b>  (covers {total} · pending {pending} · published {pub})",
        f"• posting paused: {'✅ yes' if paused else '❌ no'}",
        f"• protect_content: {'✅ on' if protect else '❌ off'}",
        f"• postcaption_extra: {esc(postcap) or '(none)'}",
        f"• filecaption_extra: {esc(filecap) or '(none)'}",
        "",
        f"• cursors ({len(cursors)}):",
    ]
    for k, v in cursors[:10]:
        lines.append(f"    - <code>{esc(k)}</code> = <code>{esc(v)}</code>")
    if len(cursors) > 10:
        lines.append(f"    ... (+{len(cursors) - 10} more)")

    # last publish error surfaced from posting.py
    last_err = getattr(posting, "LAST_PUBLISH_ERROR", "") or "(none)"
    lines.append("")
    lines.append(f"• last publish error: <code>{esc(last_err)[:200]}</code>")

    # Main-channel details for quick eyeball
    if mains:
        lines.append("")
        lines.append("<b>main channels:</b>")
        for r in mains[:5]:
            lines.append(f"  • <code>{r.get('chat_id')}</code>  "
                         f"role={esc(str(r.get('role')))}  "
                         f"also_post={r.get('also_post')}  "
                         f"title={esc(str(r.get('title') or '-'))}")
    try:
        await msg.reply("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        # HTML failure fallback
        await msg.reply(f"debug (plain): channels={len(rows_ch)} pending={pending} "
                        f"paused={paused} mains={len(mains)} last_err={last_err[:120]}")
