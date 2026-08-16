"""All bot commands (user + admin + super-admin). Full feature parity."""
from __future__ import annotations

import base64
import json

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from .. import db
from ..config import settings
from ..services import backfill, fsub, posting, repo, sync, users
from ..services.tg import answer_callback, send_message, set_my_commands
from ..utils import now_iso, parse_tme_link, to_int

router = Router(name="commands")


# ---------- permission guards ----------
async def _is_admin(m: Message) -> bool:
    return users.is_admin(m.from_user.id)


async def _is_super(m: Message) -> bool:
    return users.is_super_admin(m.from_user.id)


async def _require_admin(m: Message) -> bool:
    if await _is_admin(m):
        return True
    await m.answer("🚫 Admin only.")
    return False


async def _require_super(m: Message) -> bool:
    if await _is_super(m):
        return True
    await m.answer("🚫 Super-admin only.")
    return False


# =============================================================================
# USER COMMANDS
# =============================================================================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    uid = message.from_user.id
    # Bootstrap super-admin on first use.
    if not users.list_admins():
        forced = settings.super_admin_id
        target = forced if forced and forced == uid else uid
        users.add_admin(target, message.from_user.username,
                        message.from_user.first_name, True, target)

    payload = (command.args or "").strip()

    # Referral deep-link: /start ref_<b64>
    if payload.startswith("ref_"):
        try:
            referrer = int(base64.urlsafe_b64decode(payload[4:] + "==").decode())
            if referrer != uid:
                db.execute("INSERT INTO referrals (referrer_id, referee_id) VALUES (?,?) "
                           "ON CONFLICT DO NOTHING", (referrer, uid))
                db.execute("INSERT INTO referral_bonuses (user_id, bonus_files_remaining) VALUES (?,5) "
                           "ON CONFLICT(user_id) DO UPDATE SET bonus_files_remaining=bonus_files_remaining+5",
                           (referrer,))
                users.log_activity(uid, "referral_join", {"referrer": referrer})
        except Exception:
            pass

    # Force-sub gate before file delivery.
    if payload:
        unmet = await fsub.unmet_forcesubs(uid)
        if unmet:
            uname = await posting.get_bot_username()
            kb = fsub.build_join_keyboard(unmet, payload, uname)
            await message.answer("🔐 Join the channel(s) below, then tap Try Again.",
                                 reply_markup=kb)
            return

        post = repo.get_post_by_code(payload)
        if post:
            try:
                await posting.deliver_file_to_user(uid, post)
                users.bump_streak(uid)
                users.log_activity(uid, "fetch_by_code", {"code": payload})
                return
            except Exception as exc:
                await message.answer(f"⚠️ Could not deliver: {exc}")
                return
        await message.answer("🔍 That file could not be found.")

    await message.answer(f"👋 Hello <b>{message.from_user.first_name or 'there'}</b>!\n\n"
                         "Send /help to see available commands.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "👤 <b>User</b>\n"
        "/start /help /whoami /favs /rfavs &lt;n&gt; /random /recent /trending "
        "/similar &lt;#tag&gt; /mystats /streak /referral /notify &lt;#tag&gt; "
        "/unnotify &lt;#tag|all&gt; /leaderboard\n\n"
        "🛡️ <b>Admin</b>\n"
        "/addchannel /removechannel /listchannels /alsopost /alsofsub /alsobackup "
        "/cursor /setcursor /queue /publish /dpost /mpost /postlater /postlaterlist "
        "/postlatercancel /schedule /schedulelist /schedulecancel /backfill "
        "/backfillcancel /backup /backup10 /scandatabase /resetbackup /undoresetbackup "
        "/addbackup /removebackup /listbackup /deletepost /deletebycode /undelete "
        "/deleted /autodelete /cmdautodelete /pauseposting /resumeposting "
        "/pauseschedule /resumeschedule /pausebackup /resumebackup /setdrip "
        "/protectcontent /spoilermedia /captiontemplate /postcaptionextra "
        "/filecaptionextra /stats /users /ban /unban /warn /warns /unwarn "
        "/broadcast /broadcastlater /broadcastlist /broadcastcancel /banned /unbanall "
        "/audit /activity /health /favsall /favsrecent /whosaved /topfavs /linkweb\n\n"
        "👑 <b>Super</b>\n/addadmin /removeadmin /listadmins /genimporttoken /setweburl /setmenu"
    )
    await message.answer(text)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    uid = message.from_user.id
    role = "super-admin" if await _is_super(message) else ("admin" if await _is_admin(message) else "user")
    await message.answer(f"ID: <code>{uid}</code>\nRole: <b>{role}</b>")


