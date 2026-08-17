"""All bot commands — grouped exactly like the /help screenshot.

Strict role separation:
  * user   : /start /help /whoami /favs /rfavs
  * admin  : everything except super-admin bucket
  * super  : /addadmin /removeadmin /genimporttoken /setweburl /resetall
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from .. import db
from ..config import settings
from ..services import fsub, posting, repo, sync, users
from ..services.tg import get_chat, send_message, set_my_commands
from ..utils import (parse_duration_ms, parse_tme_link, random_code,
                     random_token, to_int)

router = Router(name="commands")


async def _require_admin(m: Message) -> bool:
    if users.is_admin(m.from_user.id):
        return True
    await m.answer("🚫 Admin only.")
    return False


async def _require_super(m: Message) -> bool:
    if users.is_super_admin(m.from_user.id):
        return True
    await m.answer("🚫 Super-admin only.")
    return False


def _extract_message_id_from_arg(arg: str) -> Optional[tuple[int, int]]:
    """Return (chat_id_or_0, message_id) from a t.me link OR a raw number."""
    arg = (arg or "").strip()
    if not arg:
        return None
    parsed = parse_tme_link(arg)
    if parsed:
        return parsed
    n = to_int(arg)
    if n > 0:
        return (0, n)
    return None


def _resolve_post_ref(arg: str):
    """Look up a post by code or by #N position."""
    arg = (arg or "").strip().lstrip("#")
    if not arg:
        return None
    n = to_int(arg)
    if n > 0:
        p = repo.get_post_by_position(n)
        if p:
            return p
    return repo.get_post_by_code(arg)


