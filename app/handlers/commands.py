"""All bot commands (user + admin + super-admin), full feature parity."""
from __future__ import annotations

import json
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from .. import db
from ..config import settings
from ..services import repo, posting, users, sync
from ..services.tg import send_message
from ..utils import now_iso, random_token, to_int

router = Router(name="commands")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _extract_hashtags(text: str) -> set[str]:
    import re
    return set(re.findall(r"#([A-Za-z0-9_]+)", text or ""))


async def _is_super(uid: int) -> bool:
    return users.is_super_admin(uid)


async def _require_admin(message: Message) -> bool:
    if users.is_admin(message.from_user.id):
        return True
    await message.answer("🚫 Admin only.")
    return False


async def _require_super(message: Message) -> bool:
    if users.is_super_admin(message.from_user.id):
        return True
    await message.answer("🚫 Super-admin only.")
    return False


# --------------------------------------------------------------------------- #
# /start  + /help + /whoami
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandStart, state: FSMContext = None):
    uid = message.from_user.id

    # Bootstrap super-admin if no admins exist yet.
    if not users.list_admins():
        forced = settings.super_admin_id
        if forced and uid == forced:
            users.add_admin(uid, message.from_user.username, message.from_user.first_name, True, uid)
        else:
            users.add_admin(uid, message.from_user.username, message.from_user.first_name, True, uid)

    # Deep-link payload: /start <code> -> deliver that file.
    payload = (command.args or "").strip()
    if payload:
        post = repo.get_post_by_code(payload)
        if post:
            try:
                await posting.deliver_file_to_user(uid, post)
                users.bump_streak(uid)
                users.log_activity(uid, "fetch_by_code", {"code": payload})
                return
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"⚠️ Could not deliver: {exc}")
                return
        await message.answer("🔍 That file could not be found.")

    await message.answer(
        f"👋 Hello <b>{message.from_user.first_name or 'there'}</b>!\n\n"
        f"Send <code>/help</code> to see available commands."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "👤 <b>User commands</b>\n"
        "/start — welcome / fetch by code\n"
        "/help — this menu\n"
        "/whoami — your ID & role\n"
        "/favs — saved files\n"
        "/rfavs <n> — remove a favorite\n"
        "/random — random published file\n"
        "/recent — latest published files\n"
        "/trending — most fetched (7d)\n"
        "/similar <#tag> — find by tag\n"
        "/mystats — your stats\n"
        "/streak — daily streak\n"
        "/referral — invite link & bonus\n"
        "/notify <#tag> / /unnotify\n\n"
        "🛡️ <b>Admin</b>\n"
        "/addchannel /removechannel /listchannels\n"
        "/cursor /setcursor <id>\n"
        "/queue /publish <code>\n"
        "/ban /unban /warn /warns /unwarn /users\n"
        "/stats /broadcast\n"
        "/addadmin /removeadmin /listadmins (super)"
    )
    await message.answer(text)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    uid = message.from_user.id
    role = "super-admin" if users.is_super_admin(uid) else ("admin" if users.is_admin(uid) else "user")
    await message.answer(f"ID: <code>{uid}</code>\nRole: <b>{role}</b>")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

@router.message(Command("random"))
async def cmd_random(message: Message):
    post = db.query_one("SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 ORDER BY RANDOM() LIMIT 1")
    if not post:
        await message.answer("❌ No published posts yet.")
        return
    await posting.deliver_file_to_user(message.from_user.id, post)


@router.message(Command("recent"))
async def cmd_recent(message: Message):
    posts = db.query_all("SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 ORDER BY id DESC LIMIT 10")
    if not posts:
        await message.answer("❌ No posts yet.")
        return
    lines = "\n".join(f"#{p['position']} <code>{p['code']}</code> — {(p['caption'] or '')[:40]}" for p in posts)
    await message.answer(f"🕘 Recent:\n{lines}")