# ---- discovery
@router.message(Command("random"))
async def cmd_random(message: Message):
    post = db.query_one("SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 "
                        "ORDER BY RANDOM() LIMIT 1")
    if not post:
        await message.answer("❌ No published posts yet.")
        return
    await posting.deliver_file_to_user(message.from_user.id, post)


@router.message(Command("recent"))
async def cmd_recent(message: Message):
    posts = db.query_all("SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 "
                         "ORDER BY id DESC LIMIT 10")
    if not posts:
        await message.answer("❌ No posts yet.")
        return
    lines = "\n".join(f"#{p['position']} <code>{p['code']}</code> — {(p['caption'] or '')[:40]}"
                      for p in posts)
    await message.answer(f"🕘 Recent:\n{lines}")


@router.message(Command("trending"))
async def cmd_trending(message: Message):
    posts = db.query_all(
        "SELECT p.*, (SELECT COUNT(*) FROM activity_log a WHERE a.action='fetch_by_code' "
        "AND a.details LIKE '%'||p.code||'%') c FROM posts p WHERE p.posted_at IS NOT NULL "
        "ORDER BY c DESC LIMIT 10"
    )
    if not posts:
        await message.answer("❌ No data.")
        return
    lines = "\n".join(f"{p['position']}. {p['c'] or 0} fetches — {(p['caption'] or '')[:30]}"
                      for p in posts)
    await message.answer(f"🔥 Trending:\n{lines}")


@router.message(Command("similar"))
async def cmd_similar(message: Message, command: CommandObject):
    tag = (command.args or "").strip().lstrip("#")
    if not tag:
        await message.answer("Usage: <code>/similar #tag</code>")
        return
    posts = db.query_all("SELECT * FROM posts WHERE posted_at IS NOT NULL AND caption LIKE ? "
                         "ORDER BY id DESC LIMIT 10", (f"%#{tag}%",))
    if not posts:
        await message.answer(f"❌ Nothing matching #{tag}.")
        return
    lines = "\n".join(f"#{p['position']} <code>{p['code']}</code>" for p in posts)
    await message.answer(f"🔎 #{tag}:\n{lines}")


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    rows = db.query_all("SELECT telegram_user_id, username, files_fetched FROM users "
                        "ORDER BY files_fetched DESC LIMIT 10")
    if not rows:
        await message.answer("❌ No users.")
        return
    lines = "\n".join(f"{i+1}. {r['username'] or r['telegram_user_id']} — {r['files_fetched']}"
                      for i, r in enumerate(rows))
    await message.answer(f"🏆 Leaderboard:\n{lines}")


# ---- favorites
@router.message(Command("favs"))
async def cmd_favs(message: Message):
    posts = users.list_favorites(message.from_user.id)
    if not posts:
        await message.answer("💔 No favorites.")
        return
    lines = "\n".join(f"{i+1}. <code>{p['code']}</code> — {(p['caption'] or '')[:30]}"
                      for i, p in enumerate(posts))
    await message.answer(f"❤️ Favorites:\n{lines}")


@router.message(Command("rfavs"))
async def cmd_rfavs(message: Message, command: CommandObject):
    idx = to_int((command.args or "").strip()) - 1
    posts = users.list_favorites(message.from_user.id)
    if idx < 0 or idx >= len(posts):
        await message.answer("Usage: <code>/rfavs <number from /favs></code>")
        return
    users.remove_favorite(message.from_user.id, posts[idx]["id"])
    await message.answer("🗑 Removed.")


# ---- stats / streak / referral / notify
@router.message(Command("mystats"))
async def cmd_mystats(message: Message):
    uid = message.from_user.id
    u = db.query_one("SELECT * FROM users WHERE telegram_user_id=?", (uid,))
    favs = db.query_scalar("SELECT COUNT(*) FROM favorites WHERE user_id=?", (uid,))
    refs = db.query_scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,))
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id=?",
                            (uid,)) or 0
    s = users.get_streak(uid)
    await message.answer(f"📊 <b>Your stats</b>\nFetched: <b>{u['files_fetched'] if u else 0}</b>\n"
                         f"Favorites: <b>{favs or 0}</b>\nReferrals: <b>{refs or 0}</b>\n"
                         f"Bonus files: <b>{bonus}</b>\nStreak: <b>{s['current']}</b>")


@router.message(Command("streak"))
async def cmd_streak(message: Message):
    s = users.get_streak(message.from_user.id)
    await message.answer(f"🔥 Streak: <b>{s['current']}</b> day(s)\n🏆 Longest: <b>{s['longest']}</b>")


