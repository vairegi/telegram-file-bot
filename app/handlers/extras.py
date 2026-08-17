"""Extra commands completing the reference-page parity.

Discovery: /random /recent /trending /similar /leaderboard
Engagement: /mystats /streak /referral /notify /unnotify
Scheduling: /postlater /postlaterlist /postlatercancel /setslotcount
Channels: /alsopost /setrole /backfill /backfillstatus /cancelbackfill
Content: /cmdautodelete
Shortener: /shortener* /randomurl /addurl /listurl /delurl /limiturl
Users: /exportusers /activity /warn /warns /unwarn /unbanall /health
        /dbexport /audit /favsall /favsrecent /whosaved /topfavs
"""
from __future__ import annotations

import base64
import json
import re
from io import BytesIO

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from .. import db
from ..config import settings
from ..services import backfill, fsub, posting, repo, users
from ..services.tg import send_message
from ..utils import now_iso, parse_duration_ms, random_token, to_int

router = Router(name="extras")


async def _req_admin(m: Message) -> bool:
    if users.is_admin(m.from_user.id):
        return True
    await m.answer("🚫 Admin only.")
    return False


async def _req_super(m: Message) -> bool:
    if users.is_super_admin(m.from_user.id):
        return True
    await m.answer("🚫 Super-admin only.")
    return False


# ==================================================================
# DISCOVERY (user)
# ==================================================================

@router.message(Command("random"))
async def cmd_random(message: Message):
    post = db.query_one(
        "SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 "
        "ORDER BY RANDOM() LIMIT 1")
    if not post:
        await message.answer("❌ No published posts yet."); return
    await posting.deliver_file_to_user(message.from_user.id, post)


@router.message(Command("recent"))
async def cmd_recent(message: Message):
    posts = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NOT NULL AND is_deleted=0 "
        "ORDER BY id DESC LIMIT 10")
    if not posts:
        await message.answer("❌ No posts yet."); return
    uname = await posting.get_bot_username()
    lines = []
    for p in posts:
        title = posting.extract_title(p)
        link = f"https://t.me/{uname}?start={p['code']}"
        lines.append(f'• <a href="{link}">{title}</a>')
    await message.answer("🕘 <b>Recent</b>\n" + "\n".join(lines),
                         disable_web_page_preview=True)


@router.message(Command("trending"))
async def cmd_trending(message: Message):
    rows = db.query_all(
        "SELECT p.*, (SELECT COUNT(*) FROM activity_log a "
        "WHERE a.action='fetch_by_code' AND a.details LIKE '%'||p.code||'%' "
        "AND a.created_at > datetime('now','-7 days')) c "
        "FROM posts p WHERE p.posted_at IS NOT NULL ORDER BY c DESC LIMIT 10")
    if not rows:
        await message.answer("❌ No data yet."); return
    uname = await posting.get_bot_username()
    lines = [f"{i+1}. <a href='https://t.me/{uname}?start={p['code']}'>"
             f"{posting.extract_title(p)}</a> — {p['c'] or 0} fetches"
             for i, p in enumerate(rows)]
    await message.answer("🔥 <b>Trending (7d)</b>\n" + "\n".join(lines),
                         disable_web_page_preview=True)


@router.message(Command("similar"))
async def cmd_similar(message: Message, command: CommandObject):
    tag = (command.args or "").strip().lstrip("#")
    if not tag:
        await message.answer("Usage: <code>/similar #tag</code>"); return
    posts = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NOT NULL AND caption LIKE ? "
        "AND is_deleted=0 ORDER BY id DESC LIMIT 10", (f"%#{tag}%",))
    if not posts:
        await message.answer(f"❌ Nothing matching #{tag}."); return
    uname = await posting.get_bot_username()
    lines = [f'• <a href="https://t.me/{uname}?start={p["code"]}">'
             f'{posting.extract_title(p)}</a>' for p in posts]
    await message.answer(f"🔎 <b>#{tag}</b>\n" + "\n".join(lines),
                         disable_web_page_preview=True)


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    rows = db.query_all(
        "SELECT telegram_user_id, username, files_fetched FROM users "
        "ORDER BY files_fetched DESC LIMIT 10")
    if not rows:
        await message.answer("❌ No users yet."); return
    lines = "\n".join(f"{i+1}. {r['username'] or r['telegram_user_id']} — {r['files_fetched']}"
                      for i, r in enumerate(rows))
    await message.answer(f"🏆 <b>Leaderboard</b>\n{lines}")