@router.message(Command("trending"))
async def cmd_trending(message: Message):
    posts = db.query_all(
        "SELECT p.*, COUNT(a.id) c FROM posts p LEFT JOIN activity_log a "
        "ON a.action='fetch_by_code' AND a.details LIKE '%\"code\":\"'||p.code||'\"%' "
        "WHERE p.posted_at IS NOT NULL GROUP BY p.id ORDER BY c DESC LIMIT 10"
    )
    if not posts:
        await message.answer("❌ No data.")
        return
    lines = "\n".join(f"{p['position']}. {p['c'] or 0} fetches — {(p['caption'] or '')[:30]}" for p in posts)
    await message.answer(f"🔥 Trending:\n{lines}")


@router.message(Command("similar"))
async def cmd_similar(message: Message, command: Command):
    tag = (command.args or "").strip().lstrip("#")
    if not tag:
        await message.answer("Usage: <code>/similar #tag</code>")
        return
    posts = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NOT NULL AND caption LIKE ? ORDER BY id DESC LIMIT 10",
        (f"%#{tag}%",),
    )
    if not posts:
        await message.answer(f"❌ Nothing matching #{tag}.")
        return
    lines = "\n".join(f"#{p['position']} <code>{p['code']}</code>" for p in posts)
    await message.answer(f"🔎 #{tag}:\n{lines}")


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    rows = db.query_all(
        "SELECT telegram_user_id, username, files_fetched FROM users ORDER BY files_fetched DESC LIMIT 10"
    )
    if not rows:
        await message.answer("❌ No users.")
        return
    lines = "\n".join(f"{i+1}. {r['username'] or r['telegram_user_id']} — {r['files_fetched']}" for i, r in enumerate(rows))
    await message.answer(f"🏆 Leaderboard:\n{lines}")


# --------------------------------------------------------------------------- #
# Favorites + stats + streaks + referrals + notifications
# --------------------------------------------------------------------------- #

@router.message(Command("favs"))
async def cmd_favs(message: Message):
    posts = users.list_favorites(message.from_user.id)
    if not posts:
        await message.answer("💔 No favorites.")
        return
    lines = "\n".join(f"{i+1}. <code>{p['code']}</code> — {(p['caption'] or '')[:30]}" for i, p in enumerate(posts))
    await message.answer(f"❤️ Favorites:\n{lines}")


@router.message(Command("rfavs"))
async def cmd_rfavs(message: Message, command: Command):
    idx = to_int((command.args or "").strip()) - 1
    posts = users.list_favorites(message.from_user.id)
    if idx < 0 or idx >= len(posts):
        await message.answer("Usage: <code>/rfavs <number from /favs></code>")
        return
    users.remove_favorite(message.from_user.id, posts[idx]["id"])
    await message.answer("🗑 Removed.")


@router.message(Command("mystats"))
async def cmd_mystats(message: Message):
    uid = message.from_user.id
    u = db.query_one("SELECT * FROM users WHERE telegram_user_id = ?", (uid,))
    favs = db.query_scalar("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (uid,))
    refs = db.query_scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id = ?", (uid,)) or 0
    await message.answer(
        f"📊 <b>Your stats</b>\n"
        f"Fetched: <b>{u['files_fetched'] if u else 0}</b>\n"
        f"Favorites: <b>{favs or 0}</b>\n"
        f"Referrals: <b>{refs or 0}</b>\n"
        f"Bonus files: <b>{bonus}</b>"
    )


@router.message(Command("streak"))
async def cmd_streak(message: Message):
    s = db.query_one("SELECT current, longest FROM user_streaks WHERE user_id = ?", (message.from_user.id,))
    if not s:
        await message.answer("🔥 No streak yet — fetch a file to start!")
        return
    await message.answer(f"🔥 Streak: <b>{s['current']}</b> day(s)\n🏆 Longest: <b>{s['longest']}</b>")


@router.message(Command("referral"))
async def cmd_referral(message: Message):
    import base64
    uid = message.from_user.id
    code = "ref_" + base64.urlsafe_b64encode(str(uid).encode()).rstrip(b"=").decode()
    uname = await posting.get_bot_username()
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id = ?", (uid,)) or 0
    refs = db.query_scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,)) or 0
    await message.answer(
        f"🔗 Invite link:\n<code>https://t.me/{uname}?start={code}</code>\n\n"
        f"Referrals: <b>{refs}</b>\nBonus files: <b>{bonus}</b>"
    )