@router.message(Command("referral"))
async def cmd_referral(message: Message):
    uid = message.from_user.id
    code = "ref_" + base64.urlsafe_b64encode(str(uid).encode()).rstrip(b"=").decode()
    uname = await posting.get_bot_username()
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id=?",
                            (uid,)) or 0
    refs = db.query_scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) or 0
    await message.answer(f"🔗 Invite link:\n<code>https://t.me/{uname}?start={code}</code>\n\n"
                         f"Referrals: <b>{refs}</b>\nBonus files: <b>{bonus}</b>")


@router.message(Command("notify"))
async def cmd_notify(message: Message, command: CommandObject):
    tag = (command.args or "").strip().lstrip("#")
    if not tag:
        subs = db.query_all("SELECT tag FROM tag_subscriptions WHERE user_id=?",
                            (message.from_user.id,))
        if not subs:
            await message.answer("You have no tag subscriptions.")
            return
        await message.answer("Subscribed: " + " ".join(f"#{s['tag']}" for s in subs))
        return
    db.execute("INSERT INTO tag_subscriptions (user_id, tag) VALUES (?,?) ON CONFLICT DO NOTHING",
               (message.from_user.id, tag))
    await message.answer(f"🔔 Subscribed to #{tag}.")


@router.message(Command("unnotify"))
async def cmd_unnotify(message: Message, command: CommandObject):
    tag = (command.args or "").strip().lstrip("#")
    if tag == "all":
        db.execute("DELETE FROM tag_subscriptions WHERE user_id=?", (message.from_user.id,))
        await message.answer("Removed all subscriptions.")
        return
    if tag:
        db.execute("DELETE FROM tag_subscriptions WHERE user_id=? AND tag=?",
                   (message.from_user.id, tag))
        await message.answer(f"Unsubscribed from #{tag}.")
    else:
        await message.answer("Usage: <code>/unnotify #tag</code> or <code>/unnotify all</code>")


# =============================================================================
# ADMIN — channels
# =============================================================================

@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Usage: <code>/addchannel &lt;chat_id&gt; &lt;role&gt;</code>\n"
                             f"Roles: {'|'.join(repo.CHANNEL_ROLES)}")
        return
    chat_id, role = parts
    if role not in repo.CHANNEL_ROLES:
        await message.answer(f"Invalid role. Choose: {', '.join(repo.CHANNEL_ROLES)}")
        return
    repo.add_channel(to_int(chat_id), role, added_by=message.from_user.id)
    users.write_audit(message.from_user.id, "addchannel", chat_id, {"role": role})
    await message.answer(f"✅ Channel <code>{chat_id}</code> registered as <b>{role}</b>.")