# ==================================================================
# GENERAL (user)
# ==================================================================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    uid = message.from_user.id
    if not users.list_admins():
        forced = settings.super_admin_id
        target = forced if forced and forced == uid else uid
        users.add_admin(target, message.from_user.username,
                        message.from_user.first_name, True, target)

    payload = (command.args or "").strip()

    if payload.startswith("ref_"):
        try:
            referrer = int(base64.urlsafe_b64decode(payload[4:] + "==").decode())
            if referrer != uid:
                db.execute("INSERT INTO referrals (referrer_id, referee_id) VALUES (?,?) "
                           "ON CONFLICT DO NOTHING", (referrer, uid))
                db.execute("INSERT INTO referral_bonuses (user_id, bonus_files_remaining) "
                           "VALUES (?,5) ON CONFLICT(user_id) DO UPDATE SET "
                           "bonus_files_remaining=bonus_files_remaining+5", (referrer,))
                users.log_activity(uid, "referral_join", {"referrer": referrer})
        except Exception:
            pass

    if payload and not payload.startswith("ref_"):
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
        return

    await message.answer(
        f"👋 Hello <b>{message.from_user.first_name or 'there'}</b>!\n\n"
        "Send /help to see available commands.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    is_admin = users.is_admin(message.from_user.id)
    text = (
        "📚 <b>Commands</b>\n\n"
        "👤 <b>General</b>\n"
        "/start · /help · /whoami · /favs · /rfavs &lt;n [n...]&gt;")
    if is_admin:
        text += (
            "\n\n🛡️ <b>Admin management</b>\n"
            "/addadmin &lt;user_id&gt; · /removeadmin &lt;user_id&gt; · /listadmins · /genimporttoken\n\n"
            "📡 <b>Channels</b>\n"
            "/addchannel &lt;chat_id&gt; &lt;role&gt; · /removechannel &lt;chat_id&gt; · "
            "/listchannels · /setlog &lt;chat_id&gt;\n\n"
            "📝 <b>Posting</b>\n"
            "/setcaption &lt;template&gt; · /postcaption &lt;text&gt; · /filecaption &lt;text&gt; · "
            "/pauseposting · /resumeposting · /repost &lt;code|#N&gt; · /mpost &lt;link&gt; [link...] · "
            "/deletepost &lt;code|#N&gt; · /undelete &lt;code&gt; · /deletedposts\n\n"
            "⏱ <b>Queue &amp; drip scheduler</b>\n"
            "/queue · /queueinfo [n] · /scheduleoff · /setschedule &lt;m&gt; [batch] · "
            "/dripnow [n] · /reset [n] · /resetall · /setcursor &lt;id|link&gt;\n\n"
            "💾 <b>Backups</b>\n"
            "/addbackup &lt;chat_id&gt; · /removebackup &lt;chat_id&gt; · /listbackup · "
            "/backup &lt;chat_id&gt; · /backup10 &lt;chat_id&gt; · /scandatabase · "
            "/resetbackup [chat_id] · /undoresetbackup &lt;chat_id&gt; &lt;link&gt; · "
            "/dltbackup &lt;chat_id&gt; · /pausebackup · /resumebackup · /backupstatus\n\n"
            "🔒 <b>Content controls</b>\n"
            "/protect 1|0 · /spoiler 1|0 · /autodelete &lt;duration&gt; · "
            "/fsub &lt;chat_id&gt; &lt;invite_link&gt; · /fsublist · /fsubremove &lt;chat_id&gt;\n\n"
            "📊 <b>Users &amp; moderation</b>\n"
            "/stats · /duplicates · /doctor · /broadcast &lt;text&gt; · "
            "/ban &lt;user_id&gt; [reason] · /unban &lt;user_id&gt; · /banlist · /search &lt;query&gt;\n\n"
            "🌐 <b>Web admin</b>\n"
            "/linkweb · /setweburl &lt;url&gt;")
    await message.answer(text)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    uid = message.from_user.id
    role = ("super-admin" if users.is_super_admin(uid)
            else "admin" if users.is_admin(uid) else "user")
    await message.answer(f"ID: <code>{uid}</code>\nRole: <b>{role}</b>")


@router.message(Command("favs"))
async def cmd_favs(message: Message):
    posts = users.list_favorites(message.from_user.id)
    if not posts:
        await message.answer("💔 No favorites.")
        return
    lines = "\n".join(
        f"{i+1}. <code>{p['code']}</code> — {(p['caption'] or '')[:50]}"
        for i, p in enumerate(posts))
    await message.answer(f"❤️ <b>Your favorites</b>\n{lines}\n\n"
                         "Remove: <code>/rfavs 1</code> or <code>/rfavs 1 2 3</code>")


@router.message(Command("rfavs"))
async def cmd_rfavs(message: Message, command: CommandObject):
    """Multi-index remove: /rfavs 1  /rfavs 1 2 3  /rfavs 1-3."""
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Usage: <code>/rfavs 1</code> or <code>/rfavs 1 2 3</code>")
        return
    idx: set[int] = set()
    for token in arg.replace(",", " ").split():
        m = re.match(r"^(\d+)-(\d+)$", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for k in range(min(a, b), max(a, b) + 1):
                idx.add(k)
        else:
            n = to_int(token)
            if n:
                idx.add(n)
    posts = users.list_favorites(message.from_user.id)
    removed = 0
    for i in sorted(idx, reverse=True):
        if 1 <= i <= len(posts):
            users.remove_favorite(message.from_user.id, posts[i - 1]["id"])
            removed += 1
    await message.answer(f"🗑 Removed <b>{removed}</b> favorite(s).")


# ==================================================================
# ADMIN MANAGEMENT
# ==================================================================

@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject):
    if not await _require_super(message): return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/addadmin &lt;user_id&gt;</code>"); return
    users.add_admin(uid, None, None, False, message.from_user.id)
    users.write_audit(message.from_user.id, "addadmin", str(uid))
    await message.answer(f"✅ <code>{uid}</code> is now admin.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message, command: CommandObject):
    if not await _require_super(message): return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/removeadmin &lt;user_id&gt;</code>"); return
    users.remove_admin(uid)
    users.write_audit(message.from_user.id, "removeadmin", str(uid))
    await message.answer(f"🗑 <code>{uid}</code> removed from admins.")


@router.message(Command("listadmins"))
async def cmd_listadmins(message: Message):
    if not await _require_admin(message): return
    rows = users.list_admins()
    if not rows:
        await message.answer("No admins."); return
    lines = "\n".join(f"• <code>{r['telegram_user_id']}</code>"
                     f"{' (super)' if r['is_super_admin'] else ''}" for r in rows)
    await message.answer(f"🛡️ <b>Admins</b>\n{lines}")


@router.message(Command("genimporttoken"))
async def cmd_genimporttoken(message: Message):
    if not await _require_super(message): return
    token = random_token()
    db.execute("INSERT INTO link_tokens (token, kind, user_id) VALUES (?,?,?)",
               (token, "import", message.from_user.id))
    await message.answer(f"🔑 Import token:\n<code>{token}</code>")


# ==================================================================
# CHANNELS
# ==================================================================

@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Usage: <code>/addchannel &lt;chat_id&gt; &lt;role&gt;</code>\n"
            f"Roles: {' | '.join(repo.CHANNEL_ROLES)}"); return
    chat_id, role = parts
    if role not in repo.CHANNEL_ROLES:
        await message.answer(f"Invalid role. Choose: {', '.join(repo.CHANNEL_ROLES)}"); return
    title = None
    try:
        chat = await get_chat(chat_id=to_int(chat_id))
        title = chat.get("title") if isinstance(chat, dict) else None
    except Exception:
        pass
    repo.add_channel(to_int(chat_id), role, title=title, added_by=message.from_user.id)
    users.write_audit(message.from_user.id, "addchannel", chat_id, {"role": role})
    await message.answer(f"✅ Channel <code>{chat_id}</code> ({title or '?'}) "
                         f"registered as <b>{role}</b>.")


@router.message(Command("removechannel"))
async def cmd_removechannel(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/removechannel &lt;chat_id&gt;</code>"); return
    repo.remove_channel(chat_id)
    users.write_audit(message.from_user.id, "removechannel", str(chat_id))
    await message.answer("🗑 Channel removed.")


@router.message(Command("listchannels"))
async def cmd_listchannels(message: Message):
    if not await _require_admin(message): return
    rows = db.query_all("SELECT * FROM channels ORDER BY role, id")
    if not rows:
        await message.answer("No channels configured."); return
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code> — {r['role']}"
                     f" ({r.get('title') or '?'})" for r in rows)
    await message.answer(f"📡 <b>Channels</b>\n{lines}")


@router.message(Command("setlog"))
async def cmd_setlog(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/setlog &lt;chat_id&gt;</code>"); return
    db.execute("DELETE FROM channels WHERE role='log'")
    repo.add_channel(chat_id, "log", added_by=message.from_user.id)
    await message.answer(f"✅ Log channel set to <code>{chat_id}</code>.")


# ==================================================================
# POSTING
# ==================================================================

@router.message(Command("setcaption"))
async def cmd_setcaption(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    text = (command.args or "").strip()
    repo.set_setting("caption_template", text or "{caption}")
    await message.answer("✅ Caption template updated. Placeholders: <code>{caption}</code>, <code>{code}</code>.")


@router.message(Command("postcaption"))
async def cmd_postcaption(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    repo.set_setting("post_caption_extra", (command.args or "").strip())
    await message.answer("✅ This text is now appended <b>below</b> each cover-post caption in the main channel.")


@router.message(Command("filecaption"))
async def cmd_filecaption(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    repo.set_setting("file_caption_extra", (command.args or "").strip())
    await message.answer("✅ This text is now appended <b>below</b> each delivered file's caption in DMs.")


@router.message(Command("pauseposting"))
async def cmd_pauseposting(message: Message):
    if not await _require_admin(message): return
    repo.set_setting("posting_paused", "1")
    await message.answer("⏸ Posting paused.")


@router.message(Command("resumeposting"))
async def cmd_resumeposting(message: Message):
    if not await _require_admin(message): return
    repo.set_setting("posting_paused", "0")
    await message.answer("▶️ Posting resumed.")


@router.message(Command("repost"))
async def cmd_repost(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    post = _resolve_post_ref((command.args or "").strip())
    if not post:
        await message.answer("Usage: <code>/repost &lt;code|#N&gt;</code>"); return
    n = await posting.publish_post_to_mains(post)
    await message.answer(f"✅ Reposted #{post['position']} to <b>{n}</b> channel(s).")


@router.message(Command("mpost"))
async def cmd_mpost(message: Message, command: CommandObject):
    """Import 1+ t.me links from the Database Channel and publish them now."""
    if not await _require_admin(message): return
    args = (command.args or "").split()
    if not args:
        await message.answer("Usage: <code>/mpost &lt;t.me link&gt; [more links…]</code>"); return
    dbs = {int(c["telegram_chat_id"]) for c in repo.get_database_channels()}
    if not dbs:
        await message.answer("⚠️ No database channels configured."); return
    ok = 0
    errs: list[str] = []
    for link in args:
        parsed = parse_tme_link(link)
        if not parsed or parsed[0] == 0:
            errs.append(f"bad link: {link}"); continue
        chat_id, msg_id = parsed
        if chat_id not in dbs:
            errs.append(f"{chat_id} not a database channel"); continue
        post = repo.get_post_by_source(chat_id, msg_id)
        if post is None:
            repo.insert_post(code=random_code(), position=repo.get_next_position(),
                             source_chat_id=chat_id, source_message_id=msg_id,
                             caption="", media_kind="photo")
            post = repo.get_post_by_source(chat_id, msg_id)
        try:
            n = await posting.publish_post_to_mains(post)
            if n > 0:
                ok += 1
            else:
                errs.append(f"{link}: no main channels")
        except Exception as exc:
            errs.append(f"{link}: {exc}")
    reply = f"✅ Posted <b>{ok}</b> of <b>{len(args)}</b>."
    if errs:
        reply += "\n" + "\n".join(f"• {e}" for e in errs[:10])
    await message.answer(reply)


@router.message(Command("deletepost"))
async def cmd_deletepost(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    post = _resolve_post_ref((command.args or "").strip())
    if not post:
        await message.answer("Usage: <code>/deletepost &lt;code|#N&gt;</code>"); return
    db.execute(
        "INSERT INTO deleted_posts (post_id, code, caption, deleted_by, snapshot) VALUES (?,?,?,?,?)",
        (post["id"], post["code"], post.get("caption"), message.from_user.id,
         json.dumps(post, ensure_ascii=False, default=str)))
    db.execute("UPDATE posts SET is_deleted=1 WHERE id=?", (post["id"],))
    users.write_audit(message.from_user.id, "delete_post", post["code"])
    await message.answer(f"🗑 Deleted <code>{post['code']}</code>.")


@router.message(Command("undelete"))
async def cmd_undelete(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    code = (command.args or "").strip()
    if not code:
        await message.answer("Usage: <code>/undelete &lt;code&gt;</code>"); return
    db.execute("UPDATE posts SET is_deleted=0 WHERE code=?", (code,))
    db.execute("DELETE FROM deleted_posts WHERE code=?", (code,))
    await message.answer(f"✅ Restored <code>{code}</code>.")


@router.message(Command("deletedposts"))
async def cmd_deletedposts(message: Message):
    if not await _require_admin(message): return
    rows = db.query_all("SELECT * FROM deleted_posts ORDER BY id DESC LIMIT 30")
    if not rows:
        await message.answer("No deleted posts."); return
    lines = "\n".join(f"• <code>{r['code']}</code> — {r['deleted_at']}" for r in rows)
    await message.answer(f"🗑 <b>Deleted</b>\n{lines}")


# ==================================================================
# QUEUE & DRIP SCHEDULER
# ==================================================================

@router.message(Command("queue"))
async def cmd_queue(message: Message):
    if not await _require_admin(message): return
    await message.answer(f"📦 Queue: <b>{repo.queued_posts_count()}</b> pending / "
                         f"<b>{repo.total_posts()}</b> total.")


@router.message(Command("queueinfo"))
async def cmd_queueinfo(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    n = to_int((command.args or "").strip()) or 10
    rows = db.query_all(
        "SELECT position, code, caption FROM posts "
        "WHERE posted_at IS NULL AND is_deleted=0 ORDER BY position ASC LIMIT ?",
        (min(n, 50),))
    if not rows:
        await message.answer("📭 Queue is empty."); return
    lines = "\n".join(f"#{r['position']} <code>{r['code']}</code> — {(r['caption'] or '')[:40]}"
                     for r in rows)
    await message.answer(f"📦 <b>Next in queue</b>\n{lines}")


@router.message(Command("scheduleoff"))
async def cmd_scheduleoff(message: Message):
    if not await _require_admin(message): return
    repo.set_setting("schedule_paused", "1")
    await message.answer("⏸ Drip scheduler disabled.")


@router.message(Command("setschedule"))
async def cmd_setschedule(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    args = (command.args or "").split()
    minutes = to_int(args[0]) if args else 5
    batch = to_int(args[1]) if len(args) > 1 else 1
    repo.set_setting("drip_config", json.dumps({"minutes": max(1, minutes), "batch": max(1, batch)}))
    repo.set_setting("schedule_paused", "0")
    await message.answer(f"⏱ Schedule set: every <b>{minutes}m</b>, batch <b>{batch}</b>.")


@router.message(Command("dripnow"))
async def cmd_dripnow(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    n = to_int((command.args or "").strip()) or 1
    queued = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NULL AND is_deleted=0 "
        "ORDER BY position ASC LIMIT ?", (min(n, 20),))
    if not queued:
        await message.answer("📭 Queue is empty."); return
    ok = 0
    for post in queued:
        try:
            await posting.publish_post_to_mains(post); ok += 1
        except Exception as exc:
            print(f"[dripnow] {exc}")
    await message.answer(f"⚡ Drip fired: posted <b>{ok}</b> of {len(queued)}.")


@router.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    n = to_int((command.args or "").strip()) or 1
    rows = db.query_all(
        "SELECT id FROM posts WHERE posted_at IS NOT NULL "
        "ORDER BY posted_at DESC LIMIT ?", (min(n, 200),))
    for r in rows:
        db.execute("UPDATE posts SET posted_at=NULL, main_message_id=NULL WHERE id=?", (r["id"],))
        db.execute("DELETE FROM post_copies WHERE post_id=?", (r["id"],))
    await message.answer(f"↩️ Reset {len(rows)} recent post(s) to queued.")


@router.message(Command("resetall"))
async def cmd_resetall(message: Message):
    if not await _require_super(message): return
    db.execute("UPDATE posts SET posted_at=NULL, main_message_id=NULL")
    db.execute("DELETE FROM post_copies")
    await message.answer("↩️ ALL posts reset to queued.")


@router.message(Command("setcursor"))
async def cmd_setcursor(message: Message, command: CommandObject):
    """Accept a raw message_id OR a t.me/c/<id>/<msg> link.

    Cursor is set to (msg_id - 1) so the NEXT captured post is exactly the
    message you pointed at — it will be published to the main channel.
    """
    if not await _require_admin(message): return
    arg = (command.args or "").strip()
    extracted = _extract_message_id_from_arg(arg)
    if not extracted:
        await message.answer(
            "Usage: <code>/setcursor &lt;message_id&gt;</code>\n"
            "Or: <code>/setcursor https://t.me/c/&lt;chan&gt;/&lt;msg_id&gt;</code>")
        return
    chat_id, msg_id = extracted
    if chat_id:
        dbs = {int(c["telegram_chat_id"]) for c in repo.get_database_channels()}
        if chat_id not in dbs:
            await message.answer(
                f"⚠️ Link points to <code>{chat_id}</code>, which is not a "
                "registered database channel. Add it first with /addchannel."); return
    new_cursor = max(0, msg_id - 1)
    repo.set_cursor(new_cursor)
    sync._pending.clear()
    users.write_audit(message.from_user.id, "setcursor", str(msg_id))
    await message.answer(
        f"✅ Cursor set to <code>{new_cursor}</code>.\n"
        f"The next post the bot picks up from the Database Channel "
        f"(message id ≥ <b>{msg_id}</b>) will be queued &amp; posted to the main channel.")


# ==================================================================
# BACKUPS
# ==================================================================

@router.message(Command("addbackup"))
async def cmd_addbackup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/addbackup &lt;chat_id&gt;</code>"); return
    repo.add_channel(chat_id, "backup", added_by=message.from_user.id)
    await message.answer("✅ Backup channel registered.")


@router.message(Command("removebackup"))
async def cmd_removebackup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    db.execute("DELETE FROM channels WHERE telegram_chat_id=? AND role='backup'", (chat_id,))
    db.execute("DELETE FROM backup_copies WHERE backup_chat_id=?", (chat_id,))
    await message.answer("🗑 Backup channel removed.")


@router.message(Command("listbackup"))
async def cmd_listbackup(message: Message):
    if not await _require_admin(message): return
    rows = db.query_all("SELECT * FROM channels WHERE role='backup'")
    if not rows:
        await message.answer("No backup channels."); return
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code>" for r in rows)
    await message.answer(f"💾 <b>Backup channels</b>\n{lines}")


async def _run_backup(chat_id: int, limit: int) -> int:
    pending = db.query_all(
        "SELECT p.* FROM posts p WHERE p.is_deleted=0 "
        "AND NOT EXISTS (SELECT 1 FROM backup_copies b "
        "                WHERE b.post_id=p.id AND b.backup_chat_id=?) "
        "ORDER BY p.position ASC LIMIT ?", (chat_id, limit))
    n = 0
    for post in pending:
        try:
            await posting.mirror_post_to_backup(post, chat_id); n += 1
        except Exception as exc:
            print(f"[backup] {exc}")
    return n


@router.message(Command("backup"))
async def cmd_backup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/backup &lt;chat_id&gt;</code>"); return
    n = await _run_backup(chat_id, 5)
    await message.answer(f"💾 Mirrored {n} post(s). Remaining continue on backup ticks.")


@router.message(Command("backup10"))
async def cmd_backup10(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/backup10 &lt;chat_id&gt;</code>"); return
    n = await _run_backup(chat_id, 10)
    await message.answer(f"💾 Mirrored {n} post(s).")


@router.message(Command("scandatabase"))
async def cmd_scandatabase(message: Message):
    if not await _require_admin(message): return
    total = 0
    for ch in repo.get_backup_channels():
        total += await _run_backup(int(ch["telegram_chat_id"]), 5)
    await message.answer(f"🔁 Forwarded {total} post(s) to backup channels.")


@router.message(Command("resetbackup"))
async def cmd_resetbackup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if chat_id:
        db.execute("DELETE FROM backup_copies WHERE backup_chat_id=?", (chat_id,))
    else:
        db.execute("DELETE FROM backup_copies")
    await message.answer("♻️ Backup mirror log cleared.")


@router.message(Command("undoresetbackup"))
async def cmd_undoresetbackup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Usage: <code>/undoresetbackup &lt;chat_id&gt; &lt;post_link&gt;</code>"); return
    chat_id = to_int(args[0])
    parsed = parse_tme_link(args[1])
    if not chat_id or not parsed:
        await message.answer("Invalid chat_id or link."); return
    db.execute("INSERT INTO backup_copies (backup_chat_id, post_id, message_id) "
               "VALUES (?,?,?) ON CONFLICT DO NOTHING", (chat_id, parsed[1], parsed[1]))
    await message.answer("✅ Marked as already mirrored.")


@router.message(Command("dltbackup"))
async def cmd_dltbackup(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    if not chat_id:
        await message.answer("Usage: <code>/dltbackup &lt;chat_id&gt;</code>"); return
    db.execute("DELETE FROM backup_copies WHERE backup_chat_id=?", (chat_id,))
    db.execute("DELETE FROM channels WHERE telegram_chat_id=? AND role='backup'", (chat_id,))
    await message.answer("🗑 Backup channel deleted and log cleared.")


@router.message(Command("pausebackup"))
async def cmd_pausebackup(message: Message):
    if not await _require_admin(message): return
    repo.set_setting("backup_paused", "1")
    await message.answer("⏸ Backup mirroring paused.")


@router.message(Command("resumebackup"))
async def cmd_resumebackup(message: Message):
    if not await _require_admin(message): return
    repo.set_setting("backup_paused", "0")
    await message.answer("▶️ Backup mirroring resumed.")


@router.message(Command("backupstatus"))
async def cmd_backupstatus(message: Message):
    if not await _require_admin(message): return
    rows = db.query_all(
        "SELECT c.telegram_chat_id, c.title, "
        "  (SELECT COUNT(*) FROM backup_copies bc WHERE bc.backup_chat_id=c.telegram_chat_id) as mirrored "
        "FROM channels c WHERE c.role='backup'")
    if not rows:
        await message.answer("No backup channels."); return
    total = repo.total_posts()
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code> ({r.get('title') or '?'}) "
                     f"— {r['mirrored']}/{total}" for r in rows)
    await message.answer(f"💾 <b>Backup status</b>\n{lines}")


# ==================================================================
# CONTENT CONTROLS
# ==================================================================

@router.message(Command("protect"))
async def cmd_protect(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    v = (command.args or "").strip()
    if v not in ("0", "1"):
        await message.answer("Usage: <code>/protect 1</code> or <code>/protect 0</code>"); return
    repo.set_setting("protect_content", v)
    await message.answer(f"🔒 Protect-content: <b>{'ON' if v == '1' else 'OFF'}</b>")


@router.message(Command("spoiler"))
async def cmd_spoiler(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    v = (command.args or "").strip()
    if v not in ("0", "1"):
        await message.answer("Usage: <code>/spoiler 1</code> or <code>/spoiler 0</code>"); return
    repo.set_setting("spoiler_media", v)
    await message.answer(f"🕶 Spoiler-media: <b>{'ON' if v == '1' else 'OFF'}</b>")


@router.message(Command("autodelete"))
async def cmd_autodelete(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    arg = (command.args or "").strip()
    ms = parse_duration_ms(arg) if arg else 0
    if not ms:
        repo.set_setting("autodelete_ms", "0")
        await message.answer("🚫 Autodelete disabled."); return
    repo.set_setting("autodelete_ms", str(ms))
    await message.answer(f"⏳ Autodelete set to <b>{arg}</b>.")


@router.message(Command("fsub"))
async def cmd_fsub(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/fsub &lt;chat_id&gt; &lt;invite_link&gt;</code>"); return
    chat_id, link = to_int(parts[0]), parts[1]
    if not chat_id:
        await message.answer("Invalid chat_id."); return
    title = None
    try:
        chat = await get_chat(chat_id=chat_id)
        title = chat.get("title") if isinstance(chat, dict) else None
    except Exception:
        pass
    repo.add_channel(chat_id, "forcesub", title=title,
                     invite_link=link, added_by=message.from_user.id)
    await message.answer(f"✅ Force-sub channel added: <code>{chat_id}</code>")


@router.message(Command("fsublist"))
async def cmd_fsublist(message: Message):
    if not await _require_admin(message): return
    rows = repo.get_forcesub_channels()
    if not rows:
        await message.answer("No force-sub channels."); return
    lines = "\n".join(f"• <code>{r['telegram_chat_id']}</code> — {r.get('title') or '?'}"
                     for r in rows)
    await message.answer(f"📢 <b>Force-sub</b>\n{lines}")


@router.message(Command("fsubremove"))
async def cmd_fsubremove(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    chat_id = to_int((command.args or "").strip())
    db.execute("DELETE FROM channels WHERE telegram_chat_id=? AND role='forcesub'", (chat_id,))
    await message.answer("🗑 Force-sub channel removed.")


# ==================================================================
# USERS & MODERATION
# ==================================================================

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _require_admin(message): return
    await message.answer(
        f"📊 <b>Stats</b>\n"
        f"Posts: <b>{repo.total_posts()}</b> (published {repo.published_posts_count()})\n"
        f"Queued: <b>{repo.queued_posts_count()}</b>\n"
        f"Users: <b>{users.user_count()}</b> (banned {users.banned_count()})\n"
        f"Cursor: <code>{repo.get_cursor()}</code>")


@router.message(Command("duplicates"))
async def cmd_duplicates(message: Message):
    if not await _require_admin(message): return
    rows = db.query_all(
        "SELECT caption, COUNT(*) c FROM posts WHERE caption IS NOT NULL AND caption != '' "
        "GROUP BY caption HAVING c > 1 ORDER BY c DESC LIMIT 20")
    if not rows:
        await message.answer("✅ No duplicate captions."); return
    lines = "\n".join(f"• ×{r['c']} — {(r['caption'] or '')[:40]}" for r in rows)
    await message.answer(f"🔁 <b>Duplicates</b>\n{lines}")


@router.message(Command("doctor"))
async def cmd_doctor(message: Message):
    if not await _require_admin(message): return
    no_file = db.query_scalar(
        "SELECT COUNT(*) FROM posts WHERE file_id IS NULL AND media_kind != 'text'") or 0
    await message.answer(
        f"🩺 <b>Doctor</b>\n"
        f"Database channels: {len(repo.get_database_channels())}\n"
        f"Main channels: {len(repo.get_main_channels())}\n"
        f"Force-sub: {len(repo.get_forcesub_channels())}\n"
        f"Backup: {len(repo.get_backup_channels())}\n"
        f"Log: {repo.get_log_channel_id() or '—'}\n"
        f"Posts without file_id: {no_file}\n"
        f"Queue: {repo.queued_posts_count()} pending\n"
        f"Cursor: <code>{repo.get_cursor()}</code>\n"
        f"Posting paused: {repo.get_setting_bool('posting_paused')}\n"
        f"Schedule paused: {repo.get_setting_bool('schedule_paused')}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Usage: <code>/broadcast &lt;text&gt;</code>"); return
    all_users = db.query_all("SELECT telegram_user_id FROM users WHERE is_banned=0")
    sent = 0
    for u in all_users:
        try:
            await send_message(u["telegram_user_id"], text, disable_notification=True); sent += 1
        except Exception:
            pass
    users.write_audit(message.from_user.id, "broadcast", None, {"sent": sent})
    await message.answer(f"📣 Sent to <b>{sent}</b> of <b>{len(all_users)}</b>.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    args = (command.args or "").split(maxsplit=1)
    uid = to_int(args[0]) if args else 0
    reason = args[1] if len(args) > 1 else "admin ban"
    if not uid:
        await message.answer("Usage: <code>/ban &lt;user_id&gt; [reason]</code>"); return
    users.upsert_user(uid)
    users.set_ban(uid, True, reason)
    users.write_audit(message.from_user.id, "ban", str(uid), {"reason": reason})
    await message.answer(f"⛔ Banned <code>{uid}</code>: {reason}")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    uid = to_int((command.args or "").strip())
    if not uid:
        await message.answer("Usage: <code>/unban &lt;user_id&gt;</code>"); return
    users.set_ban(uid, False)
    users.write_audit(message.from_user.id, "unban", str(uid))
    await message.answer(f"✅ Unbanned <code>{uid}</code>.")


@router.message(Command("banlist"))
async def cmd_banlist(message: Message):
    if not await _require_admin(message): return
    rows = users.list_banned()
    if not rows:
        await message.answer("✅ No banned users."); return
    lines = "\n".join(f"• <code>{r['telegram_user_id']}</code> — {r.get('ban_reason') or '—'}"
                     for r in rows)
    await message.answer(f"⛔ <b>Banned</b>\n{lines}")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject):
    if not await _require_admin(message): return
    q = (command.args or "").strip()
    if not q:
        await message.answer("Usage: <code>/search &lt;query&gt;</code>"); return
    rows = db.query_all(
        "SELECT position, code, caption FROM posts "
        "WHERE caption LIKE ? AND is_deleted=0 ORDER BY position DESC LIMIT 20",
        (f"%{q}%",))
    if not rows:
        await message.answer(f"🔍 No matches for “{q}”."); return
    lines = "\n".join(f"#{r['position']} <code>{r['code']}</code> — {(r['caption'] or '')[:40]}"
                     for r in rows)
    await message.answer(f"🔍 <b>{len(rows)} match(es)</b>\n{lines}")


# ==================================================================
# WEB ADMIN
# ==================================================================

@router.message(Command("linkweb"))
async def cmd_linkweb(message: Message):
    if not await _require_admin(message): return
    token = random_token()
    db.execute("INSERT INTO link_tokens (token, kind, user_id) VALUES (?,?,?)",
               (token, "web_admin", message.from_user.id))
    web = repo.get_setting("web_app_url") or settings.base_webhook_url
    await message.answer(f"🌐 Admin link:\n<code>{web}/admin?token={token}</code>")


@router.message(Command("setweburl"))
async def cmd_setweburl(message: Message, command: CommandObject):
    if not await _require_super(message): return
    repo.set_setting("web_app_url", (command.args or "").strip())
    await message.answer("✅ Web URL set.")


# ==================================================================
# Menu registration (called from main.on_startup)
# ==================================================================

async def register_menu_commands():
    """Only USER-visible commands appear in the blue Menu button."""
    cmds = [
        ("start", "Welcome / fetch by code"),
        ("help", "Command list"),
        ("whoami", "Your ID and role"),
        ("favs", "Your saved files"),
        ("rfavs", "Remove favorites by number"),
    ]
    try:
        await set_my_commands(cmds)
    except Exception as exc:
        print(f"[menu] set_my_commands failed: {exc}")