@router.message(Command("notify"))
async def cmd_notify(message: Message, command: Command):
    tag = (command.args or "").strip().lstrip("#")
    if not tag:
        subs = db.query_all("SELECT tag FROM tag_subscriptions WHERE user_id = ?", (message.from_user.id,))
        if not subs:
            await message.answer("You have no tag subscriptions.")
            return
        await message.answer("Subscribed: " + " ".join(f"#{s['tag']}" for s in subs))
        return
    db.execute("INSERT INTO tag_subscriptions (user_id, tag) VALUES (?, ?) ON CONFLICT DO NOTHING",
               (message.from_user.id, tag))
    await message.answer(f"🔔 Subscribed to #{tag}.")


@router.message(Command("unnotify"))
async def cmd_unnotify(message: Message, command: Command):
    tag = (command.args or "").strip().lstrip("#")
    if tag == "all":
        db.execute("DELETE FROM tag_subscriptions WHERE user_id = ?", (message.from_user.id,))
        await message.answer("Removed all subscriptions.")
        return
    if tag:
        db.execute("DELETE FROM tag_subscriptions WHERE user_id = ? AND tag = ?", (message.from_user.id, tag))
        await message.answer(f"Unsubscribed from #{tag}.")
    else:
        await message.answer("Usage: <code>/unnotify #tag</code> or <code>/unnotify all</code>")


# --------------------------------------------------------------------------- #
# Admin — channels
# --------------------------------------------------------------------------- #

@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, command: Command):
    if not await _require_admin(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Usage: <code>/addchannel <chat_id> <role></code>\nRoles: database|main|log|backup|forcesub")
        return
    chat_id, role = parts
    if role not in repo.CHANNEL_ROLES:
        await message.answer(f"Invalid role. Choose: {', '.join(repo.CHANNEL_ROLES)}")
        return
    repo.add_channel(to_int(chat_id), role, added_by=message.from_user.id)
    await message.answer(f"✅ Channel <code>{chat_id}</code> registered as <b>{role}</b>.")


@router.message(Command("removechannel"))
async def cmd_removechannel(message: Message, command: Command):
    if not await _require_admin(message):
        return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/removechannel <chat_id></code>")
        return
    repo.remove_channel(chat_id)
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


# --------------------------------------------------------------------------- #
# Admin — sync / cursor / queue / publish
# --------------------------------------------------------------------------- #

@router.message(Command("cursor"))
async def cmd_cursor(message: Message):
    if not await _require_admin(message):
        return
    await message.answer(f"Current cursor: <code>{repo.get_cursor()}</code>")


@router.message(Command("setcursor"))
async def cmd_setcursor(message: Message, command: Command, state: FSMContext):
    if not await _require_admin(message):
        return
    val = to_int((command.args or "").strip())
    if not val:
        await message.answer("Usage: <code>/setcursor <message_id></code>")
        return
    repo.set_cursor(val)
    sync._pending.clear()
    await message.answer(f"✅ Cursor set to <code>{val}</code>. The bot will sync from the next message.")


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    if not await _require_admin(message):
        return
    n = db.query_scalar("SELECT COUNT(*) FROM posts WHERE posted_at IS NULL AND is_deleted=0")
    total = db.query_scalar("SELECT COUNT(*) FROM posts")
    await message.answer(f"📦 Queue: <b>{n}</b> pending / <b>{total}</b> total.")


@router.message(Command("publish"))
async def cmd_publish(message: Message, command: Command):
    if not await _require_admin(message):
        return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code) if code else None
    if not post:
        await message.answer("Usage: <code>/publish <code></code> — get codes from /recent or /queue.")
        return
    await message.answer(f"⏳ Publishing #{post['position']} ({post['code']})…")
    n = await posting.publish_post_to_mains(post)
    await message.answer(f"✅ Published to <b>{n}</b> channel(s).")