@router.message(Command("removechannel"))
async def cmd_removechannel(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/removechannel &lt;chat_id&gt;</code>")
        return
    repo.remove_channel(chat_id)
    users.write_audit(message.from_user.id, "removechannel", str(chat_id))
    await message.answer("🗑 Channel removed.")


@router.message(Command("listchannels"))
async def cmd_listchannels(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT * FROM channels ORDER BY role, id")
    if not rows:
        await message.answer("No channels configured.")
        return
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code> — {r['role']}" for r in rows)
    await message.answer(f"📡 Channels:\n{lines}")


async def _flag_handler(message, command, field, label):
    if not await _require_admin(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(f"Usage: <code>/{label} &lt;chat_id&gt; &lt;on|off&gt;</code>")
        return
    chat_id, toggle = to_int(parts[0]), parts[1].lower()
    if toggle not in ("on", "off"):
        await message.answer("Use on|off")
        return
    repo.set_channel_flag(chat_id, field, toggle == "on")
    users.write_audit(message.from_user.id, label, str(chat_id), {"on": toggle})
    await message.answer(f"✅ {label} {toggle} for <code>{chat_id}</code>.")


@router.message(Command("alsopost"))
async def cmd_alsopost(message: Message, command: CommandObject):
    await _flag_handler(message, command, "also_post", "alsopost")


@router.message(Command("alsofsub"))
async def cmd_alsofsub(message: Message, command: CommandObject):
    await _flag_handler(message, command, "also_fsub", "alsofsub")


@router.message(Command("alsobackup"))
async def cmd_alsobackup(message: Message, command: CommandObject):
    await _flag_handler(message, command, "also_backup", "alsobackup")


# =============================================================================
# ADMIN — sync / cursor / queue / publish
# =============================================================================

@router.message(Command("cursor"))
async def cmd_cursor(message: Message):
    if not await _require_admin(message):
        return
    await message.answer(f"Current cursor: <code>{repo.get_cursor()}</code>")


@router.message(Command("setcursor"))
async def cmd_setcursor(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    val = to_int((command.args or "").strip())
    if not val:
        await message.answer("Usage: <code>/setcursor &lt;message_id&gt;</code>")
        return
    repo.set_cursor(val)
    sync._pending.clear()
    users.write_audit(message.from_user.id, "setcursor", str(val))
    await message.answer(f"✅ Cursor set to <code>{val}</code>. Sync resumes from the next message.")


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    if not await _require_admin(message):
        return
    n = repo.queued_posts_count()
    total = repo.total_posts()
    await message.answer(f"📦 Queue: <b>{n}</b> pending / <b>{total}</b> total.")


@router.message(Command("publish"))
async def cmd_publish(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code) if code else None
    if not post:
        await message.answer("Usage: <code>/publish &lt;code&gt;</code>")
        return
    n = await posting.publish_post_to_mains(post)
    await message.answer(f"✅ Published #{post['position']} to <b>{n}</b> channel(s).")


@router.message(Command("dpost", "mpost"))
async def cmd_dpost(message: Message, command: CommandObject):
    """Post a stored post (by code) to main channels right now."""
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code) if code else None
    if not post:
        await message.answer("Usage: <code>/dpost &lt;code&gt;</code> (get codes from /recent)")
        return
    n = await posting.publish_post_to_mains(post)
    await message.answer(f"✅ Posted to <b>{n}</b> channel(s).")


# ---- scheduling
def _parse_schedule(args: str) -> tuple[int, str] | None:
    """Return (epoch_ms, code) or None."""
    parts = (args or "").split()
    if len(parts) < 2:
        return None
    ms = 0
    import re
    for tok in parts:
        m = re.match(r"^(\d+)([smhd])$", tok.lower())
        if m:
            ms += int(m.group(1)) * {"s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[m.group(2)]
    code = parts[-1] if parts else ""
    if ms <= 0:
        return None
    return ms, code


@router.message(Command("postlater"))
async def cmd_postlater(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    parsed = _parse_schedule(command.args or "")
    if not parsed:
        await message.answer("Usage: <code>/postlater 5h 2m &lt;code&gt;</code>")
        return
    ms, code = parsed
    import time
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ms / 1000))
    db.execute("INSERT INTO scheduled_posts (kind, post_code, scheduled_for, created_by) "
               "VALUES ('code',?,?,?)", (code, when, message.from_user.id))
    await message.answer(f"⏳ Scheduled <code>{code}</code> for {when} UTC.")


@router.message(Command("postlaterlist"))
async def cmd_postlaterlist(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT * FROM scheduled_posts WHERE status='pending' "
                        "ORDER BY scheduled_for ASC LIMIT 20")
    if not rows:
        await message.answer("No pending scheduled posts.")
        return
    lines = "\n".join(f"• id={r['id']} {r['kind']} <code>{r['post_code'] or ''}</code> @ {r['scheduled_for']}"
                      for r in rows)
    await message.answer(f"📅 Scheduled:\n{lines}")


@router.message(Command("postlatercancel"))
async def cmd_postlatercancel(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    sid = to_int((command.args or "").strip())
    db.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='pending'", (sid,))
    await message.answer("🗑 Cancelled.")


# ---- backfill
@router.message(Command("backfill"))
async def cmd_backfill(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Usage: <code>/backfill &lt;chat_id&gt; &lt;from_pos&gt; [to_pos]</code>")
        return
    chat_id = to_int(args[0])
    from_pos = to_int(args[1])
    to_pos = to_int(args[2]) if len(args) > 2 else None
    job = backfill.start_job([chat_id], from_pos, to_pos, message.from_user.id)
    users.write_audit(message.from_user.id, "backfill", str(chat_id), {"from": from_pos, "to": to_pos})
    await message.answer(backfill.status_text(job))


@router.message(Command("backfillcancel"))
async def cmd_backfillcancel(message: Message):
    if not await _require_admin(message):
        return
    ok = backfill.cancel_job()
    await message.answer("🗑 Backfill cancelled." if ok else "No running backfill.")


# =============================================================================
# ADMIN — backups
# =============================================================================

@router.message(Command("addbackup"))
async def cmd_addbackup(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/addbackup &lt;chat_id&gt;</code>")
        return
    repo.add_channel(chat_id, "backup", added_by=message.from_user.id)
    users.write_audit(message.from_user.id, "addbackup", str(chat_id))
    await message.answer("✅ Backup channel registered.")


@router.message(Command("removebackup"))
async def cmd_removebackup(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    db.execute("DELETE FROM channels WHERE telegram_chat_id=? AND role='backup'", (chat_id,))
    db.execute("DELETE FROM backup_copies WHERE backup_chat_id=?", (chat_id,))
    users.write_audit(message.from_user.id, "removebackup", str(chat_id))
    await message.answer("🗑 Backup channel removed.")


@router.message(Command("listbackup"))
async def cmd_listbackup(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT * FROM channels WHERE role='backup'")
    if not rows:
        await message.answer("No backup channels.")
        return
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code>" for r in rows)
    await message.answer(f"💾 Backup channels:\n{lines}")


@router.message(Command("backup"))
async def cmd_backup(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/backup &lt;chat_id&gt;</code>")
        return
    pending = db.query_all(
        "SELECT p.* FROM posts p WHERE p.is_deleted=0 "
        "AND NOT EXISTS (SELECT 1 FROM backup_copies b WHERE b.post_id=p.id AND b.backup_chat_id=?) "
        "ORDER BY p.position ASC LIMIT 5",
        (chat_id,),
    )
    n = 0
    for post in pending:
        await posting.mirror_post_to_backup(post, chat_id)
        n += 1
    await message.answer(f"💾 Mirrored {n} post(s). The rest continue on backup ticks.")


@router.message(Command("backup10"))
async def cmd_backup10(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/backup10 &lt;chat_id&gt;</code>")
        return
    pending = db.query_all(
        "SELECT p.* FROM posts p WHERE p.is_deleted=0 "
        "AND NOT EXISTS (SELECT 1 FROM backup_copies b WHERE b.post_id=p.id AND b.backup_chat_id=?) "
        "ORDER BY p.position ASC LIMIT 10",
        (chat_id,),
    )
    n = 0
    for post in pending:
        await posting.mirror_post_to_backup(post, chat_id)
        n += 1
    await message.answer(f"💾 Mirrored {n} post(s).")


@router.message(Command("scandatabase"))
async def cmd_scandatabase(message: Message):
    if not await _require_admin(message):
        return
    backups = repo.get_backup_channels()
    n = 0
    for ch in backups:
        cid = int(ch["telegram_chat_id"])
        pending = db.query_all(
            "SELECT p.* FROM posts p WHERE p.is_deleted=0 "
            "AND NOT EXISTS (SELECT 1 FROM backup_copies b WHERE b.post_id=p.id AND b.backup_chat_id=?) "
            "ORDER BY p.position ASC LIMIT 5",
            (cid,),
        )
        for post in pending:
            await posting.mirror_post_to_backup(post, cid)
            n += 1
    await message.answer(f"🔁 Forwarded {n} new database posts to backup channels.")


@router.message(Command("resetbackup"))
async def cmd_resetbackup(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if chat_id:
        db.execute("DELETE FROM backup_copies WHERE backup_chat_id=?", (chat_id,))
    else:
        db.execute("DELETE FROM backup_copies")
    users.write_audit(message.from_user.id, "resetbackup", str(chat_id))
    await message.answer("♻️ Backup mirror log cleared.")


@router.message(Command("undoresetbackup"))
async def cmd_undoresetbackup(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    args = (command.args or "").split()
    chat_id = to_int(args[0]) if args else 0
    link = args[1] if len(args) > 1 else ""
    parsed = parse_tme_link(link) if link else None
    if not chat_id or not parsed:
        await message.answer("Usage: <code>/undoresetbackup &lt;chat_id&gt; &lt;post_link&gt;</code>")
        return
    db.execute("INSERT INTO backup_copies (backup_chat_id, post_id, message_id) VALUES (?,?,?) "
               "ON CONFLICT DO NOTHING", (chat_id, parsed[1], parsed[1]))
    await message.answer("✅ Marked as already mirrored.")


# =============================================================================
# ADMIN — post deletion
# =============================================================================

@router.message(Command("deletepost"))
async def cmd_deletepost(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    link = (command.args or "").strip()
    parsed = parse_tme_link(link)
    if not parsed:
        await message.answer("Usage: <code>/deletepost &lt;post_link&gt;</code>")
        return
    chat_id, msg_id = parsed
    post = repo.get_post_by_source(chat_id, msg_id) or db.query_one(
        "SELECT p.* FROM posts p JOIN post_copies pc ON pc.post_id=p.id "
        "WHERE pc.target_chat_id=? AND pc.message_id=?", (chat_id, msg_id))
    if not post:
        await message.answer("Post not found.")
        return
    _archive_deleted(post, message.from_user.id)
    await message.answer("🗑 Post archived as deleted.")


@router.message(Command("deletebycode"))
async def cmd_deletebycode(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code)
    if not post:
        await message.answer("Post not found.")
        return
    _archive_deleted(post, message.from_user.id)
    await message.answer(f"🗑 Deleted <code>{code}</code>.")


def _archive_deleted(post, by):
    db.execute(
        "INSERT INTO deleted_posts (post_id, code, caption, deleted_by, snapshot) VALUES (?,?,?,?,?)",
        (post["id"], post["code"], post.get("caption"), by, json.dumps(post, ensure_ascii=False, default=str)),
    )
    db.execute("UPDATE posts SET is_deleted=1 WHERE id=?", (post["id"],))
    users.write_audit(by, "delete_post", post["code"])


@router.message(Command("deleted"))
async def cmd_deleted(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT * FROM deleted_posts ORDER BY id DESC LIMIT 20")
    if not rows:
        await message.answer("No deleted posts.")
        return
    lines = "\n".join(f"• <code>{r['code']}</code> — {r['deleted_at']}" for r in rows)
    await message.answer(f"🗑 Deleted:\n{lines}")


@router.message(Command("undelete"))
async def cmd_undelete(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    db.execute("UPDATE posts SET is_deleted=0 WHERE code=?", (code,))
    db.execute("DELETE FROM deleted_posts WHERE code=?", (code,))
    await message.answer(f"✅ Restored <code>{code}</code>.")


# =============================================================================
# ADMIN — moderation
# =============================================================================

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").split()[0])
    if not uid:
        await message.answer("Usage: <code>/ban &lt;user_id&gt;</code>")
        return
    users.set_ban(uid, True, "admin ban")
    users.write_audit(message.from_user.id, "ban", str(uid))
    await message.answer(f"⛔ Banned <code>{uid}</code>.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/unban &lt;user_id&gt;</code>")
        return
    users.set_ban(uid, False)
    users.write_audit(message.from_user.id, "unban", str(uid))
    await message.answer(f"✅ Unbanned <code>{uid}</code>.")


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    args = (command.args or "").split(maxsplit=1)
    uid = to_int(args[0]) if args else 0
    reason = args[1] if len(args) > 1 else ""
    if not uid:
        await message.answer("Usage: <code>/warn &lt;user_id&gt; [reason]</code>")
        return
    count = users.add_warning(uid, message.from_user.id, reason)
    users.write_audit(message.from_user.id, "warn", str(uid), {"reason": reason})
    if count >= 3:
        users.set_ban(uid, True, "3 warnings")
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {count}). Auto-banned for 3 warnings.")
    else:
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {count}).")


@router.message(Command("warns"))
async def cmd_warns(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    rows = users.list_warnings(uid)
    if not rows:
        await message.answer("No warnings for this user.")
        return
    lines = "\n".join(f"• {r['reason'] or '—'} ({r['created_at']})" for r in rows)
    await message.answer(f"⚠️ Warnings for <code>{uid}</code>:\n{lines}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    users.clear_warnings(uid)
    users.write_audit(message.from_user.id, "unwarn", str(uid))
    await message.answer(f"✅ Cleared warnings for <code>{uid}</code>.")


@router.message(Command("banned"))
async def cmd_banned(message: Message):
    if not await _require_admin(message):
        return
    rows = users.list_banned()
    if not rows:
        await message.answer("No banned users.")
        return
    lines = "\n".join(f"• <code>{r['telegram_user_id']}</code> — {r.get('ban_reason') or '—'}" for r in rows)
    await message.answer(f"⛔ Banned:\n{lines}")


@router.message(Command("unbanall"))
async def cmd_unbanall(message: Message):
    if not await _require_admin(message):
        return
    n = users.unban_all()
    users.write_audit(message.from_user.id, "unbanall")
    await message.answer(f"✅ Unbanned {n} user(s).")


# =============================================================================
# ADMIN — broadcast
# =============================================================================

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/broadcast &lt;text&gt;</code>")
        return
    all_users = db.query_all("SELECT telegram_user_id FROM users WHERE is_banned=0")
    sent = 0
    for u in all_users:
        try:
            await send_message(u["telegram_user_id"], text, disable_notification=True)
            sent += 1
        except Exception:
            pass
    users.write_audit(message.from_user.id, "broadcast", None, {"sent": sent})
    await message.answer(f"📣 Broadcast sent to <b>{sent}</b> of <b>{len(all_users)}</b> users.")


@router.message(Command("broadcastlater"))
async def cmd_broadcastlater(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/broadcastlater 5h 2m &lt;text&gt;</code>")
        return
    ms = 0
    import re
    for tok in args[0].split():
        m = re.match(r"^(\d+)([smhd])$", tok.lower())
        if m:
            ms += int(m.group(1)) * {"s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[m.group(2)]
    if ms <= 0:
        await message.answer("Invalid duration.")
        return
    import time
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ms / 1000))
    db.execute("INSERT INTO broadcast_jobs (text, status, scheduled_for, created_by) VALUES (?,?,?,?)",
               (args[1], "scheduled", when, message.from_user.id))
    await message.answer(f"⏳ Broadcast scheduled for {when} UTC.")


@router.message(Command("broadcastlist"))
async def cmd_broadcastlist(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT * FROM broadcast_jobs ORDER BY id DESC LIMIT 20")
    if not rows:
        await message.answer("No broadcast jobs.")
        return
    lines = "\n".join(f"• id={r['id']} [{r['status']}] {r['text'][:40] if r['text'] else ''}" for r in rows)
    await message.answer(f"📣 Broadcasts:\n{lines}")


@router.message(Command("broadcastcancel"))
async def cmd_broadcastcancel(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    jid = to_int((command.args or "").strip())
    db.execute("UPDATE broadcast_jobs SET status='cancelled' WHERE id=?", (jid,))
    await message.answer("🗑 Broadcast cancelled.")


# =============================================================================
# ADMIN — settings / audit / stats
# =============================================================================

def _toggle_setting(key, label):
    async def handler(message: Message):
        if not await _require_admin(message):
            return
        cur = repo.get_setting_bool(key, False)
        repo.set_setting(key, not cur)
        users.write_audit(message.from_user.id, key, None, {"value": not cur})
        await message.answer(f"✅ {label} is now <b>{'ON' if not cur else 'OFF'}</b>.")
    return handler


router.message(Command("pauseposting"))(_toggle_setting("posting_paused", "Posting"))
router.message(Command("resumeposting"))(_toggle_setting("posting_paused", "Posting"))
router.message(Command("pauseschedule"))(_toggle_setting("schedule_paused", "Scheduling"))
router.message(Command("resumeschedule"))(_toggle_setting("schedule_paused", "Scheduling"))
router.message(Command("pausebackup"))(_toggle_setting("backup_paused", "Backups"))
router.message(Command("resumebackup"))(_toggle_setting("backup_paused", "Backups"))
router.message(Command("protectcontent"))(_toggle_setting("protect_content", "Protect content"))
router.message(Command("spoilermedia"))(_toggle_setting("spoiler_media", "Spoiler media"))


@router.message(Command("setdrip"))
async def cmd_setdrip(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    args = (command.args or "").split()
    minutes = to_int(args[0]) if args else 5
    batch = to_int(args[1]) if len(args) > 1 else 1
    repo.set_setting("drip_config", {"minutes": max(1, minutes), "batch": max(1, batch)})
    await message.answer(f"⏲ Drip set: every {minutes}m, batch {batch}.")


@router.message(Command("captiontemplate"))
async def cmd_captiontemplate(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    text = (command.args or "").strip()
    repo.set_setting("caption_template", text or "{caption}")
    await message.answer("✅ Caption template updated.")


@router.message(Command("postcaptionextra"))
async def cmd_postcaptionextra(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    repo.set_setting("post_caption_extra", (command.args or "").strip())
    await message.answer("✅ Post caption extra updated.")


@router.message(Command("filecaptionextra"))
async def cmd_filecaptionextra(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    repo.set_setting("file_caption_extra", (command.args or "").strip())
    await message.answer("✅ File caption extra updated.")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _require_admin(message):
        return
    posts = repo.total_posts()
    published = repo.published_posts_count()
    users_n = users.user_count()
    await message.answer(f"📊 <b>Stats</b>\nPosts: <b>{posts}</b> (published {published})\nUsers: <b>{users_n}</b>")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not await _require_admin(message):
        return
    await message.answer(f"👥 Users: <b>{users.user_count()}</b> (banned: {users.banned_count()}).")


@router.message(Command("audit"))
async def cmd_audit(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    n = to_int((command.args or "").strip()) or 20
    rows = db.query_all("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (min(n, 100),))
    if not rows:
        await message.answer("No audit entries.")
        return
    lines = "\n".join(f"• {r['admin_id']} {r['action']} {r['target'] or ''}" for r in rows)
    await message.answer(f"🧾 Audit:\n{lines}")


@router.message(Command("activity"))
async def cmd_activity(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    n = to_int((command.args or "").strip()) or 20
    rows = db.query_all("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (min(n, 100),))
    if not rows:
        await message.answer("No activity.")
        return
    lines = "\n".join(f"• {r['actor_id']} {r['action']}" for r in rows)
    await message.answer(f"📋 Activity:\n{lines}")


@router.message(Command("health"))
async def cmd_health(message: Message):
    if not await _require_admin(message):
        return
    pd = db.query_scalar("SELECT COUNT(*) FROM pending_deletions") or 0
    q = repo.queued_posts_count()
    await message.answer(f"💚 Health\nPending deletions: <b>{pd}</b>\nQueued posts: <b>{q}</b>")


# ---- favorites admin
@router.message(Command("favsall"))
async def cmd_favsall(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT user_id, COUNT(*) c FROM favorites GROUP BY user_id ORDER BY c DESC LIMIT 10")
    if not rows:
        await message.answer("No favorites.")
        return
    lines = "\n".join(f"• <code>{r['user_id']}</code> — {r['c']}" for r in rows)
    await message.answer(f"❤️ Top savers:\n{lines}")


@router.message(Command("favsrecent"))
async def cmd_favsrecent(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all("SELECT user_id, post_id, created_at FROM favorites ORDER BY created_at DESC LIMIT 20")
    if not rows:
        await message.answer("No favorites.")
        return
    lines = "\n".join(f"• <code>{r['user_id']}</code> saved post {r['post_id']}" for r in rows)
    await message.answer(f"❤️ Recent favorites:\n{lines}")


@router.message(Command("whosaved"))
async def cmd_whosaved(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code)
    if not post:
        await message.answer("Post not found.")
        return
    rows = db.query_all("SELECT user_id FROM favorites WHERE post_id=?", (post["id"],))
    lines = "\n".join(f"• <code>{r['user_id']}</code>" for r in rows)
    await message.answer(f"👥 Saved by:\n{lines}")


@router.message(Command("topfavs"))
async def cmd_topfavs(message: Message):
    if not await _require_admin(message):
        return
    rows = db.query_all(
        "SELECT p.code, COUNT(*) c FROM favorites f JOIN posts p ON p.id=f.post_id "
        "GROUP BY p.id ORDER BY c DESC LIMIT 10")
    if not rows:
        await message.answer("No favorites.")
        return
    lines = "\n".join(f"• <code>{r['code']}</code> — {r['c']} saves" for r in rows)
    await message.answer(f"❤️ Most saved:\n{lines}")


# =============================================================================
# ADMIN — web
# =============================================================================

@router.message(Command("linkweb"))
async def cmd_linkweb(message: Message):
    if not await _require_admin(message):
        return
    from ..utils import random_token
    token = random_token()
    db.execute("INSERT INTO link_tokens (token, kind, user_id) VALUES (?,?,?)",
               (token, "web_admin", message.from_user.id))
    web = repo.get_setting("web_app_url") or settings.base_webhook_url
    await message.answer(f"🔗 Admin link:\n<code>{web}/admin?token={token}</code>")


@router.message(Command("setweburl"))
async def cmd_setweburl(message: Message, command: CommandObject):
    if not await _require_super(message):
        return
    repo.set_setting("web_app_url", (command.args or "").strip())
    await message.answer("✅ Web URL set.")


@router.message(Command("setmenu"))
async def cmd_setmenu(message: Message):
    if not await _require_admin(message):
        return
    cmds = [
        ("start", "Welcome / fetch by code"), ("help", "Command index"),
        ("random", "Random post"), ("recent", "Latest posts"),
        ("trending", "Top fetched"), ("mystats", "Your stats"),
        ("streak", "Daily streak"), ("referral", "Invite link"),
        ("favs", "Saved posts"), ("leaderboard", "Top savers"),
    ]
    await set_my_commands(cmds)
    await message.answer("✅ Menu commands registered with Telegram.")


# =============================================================================
# SUPER-ADMIN
# =============================================================================

@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject):
    if not await _require_super(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/addadmin &lt;user_id&gt;</code>")
        return
    users.add_admin(uid, None, None, False, message.from_user.id)
    users.write_audit(message.from_user.id, "addadmin", str(uid))
    await message.answer(f"✅ <code>{uid}</code> is now admin.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message, command: CommandObject):
    if not await _require_super(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/removeadmin &lt;user_id&gt;</code>")
        return
    users.remove_admin(uid)
    users.write_audit(message.from_user.id, "removeadmin", str(uid))
    await message.answer(f"🗑 <code>{uid}</code> removed from admins.")


@router.message(Command("listadmins"))
async def cmd_listadmins(message: Message):
    if not await _require_admin(message):
        return
    rows = users.list_admins()
    lines = "\n".join(f"• <code>{r['telegram_user_id']}</code> {'(super)' if r['is_super_admin'] else ''}"
                      for r in rows)
    await message.answer(f"🛡️ Admins:\n{lines}")


@router.message(Command("genimporttoken"))
async def cmd_genimporttoken(message: Message):
    if not await _require_super(message):
        return
    from ..utils import random_token
    token = random_token()
    db.execute("INSERT INTO link_tokens (token, kind, user_id) VALUES (?,?,?)",
               (token, "import", message.from_user.id))
    await message.answer(f"🔑 Import token:\n<code>{token}</code>")