# ==================================================================
# ENGAGEMENT (user)
# ==================================================================

@router.message(Command("mystats"))
async def cmd_mystats(message: Message):
    uid = message.from_user.id
    u = db.query_one("SELECT * FROM users WHERE telegram_user_id=?", (uid,))
    favs = db.query_scalar("SELECT COUNT(*) FROM favorites WHERE user_id=?", (uid,))
    refs = db.query_scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,))
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id=?", (uid,)) or 0
    s = users.get_streak(uid)
    await message.answer(
        f"📊 <b>Your stats</b>\n"
        f"Fetched: <b>{u['files_fetched'] if u else 0}</b>\n"
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
    bonus = db.query_scalar("SELECT bonus_files_remaining FROM referral_bonuses WHERE user_id=?", (uid,)) or 0
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
            await message.answer("You have no tag subscriptions."); return
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
        await message.answer("Removed all subscriptions."); return
    if tag:
        db.execute("DELETE FROM tag_subscriptions WHERE user_id=? AND tag=?",
                   (message.from_user.id, tag))
        await message.answer(f"Unsubscribed from #{tag}.")
    else:
        await message.answer("Usage: <code>/unnotify #tag</code> or <code>/unnotify all</code>")


# ==================================================================
# CHANNELS (admin) — extras
# ==================================================================

@router.message(Command("alsopost"))
async def cmd_alsopost(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Usage: <code>/alsopost &lt;chat_id&gt; &lt;on|off&gt;</code>"); return
    chat_id, toggle = to_int(parts[0]), parts[1].lower()
    if toggle not in ("on", "off"):
        await message.answer("Use on|off"); return
    repo.set_channel_flag(chat_id, "also_post", toggle == "on")
    await message.answer(f"✅ alsopost {toggle} for <code>{chat_id}</code>.")


@router.message(Command("setrole"))
async def cmd_setrole(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer("Usage: <code>/setrole &lt;chat_id&gt; &lt;main|forcesub|backup&gt; &lt;on|off&gt;</code>")
        return
    chat_id, role, toggle = to_int(parts[0]), parts[1], parts[2].lower()
    field = {"main": "also_post", "forcesub": "also_fsub", "backup": "also_backup"}.get(role)
    if not field or toggle not in ("on", "off"):
        await message.answer("Role must be main|forcesub|backup, toggle on|off."); return
    repo.set_channel_flag(chat_id, field, toggle == "on")
    await message.answer(f"✅ Extra role <b>{role}</b> {toggle} for <code>{chat_id}</code>.")


@router.message(Command("backfill"))
async def cmd_backfill(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    args = (command.args or "").split()
    if not args:
        await message.answer("Usage: <code>/backfill #&lt;from&gt; [#&lt;to&gt;] [chat_id]</code>"); return
    from_pos = to_int(args[0].lstrip("#"))
    to_pos = to_int(args[1].lstrip("#")) if len(args) > 1 and args[1].startswith("#") else None
    chat_id = to_int(args[2]) if len(args) > 2 else (to_int(args[1]) if len(args) > 1 and not args[1].startswith("#") else None)
    chat_ids = [chat_id] if chat_id else [int(c["telegram_chat_id"]) for c in repo.get_main_channels()]
    if not chat_ids:
        await message.answer("⚠️ No main channels configured."); return
    job = backfill.start_job(chat_ids, from_pos, to_pos, message.from_user.id)
    await message.answer(backfill.status_text(job))


@router.message(Command("backfillstatus"))
async def cmd_backfillstatus(message: Message):
    if not await _req_admin(message): return
    job = backfill.get_running_job()
    await message.answer(backfill.status_text(job) if job else "No running backfill.")


@router.message(Command("cancelbackfill"))
async def cmd_cancelbackfill(message: Message):
    if not await _req_admin(message): return
    ok = backfill.cancel_job()
    await message.answer("🗑 Backfill cancelled." if ok else "No running backfill.")


# ==================================================================
# QUEUE & SCHEDULER — extras
# ==================================================================

@router.message(Command("postlater"))
async def cmd_postlater(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    args = (command.args or "").split()
    if len(args) < 1:
        await message.answer("Usage: <code>/postlater &lt;5h 2m&gt; [code]</code>"); return
    # Find duration tokens (e.g. 5h 2m) and an optional trailing code.
    dur_tokens = [t for t in args if re.match(r"^\d+[smhd]$", t.lower())]
    code = args[-1] if args and not re.match(r"^\d+[smhd]$", args[-1].lower()) else None
    if not dur_tokens:
        await message.answer("Invalid duration."); return
    ms = parse_duration_ms(" ".join(dur_tokens))
    if not ms:
        await message.answer("Invalid duration."); return
    import time
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ms / 1000))
    db.execute("INSERT INTO scheduled_posts (kind, post_code, scheduled_for, created_by) "
               "VALUES ('code',?,?,?)", (code, when, message.from_user.id))
    await message.answer(f"⏳ Scheduled <code>{code or '(reply-to-media)'}</code> for {when} UTC.")


@router.message(Command("postlaterlist"))
async def cmd_postlaterlist(message: Message):
    if not await _req_admin(message): return
    rows = db.query_all(
        "SELECT * FROM scheduled_posts WHERE status='pending' "
        "ORDER BY scheduled_for ASC LIMIT 20")
    if not rows:
        await message.answer("No pending scheduled posts."); return
    lines = "\n".join(f"• id={r['id']} {r['kind']} <code>{r['post_code'] or ''}</code> @ {r['scheduled_for']}"
                      for r in rows)
    await message.answer(f"📅 <b>Scheduled</b>\n{lines}")


@router.message(Command("postlatercancel"))
async def cmd_postlatercancel(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    sid = to_int((command.args or "").strip())
    db.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='pending'", (sid,))
    await message.answer("🗑 Cancelled.")


@router.message(Command("setslotcount"))
async def cmd_setslotcount(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Usage: <code>/setslotcount &lt;HH:MM|all&gt; &lt;n&gt;</code>"); return
    slot, n = parts[0], to_int(parts[1])
    if not n:
        await message.answer("Invalid count."); return
    cfg = repo.get_setting_json("drip_slots", {}) or {}
    cfg[slot] = n
    repo.set_setting("drip_slots", cfg)
    await message.answer(f"⏱ Slot <b>{slot}</b> → <b>{n}</b> posts.")


# ==================================================================
# CONTENT CONTROLS — extras
# ==================================================================

@router.message(Command("cmdautodelete"))
async def cmd_cmdautodelete(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    arg = (command.args or "").strip()
    if arg.lower() in ("off", "0", ""):
        repo.set_setting("cmd_autodelete_ms", "0")
        await message.answer("🚫 Command autodelete disabled."); return
    ms = parse_duration_ms(arg)
    if not ms:
        await message.answer("Invalid duration."); return
    repo.set_setting("cmd_autodelete_ms", str(ms))
    await message.answer(f"⏳ Command autodelete set to <b>{arg}</b> (admins exempt).")


# ==================================================================
# LINK SHORTENER (admin)
# ==================================================================

def _sh_get(key, default=""):
    cfg = repo.get_setting_json("shortener", {}) or {}
    return cfg.get(key, default)


def _sh_set(key, value):
    cfg = repo.get_setting_json("shortener", {}) or {}
    cfg[key] = value
    repo.set_setting("shortener", cfg)


@router.message(Command("shortener"))
async def cmd_shortener(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    arg = (command.args or "").strip().lower()
    if arg == "status" or not arg:
        cfg = repo.get_setting_json("shortener", {}) or {}
        await message.answer(f"🔗 <b>Shortener</b>\n<code>{json.dumps(cfg, indent=2)}</code>")
        return
    if arg in ("on", "off"):
        _sh_set("enabled", arg == "on")
        await message.answer(f"🔗 Shortener: <b>{arg.upper()}</b>"); return
    await message.answer("Usage: /shortener on|off|status")


@router.message(Command("shortenerapi"))
async def cmd_shortenerapi(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    _sh_set("api", (command.args or "").strip())
    await message.answer("✅ Shortener API set.")


@router.message(Command("shortenerlimit"))
async def cmd_shortenerlimit(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    n = to_int((command.args or "").strip())
    if not n:
        await message.answer("Usage: <code>/shortenerlimit &lt;n&gt;</code>"); return
    _sh_set("limit", n)
    await message.answer(f"✅ Limit set to <b>{n}</b> files.")


@router.message(Command("shortenerhours"))
async def cmd_shortenerhours(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    n = to_int((command.args or "").strip())
    if not n:
        await message.answer("Usage: <code>/shortenerhours &lt;n&gt;</code>"); return
    _sh_set("hours", n)
    await message.answer(f"✅ Verification valid for <b>{n}h</b>.")


@router.message(Command("shortenermsg"))
async def cmd_shortenermsg(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    _sh_set("message", (command.args or "").strip())
    await message.answer("✅ Verify message set.")


@router.message(Command("shortenertutorial"))
async def cmd_shortenertutorial(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    v = (command.args or "").strip()
    _sh_set("tutorial_url", "" if v.lower() == "off" else v)
    await message.answer("✅ Tutorial link updated.")


@router.message(Command("shortenerbtn"))
async def cmd_shortenerbtn(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    parts = (command.args or "").split("|")
    if len(parts) < 1:
        await message.answer("Usage: <code>/shortenerbtn Verify | How to open</code>"); return
    _sh_set("button_text", parts[0].strip())
    if len(parts) > 1:
        _sh_set("tutorial_text", parts[1].strip())
    await message.answer("✅ Button labels updated.")


# ==================================================================
# URL LISTS (admin)
# ==================================================================

def _url_templates() -> list[dict]:
    return db.query_all("SELECT * FROM url_templates ORDER BY position ASC")


@router.message(Command("addurl"))
async def cmd_addurl(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    url = (command.args or "").strip()
    if not url:
        await message.answer("Usage: <code>/addurl &lt;url&gt;</code>"); return
    m = re.search(r"(\d+)", url)
    if not m:
        await message.answer("URL must contain a numeric run to randomize."); return
    prefix, digits, suffix = url[:m.start()], m.group(1), url[m.end():]
    pos = int(db.query_scalar("SELECT COALESCE(MAX(position),0)+1 FROM url_templates") or 1)
    db.execute(
        "INSERT INTO url_templates (position, url, prefix, suffix, min_id, max_id) "
        "VALUES (?,?,?,?,?,?)",
        (pos, url, prefix, suffix, 10 ** (len(digits) - 1), 10 ** len(digits) - 1))
    await message.answer(f"✅ URL #{pos} stored (digits: {digits}).")


@router.message(Command("listurl"))
async def cmd_listurl(message: Message):
    if not await _req_admin(message): return
    rows = _url_templates()
    if not rows:
        await message.answer("No URL templates."); return
    lines = "\n".join(f"#{r['position']} — {r['url']}  ({r['min_id']}–{r['max_id']})"
                      for r in rows)
    await message.answer(f"🔗 <b>URL templates</b>\n{lines}")


@router.message(Command("delurl"))
async def cmd_delurl(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    n = to_int((command.args or "").strip())
    db.execute("DELETE FROM url_templates WHERE position=?", (n,))
    await message.answer("🗑 URL removed.")


@router.message(Command("limiturl"))
async def cmd_limiturl(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    parts = (command.args or "").replace("-", " ").split()
    if len(parts) == 2:
        lo, hi = to_int(parts[0]), to_int(parts[1])
        db.execute("UPDATE url_templates SET min_id=?, max_id=?", (lo, hi))
        await message.answer(f"✅ All URLs limited to {lo}–{hi}."); return
    if len(parts) == 3:
        n, lo, hi = to_int(parts[0]), to_int(parts[1]), to_int(parts[2])
        db.execute("UPDATE url_templates SET min_id=?, max_id=? WHERE position=?", (lo, hi, n))
        await message.answer(f"✅ URL #{n} limited to {lo}–{hi}."); return
    await message.answer("Usage: <code>/limiturl [n] &lt;min&gt; - &lt;max&gt;</code>")


@router.message(Command("randomurl"))
async def cmd_randomurl(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    import random
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Usage: <code>/randomurl &lt;n&gt; [count]</code>"); return
    n, count = to_int(parts[0]), to_int(parts[1]) if len(parts) > 1 else 1
    count = max(1, min(count, 50))
    row = db.query_one("SELECT * FROM url_templates WHERE position=?", (n,))
    if not row:
        await message.answer("Template not found."); return
    lo, hi = int(row["min_id"]), int(row["max_id"])
    used = set()
    out = []
    while len(out) < count and len(used) < (hi - lo + 1):
        num = random.randint(lo, hi)
        if num in used:
            continue
        used.add(num)
        out.append(f"{row['prefix']}{num}{row['suffix']}")
    await message.answer("\n".join(out))


# ==================================================================
# USERS & MODERATION — extras
# ==================================================================

@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    args = (command.args or "").split(maxsplit=1)
    uid = to_int(args[0]) if args else 0
    reason = args[1] if len(args) > 1 else ""
    if not uid:
        await message.answer("Usage: <code>/warn &lt;user_id&gt; [reason]</code>"); return
    count = users.add_warning(uid, message.from_user.id, reason)
    users.write_audit(message.from_user.id, "warn", str(uid), {"reason": reason})
    if count >= 3:
        users.set_ban(uid, True, "3 warnings")
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {count}). Auto-banned for 3 warnings.")
    else:
        await message.answer(f"⚠️ Warned <code>{uid}</code> (now {count}).")


@router.message(Command("warns"))
async def cmd_warns(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    uid = to_int((command.args or "").strip())
    rows = users.list_warnings(uid)
    if not rows:
        await message.answer("No warnings for this user."); return
    lines = "\n".join(f"• {r['reason'] or '—'} ({r['created_at']})" for r in rows)
    await message.answer(f"⚠️ <b>Warnings for {uid}</b>\n{lines}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    uid = to_int((command.args or "").strip())
    users.clear_warnings(uid)
    users.write_audit(message.from_user.id, "unwarn", str(uid))
    await message.answer(f"✅ Cleared warnings for <code>{uid}</code>.")


@router.message(Command("unbanall"))
async def cmd_unbanall(message: Message):
    if not await _req_admin(message): return
    n = users.unban_all()
    users.write_audit(message.from_user.id, "unbanall")
    await message.answer(f"✅ Unbanned {n} user(s).")


@router.message(Command("activity"))
async def cmd_activity(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    n = to_int((command.args or "").strip()) or 20
    rows = db.query_all("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?",
                        (min(n, 100),))
    if not rows:
        await message.answer("No activity."); return
    lines = "\n".join(f"• {r['actor_id']} {r['action']} ({r['created_at'][:16]})" for r in rows)
    await message.answer(f"📋 <b>Activity</b>\n{lines}")


@router.message(Command("audit"))
async def cmd_audit(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    n = to_int((command.args or "").strip()) or 20
    rows = db.query_all("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
                        (min(n, 100),))
    if not rows:
        await message.answer("No audit entries."); return
    lines = "\n".join(f"• {r['admin_id']} {r['action']} {r['target'] or ''}" for r in rows)
    await message.answer(f"🧾 <b>Audit</b>\n{lines}")


@router.message(Command("health"))
async def cmd_health(message: Message):
    if not await _req_admin(message): return
    pd = db.query_scalar("SELECT COUNT(*) FROM pending_deletions") or 0
    q = repo.queued_posts_count()
    await message.answer(f"💚 <b>Health</b>\nPending deletions: <b>{pd}</b>\n"
                         f"Queued posts: <b>{q}</b>\nCursor: <code>{repo.get_cursor()}</code>")


@router.message(Command("exportusers"))
async def cmd_exportusers(message: Message):
    if not await _req_admin(message): return
    rows = db.query_all("SELECT telegram_user_id, username, first_name, joined_at, "
                        "files_fetched, is_banned FROM users ORDER BY id")
    lines = ["user_id,username,first_name,joined_at,files_fetched,is_banned"]
    for r in rows:
        lines.append(f"{r['telegram_user_id']},{r.get('username') or ''},"
                     f"{(r.get('first_name') or '').replace(',', ' ')},"
                     f"{r.get('joined_at') or ''},{r['files_fetched']},{r['is_banned']}")
    buf = BytesIO("\n".join(lines).encode())
    await message.answer_document(BufferedInputFile(buf.read(), filename="users.csv"),
                                  caption="👥 Users export")


@router.message(Command("dbexport"))
async def cmd_dbexport(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    arg = (command.args or "").strip().lower()
    if arg == "now" or not arg:
        # Simple JSON export of key tables.
        payload = {
            "users": db.query_all("SELECT * FROM users"),
            "posts": db.query_all("SELECT * FROM posts"),
            "channels": db.query_all("SELECT * FROM channels"),
        }
        buf = BytesIO(json.dumps(payload, ensure_ascii=False, default=str).encode())
        await message.answer_document(
            BufferedInputFile(buf.read(), filename="dbexport.json"),
            caption="💾 Database export")
    else:
        repo.set_setting("dbexport_schedule", arg)
        await message.answer(f"✅ dbexport schedule: {arg}")


# ---- favorites admin
@router.message(Command("favsall"))
async def cmd_favsall(message: Message):
    if not await _req_admin(message): return
    rows = db.query_all("SELECT user_id, COUNT(*) c FROM favorites GROUP BY user_id "
                        "ORDER BY c DESC LIMIT 10")
    if not rows:
        await message.answer("No favorites."); return
    lines = "\n".join(f"• <code>{r['user_id']}</code> — {r['c']}" for r in rows)
    await message.answer(f"❤️ <b>Top savers</b>\n{lines}")


@router.message(Command("favsrecent"))
async def cmd_favsrecent(message: Message):
    if not await _req_admin(message): return
    rows = db.query_all("SELECT user_id, post_id, created_at FROM favorites "
                        "ORDER BY created_at DESC LIMIT 20")
    if not rows:
        await message.answer("No favorites."); return
    lines = "\n".join(f"• <code>{r['user_id']}</code> saved post {r['post_id']}" for r in rows)
    await message.answer(f"❤️ <b>Recent favorites</b>\n{lines}")


@router.message(Command("whosaved"))
async def cmd_whosaved(message: Message, command: CommandObject):
    if not await _req_admin(message): return
    code = (command.args or "").strip()
    post = repo.get_post_by_code(code)
    if not post:
        await message.answer("Post not found."); return
    rows = db.query_all("SELECT user_id FROM favorites WHERE post_id=?", (post["id"],))
    if not rows:
        await message.answer("Nobody saved this yet."); return
    lines = "\n".join(f"• <code>{r['user_id']}</code>" for r in rows)
    await message.answer(f"👥 <b>Saved by</b>\n{lines}")


@router.message(Command("topfavs"))
async def cmd_topfavs(message: Message):
    if not await _req_admin(message): return
    rows = db.query_all(
        "SELECT p.code, COUNT(*) c FROM favorites f JOIN posts p ON p.id=f.post_id "
        "GROUP BY p.id ORDER BY c DESC LIMIT 10")
    if not rows:
        await message.answer("No favorites."); return
    uname = await posting.get_bot_username()
    lines = [f'• <a href="https://t.me/{uname}?start={r["code"]}"><code>{r["code"]}</code></a> '
             f'— {r["c"]} saves' for r in rows]
    await message.answer("❤️ <b>Most saved</b>\n" + "\n".join(lines),
                         disable_web_page_preview=True)
