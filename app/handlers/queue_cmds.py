"""Queue-control commands: /queue /queueinfo /peek /whereami /find /dripnow
/dripstop /setschedule /scheduleoff /pauseposting /resumeposting
/skip /skip_range /unskip /jumpto /queue_reset /repost /preview /deletepost
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import posting, repo, scheduler as sched
from ..utils import (esc, first_line, parse_channel_id, parse_hash_number,
                     parse_tme_link, source_link, to_int)
from .setup_cmds import _reject_non_admin, _reject_non_super

log = logging.getLogger("queue_cmds")
router = Router(name="queue_cmds")

_drip_task = None
_drip_stop = False


# ------------------------- inspection -------------------------
@router.message(Command("queue"))
async def cmd_queue(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    rows = repo.predicted_number_of_next(10)
    if not rows:
        await msg.reply("💤 Queue is empty.")
        return
    lines = ["<b>Next up (predicted #N)</b>"]
    for r in rows:
        lines.append(
            f"{r['predicted_number']}. <b>#{r['predicted_number']}</b> — "
            f"{esc(first_line(r.get('caption'), 50))}")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("queueinfo"))
async def cmd_queueinfo(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    pending = repo.queued_cover_count()
    published = repo.published_cover_count()
    covers = repo.total_cover_count()
    files = repo.total_file_count()
    last_n = repo.highest_post_number()
    nxt = last_n + 1 if pending else None
    cfg = sched.get_schedule()
    sched_txt = "off"
    if cfg:
        sched_txt = f"{','.join(cfg.get('slots', []))} IST × {cfg.get('batch')}"
    await msg.reply(
        f"<b>Queue</b>\n"
        f"🖼 Covers total: {covers}\n"
        f"📄 Files total: {files}\n"
        f"✅ Published: {published} (last <b>#{last_n}</b>)\n"
        f"⏳ Pending: {pending}\n"
        f"▶️ Next: <b>#{nxt}</b>\n"
        f"🕒 Schedule: {sched_txt}\n"
        f"⏸ Paused: {posting._paused()}",
        parse_mode="HTML",
    )


@router.message(Command("peek"))
async def cmd_peek(msg: Message) -> None:
    """Show next N pending covers — title + predicted #N only."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    n = to_int(parts[1], 10) if len(parts) > 1 else 10
    n = max(1, min(50, int(n)))
    rows = repo.predicted_number_of_next(n)
    if not rows:
        await msg.reply("💤 Queue is empty.")
        return
    lines = [f"<b>Next {len(rows)} in queue</b>"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. #{r['predicted_number']} {esc(first_line(r.get('caption'), 60))}")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("whereami"))
async def cmd_whereami(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    last_n = repo.highest_post_number()
    nxt = repo.predicted_number_of_next(1)
    nxt_txt = f"#{nxt[0]['predicted_number']}" if nxt else "—"
    mains = repo.get_main_channels()
    dbs = repo.get_database_channels()
    await msg.reply(
        f"📍 <b>State</b>\n"
        f"Last published: <b>#{last_n}</b>\n"
        f"Next to publish: <b>{nxt_txt}</b>\n"
        f"DB channels: {len(dbs)}\n"
        f"Main channels: {len(mains)}\n"
        f"Spoiler: {'ON' if repo.get_setting_bool('spoiler', True) else 'OFF'}\n"
        f"Protect: {'ON' if repo.get_setting_bool('protect_content') else 'OFF'}\n"
        f"Paused: {'YES' if posting._paused() else 'no'}",
        parse_mode="HTML",
    )


@router.message(Command("find"))
async def cmd_find(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    q = (msg.text or "").split(maxsplit=1)
    if len(q) < 2:
        await msg.reply("Usage: <code>/find &lt;text&gt;</code>", parse_mode="HTML")
        return
    rows = repo.find_by_caption(q[1])
    if not rows:
        await msg.reply("💤 No matches.")
        return
    lines = [f"<b>{len(rows)} match(es)</b>"]
    for r in rows:
        n = r.get("post_number") or "?"
        lines.append(f"• <b>#{n}</b> {esc(first_line(r.get('caption'), 60))}\n"
                     f"  {source_link(r['source_chat_id'], r['source_message_id'])}")
    await msg.reply("\n".join(lines), parse_mode="HTML",
                    disable_web_page_preview=True)


# ------------------------- drip -------------------------
@router.message(Command("dripnow"))
async def cmd_dripnow(msg: Message, bot: Bot) -> None:
    global _drip_task, _drip_stop
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    n = to_int(parts[1], 1) if len(parts) > 1 else 1
    n = max(1, min(100, int(n)))
    if _drip_task and not _drip_task.done():
        await msg.reply("⚠️ A drip is already running. /dripstop to cancel.")
        return

    async def _run():
        global _drip_stop
        _drip_stop = False
        for i in range(n):
            if _drip_stop:
                break
            cover = await posting.publish_next(bot)
            if not cover:
                break
        done_txt = "🛑 stopped early" if _drip_stop else "✅ done"
        try:
            await bot.send_message(msg.chat.id, f"📤 dripnow: {done_txt} — published {i + (0 if _drip_stop else 1) if i >= 0 else 0} cover(s).")
        except Exception:
            pass

    _drip_task = __import__("asyncio").create_task(_run())
    await msg.reply(f"🚀 Drip started: {n} cover(s). Use /dripstop to cancel.")


@router.message(Command("dripstop"))
async def cmd_dripstop(msg: Message) -> None:
    global _drip_stop
    if await _reject_non_admin(msg):
        return
    _drip_stop = True
    await msg.reply("🛑 Stop requested — exits after current cover.")


# ------------------------- schedule -------------------------
@router.message(Command("setschedule"))
async def cmd_setschedule(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Usage: <code>/setschedule 07:00,19:00 15</code>\n"
                        "(IST times comma-separated, then batch size)",
                        parse_mode="HTML")
        return
    slots = [s.strip() for s in parts[1].split(",") if s.strip()]
    batch = to_int(parts[2], 1)
    if not slots or not batch:
        await msg.reply("❌ Bad slots or batch.")
        return
    sched.set_schedule(slots, int(batch))
    await msg.reply(f"✅ Schedule set: {', '.join(slots)} IST × {batch}")


@router.message(Command("scheduleoff"))
async def cmd_scheduleoff(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    sched.clear_schedule()
    await msg.reply("🛑 Schedule cleared.")


# ------------------------- pause -------------------------
@router.message(Command("pauseposting"))
async def cmd_pause(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_setting("posting_paused", "1")
    await msg.reply("⏸ Posting paused.")


@router.message(Command("resumeposting"))
async def cmd_resume(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    repo.set_setting("posting_paused", None)
    await msg.reply("▶️ Posting resumed.")


# ------------------------- skip / jump -------------------------
def _first_main_chat_id() -> int:
    mains = repo.get_main_channels()
    return int(mains[0]["chat_id"]) if mains else 0


@router.message(Command("skip"))
async def cmd_skip(msg: Message) -> None:
    """Mark the next N pending covers (or everything up to a link) as already
    published, so the queue jumps past them."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/skip #N</code> or <code>/skip &lt;t.me/c/link&gt;</code>",
                        parse_mode="HTML")
        return
    arg = parts[1]
    main_cid = _first_main_chat_id()

    if "t.me/" in arg:
        link = parse_tme_link(arg)
        if not link or not link[0]:
            await msg.reply("❌ Use a full https://t.me/c/… link.")
            return
        cid, _, mid = link
        affected = repo.skip_up_to_source(int(cid), int(mid), main_cid)
        await msg.reply(f"⏭ Skipped {affected} cover(s) up to msg <code>{mid}</code>.",
                        parse_mode="HTML")
        return

    n = parse_hash_number(arg)
    if not n:
        await msg.reply("❌ Bad number.")
        return
    affected = repo.skip_first_n(int(n), main_cid)
    await msg.reply(f"⏭ Skipped the next {affected} pending cover(s).\n"
                    f"Next publish will be <b>#{repo.highest_post_number() + 1}</b>.",
                    parse_mode="HTML")


@router.message(Command("skip_range"))
async def cmd_skip_range(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or "-" not in parts[1]:
        await msg.reply("Usage: <code>/skip_range #100-#200</code>", parse_mode="HTML")
        return
    lo_s, hi_s = parts[1].split("-", 1)
    lo = parse_hash_number(lo_s)
    hi = parse_hash_number(hi_s)
    if not lo or not hi or lo > hi:
        await msg.reply("❌ Bad range.")
        return
    count = 0
    main_cid = _first_main_chat_id()
    for n in range(int(lo), int(hi) + 1):
        row = repo.get_post_by_number(n)
        if row and row.get("published_at") is None:
            repo.mark_published(int(row["id"]), main_cid, 0)
            count += 1
    await msg.reply(f"⏭ Marked {count} cover(s) in range #{lo}–#{hi} as published.")


@router.message(Command("unskip"))
async def cmd_unskip(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/unskip #N</code>", parse_mode="HTML")
        return
    n = parse_hash_number(parts[1])
    if not n:
        await msg.reply("❌ Bad #N.")
        return
    row = repo.unskip_by_number(int(n))
    if not row:
        await msg.reply(f"❌ No post #{n} found.")
        return
    await msg.reply(f"♻️ #{n} is back in the queue (unpublished).")


@router.message(Command("jumpto"))
async def cmd_jumpto(msg: Message) -> None:
    """Force queue BACK to #N: unpublish #N and every published cover after it."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Usage: <code>/jumpto #N</code>", parse_mode="HTML")
        return
    n = parse_hash_number(parts[1])
    if not n:
        await msg.reply("❌ Bad #N.")
        return
    count = repo.jumpto_number(int(n))
    await msg.reply(f"⏪ Queue jumped back to <b>#{n}</b> — "
                    f"{count} published cover(s) returned to the queue.",
                    parse_mode="HTML")


@router.message(Command("queue_reset"))
async def cmd_queue_reset(msg: Message) -> None:
    if await _reject_non_super(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or parts[1] != "CONFIRM":
        await msg.reply("⚠️ This unpublishes EVERY cover (queue restarts from #1).\n"
                        "Type: <code>/queue_reset CONFIRM</code>", parse_mode="HTML")
        return
    count = repo.queue_reset()
    await msg.reply(f"🧨 Queue reset — {count} cover(s) unpublished. "
                    f"Next publish starts from <b>#1</b>.", parse_mode="HTML")


# ------------------------- repost / preview / deletepost -------------------------
def _resolve_post(msg_args: str):
    """Accept #N, N, or a code."""
    s = (msg_args or "").strip()
    if not s:
        return None
    n = parse_hash_number(s)
    if n:
        return repo.get_post_by_number(int(n))
    return repo.get_post_by_code(s)


@router.message(Command("repost"))
async def cmd_repost(msg: Message, bot: Bot) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Usage: <code>/repost #N</code> or <code>/repost &lt;code&gt;</code>",
                        parse_mode="HTML")
        return
    cover = _resolve_post(parts[1])
    if not cover or cover.get("kind") != "cover":
        await msg.reply("❌ No such cover.")
        return
    results = await posting.publish_cover_to_mains(bot, cover)
    ok = [r for r in results if r.get("ok")]
    if ok:
        await msg.reply(f"✅ Reposted #{cover.get('post_number') or '?'}.")
    else:
        await msg.reply(f"❌ Failed: <code>{esc(posting.LAST_PUBLISH_ERROR)}</code>",
                        parse_mode="HTML")


@router.message(Command("preview"))
async def cmd_preview(msg: Message, bot: Bot) -> None:
    """Forward the cover (no publish, no queue change) to the admin's DM."""
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Usage: <code>/preview #N</code>", parse_mode="HTML")
        return
    cover = _resolve_post(parts[1])
    if not cover:
        await msg.reply("❌ No such post.")
        return
    try:
        await bot.copy_message(
            chat_id=msg.from_user.id,
            from_chat_id=int(cover["source_chat_id"]),
            message_id=int(cover["source_message_id"]),
        )
    except Exception as e:
        await msg.reply(f"❌ Preview failed: <code>{esc(str(e))}</code>",
                        parse_mode="HTML")


@router.message(Command("deletepost"))
async def cmd_deletepost(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Usage: <code>/deletepost #N</code> or <code>/deletepost &lt;code&gt;</code>",
                        parse_mode="HTML")
        return
    arg = parts[1].strip()
    n = parse_hash_number(arg)
    ok = repo.delete_post_by_number(int(n)) if n else repo.delete_post_by_code(arg)
    if ok:
        await msg.reply("🗑 Post removed from queue (kind → skip).")
    else:
        await msg.reply("❌ No such post.")