# --------------------------------------------------------------------------- #
# Admin — user management + moderation
# --------------------------------------------------------------------------- #

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: Command):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").split()[0])
    if not uid:
        await message.answer("Usage: <code>/ban <user_id></code>")
        return
    users.set_ban(uid, True, "admin ban")
    users.write_audit(message.from_user.id, "ban", str(uid))
    await message.answer(f"⛔ Banned <code>{uid}</code>.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: Command):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/unban <user_id></code>")
        return
    users.set_ban(uid, False)
    users.write_audit(message.from_user.id, "unban", str(uid))
    await message.answer(f"✅ Unbanned <code>{uid}</code>.")


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: Command):
    if not await _require_admin(message):
        return
    args = (command.args or "").split(maxsplit=1)
    uid = to_int(args[0]) if args else 0
    reason = args[1] if len(args) > 1 else ""
    if not uid:
        await message.answer("Usage: <code>/warn <user_id> [reason]</code>")
        return
    db.execute("INSERT INTO warnings (user_id, admin_id, reason) VALUES (?, ?, ?)", (uid, message.from_user.id, reason))
    db.execute("UPDATE users SET warn_count = warn_count + 1 WHERE telegram_user_id = ?", (uid,))
    users.write_audit(message.from_user.id, "warn", str(uid), {"reason": reason})
    w = db.query_scalar("SELECT warn_count FROM users WHERE telegram_user_id = ?", (uid,)) or 0
    if w >= 3:
        users.set_ban(uid, True, "3 warnings")
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {w}). Auto-banned for 3 warnings.")
    else:
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {w}).")


@router.message(Command("warns"))
async def cmd_warns(message: Message, command: Command):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    rows = db.query_all("SELECT * FROM warnings WHERE user_id = ? ORDER BY id DESC LIMIT 20", (uid,))
    if not rows:
        await message.answer("No warnings for this user.")
        return
    lines = "\n".join(f"• {r['reason'] or '—'} ({r['created_at']})" for r in rows)
    await message.answer(f"⚠️ Warnings for <code>{uid}</code>:\n{lines}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: Command):
    if not await _require_admin(message):
        return
    uid = to_int((command.args or "").strip())
    db.execute("DELETE FROM warnings WHERE user_id = ?", (uid,))
    db.execute("UPDATE users SET warn_count = 0 WHERE telegram_user_id = ?", (uid,))
    await message.answer(f"✅ Cleared warnings for <code>{uid}</code>.")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not await _require_admin(message):
        return
    n = db.query_scalar("SELECT COUNT(*) FROM users")
    banned = db.query_scalar("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    await message.answer(f"👥 Users: <b>{n}</b> (banned: {banned}).")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _require_admin(message):
        return
    posts = db.query_scalar("SELECT COUNT(*) FROM posts")
    published = db.query_scalar("SELECT COUNT(*) FROM posts WHERE posted_at IS NOT NULL")
    users_n = db.query_scalar("SELECT COUNT(*) FROM users")
    await message.answer(
        f"📊 <b>Stats</b>\nPosts: <b>{posts}</b> (published {published})\nUsers: <b>{users_n}</b>"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: Command, state: FSMContext):
    if not await _require_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/broadcast <text></code>")
        return
    all_users = db.query_all("SELECT telegram_user_id FROM users WHERE is_banned = 0")
    sent = 0
    for u in all_users:
        try:
            await send_message(u["telegram_user_id"], text, disable_notification=True)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    await message.answer(f"📣 Broadcast sent to <b>{sent}</b> of <b>{len(all_users)}</b> users.")


# --------------------------------------------------------------------------- #
# Super-admin — admin management
# --------------------------------------------------------------------------- #

@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: Command):
    if not await _require_super(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/addadmin <user_id></code>")
        return
    users.add_admin(uid, None, None, False, message.from_user.id)
    await message.answer(f"✅ <code>{uid}</code> is now admin.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message, command: Command):
    if not await _require_super(message):
        return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/removeadmin <user_id></code>")
        return
    users.remove_admin(uid)
    await message.answer(f"🗑 <code>{uid}</code> removed from admins.")


@router.message(Command("listadmins"))
async def cmd_listadmins(message: Message):
    if not await _require_admin(message):
        return
    rows = users.list_admins()
    lines = "\n".join(f"• <code>{r['telegram_user_id']}</code> {'(super)' if r['is_super_admin'] else ''}" for r in rows)
    await message.answer(f"🛡️ Admins:\n{lines}")
