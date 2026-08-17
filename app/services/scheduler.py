"""In-process scheduler replacing Supabase pg_cron.

Drip modes:
  * Slot mode   — /setschedule 07:00,19:00 15 → at 07:00 IST post 15, at 19:00 IST post 15.
  * Legacy mode — /setschedule 5 2 → every 5 min post a batch of 2.
/dripnow [n] bypasses the schedule and posts the next n queued posts.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json

from .. import db
from ..utils import now_iso
from . import backfill, posting, repo
from .tg import (delete_message, send_audio, send_document,
                 send_message, send_photo, send_video)

IST = _dt.timedelta(hours=5, minutes=30)


def _drip_config() -> dict:
    v = repo.get_setting_json("drip_config", default={"minutes": 5, "batch": 1})
    return v if isinstance(v, dict) else {"minutes": 5, "batch": 1}


def _slot_config() -> dict | None:
    v = repo.get_setting_json("drip_schedule", None)
    if isinstance(v, dict) and v.get("slots"):
        return v
    return None


def _paused() -> bool:
    return repo.get_setting_bool("posting_paused") or repo.get_setting_bool("schedule_paused")


async def _publish_n(count: int) -> list[int]:
    """Publish the next `count` queued posts. Returns posted positions."""
    queued = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NULL AND is_deleted=0 "
        "ORDER BY position ASC LIMIT ?", (max(1, count),))
    posted: list[int] = []
    for post in queued:
        try:
            await posting.publish_post_to_mains(post)
            posted.append(int(post["position"]))
        except Exception as exc:
            print(f"[drip] post {post['id']} failed: {exc}")
    return posted


async def _drip_once() -> int:
    if _paused():
        return 0
    return len(await _publish_n(int(_drip_config().get("batch", 1))))


async def _slot_tick(now_ist: tuple[str, str] | None = None) -> int:
    cfg = _slot_config()
    if not cfg or _paused():
        return 0
    if now_ist is None:
        ist = _dt.datetime.now(_dt.timezone.utc) + IST
        now_ist = (ist.strftime("%H:%M"), ist.date().isoformat())
    hhmm, today = now_ist
    fired = repo.get_setting_json("drip_fired", {"date": "", "slots": []})
    if not isinstance(fired, dict):
        fired = {"date": "", "slots": []}
    if fired.get("date") != today:
        fired = {"date": today, "slots": []}
    total = 0
    for slot in cfg.get("slots", []):
        if slot == hhmm and slot not in fired.get("slots", []):
            nums = await _publish_n(int(cfg.get("per_slot", 1)))
            fired.setdefault("slots", []).append(slot)
            repo.set_setting("drip_fired", fired)
            total += len(nums)
            if nums:
                print(f"[drip-slot] {slot} IST fired: {nums}")
    return total


async def _schedule_posts_once(batch=5) -> int:
    due = db.query_all(
        "SELECT * FROM scheduled_posts WHERE status='pending' AND scheduled_for<=? "
        "ORDER BY scheduled_for ASC LIMIT ?", (now_iso(), batch))
    processed = 0
    for row in due:
        try:
            if row["kind"] == "code" and row.get("post_code"):
                post = repo.get_post_by_code(row["post_code"])
                if post:
                    await posting.publish_post_to_mains(post)
            elif row["kind"] == "oneshot":
                media = json.loads(row.get("media") or "{}")
                cap = row.get("caption")
                for ch in repo.get_main_channels():
                    await _publish_oneshot(int(ch["telegram_chat_id"]), media, cap)
            db.execute("UPDATE scheduled_posts SET status='done', processed_at=? WHERE id=?",
                       (now_iso(), row["id"]))
            processed += 1
        except Exception as exc:
            db.execute("UPDATE scheduled_posts SET status='failed', last_error=?, processed_at=? WHERE id=?",
                       (str(exc)[:500], now_iso(), row["id"]))
    return processed


async def _publish_oneshot(chat_id, media, caption):
    kind = media.get("kind")
    fid = media.get("file_id")
    cap = {"caption": caption} if caption else {}
    if kind == "photo" and fid:
        await send_photo(chat_id, fid, **cap)
    elif kind == "video" and fid:
        await send_video(chat_id, fid, **cap)
    elif kind == "document" and fid:
        await send_document(chat_id, fid, **cap)
    elif kind == "audio" and fid:
        await send_audio(chat_id, fid, **cap)
    elif caption:
        await send_message(chat_id, caption)


async def _autodelete_once(batch=50) -> int:
    due = db.query_all("SELECT * FROM pending_deletions WHERE delete_at<=? LIMIT ?",
                       (now_iso(), batch))
    n = 0
    for row in due:
        try:
            await delete_message(int(row["chat_id"]), int(row["message_id"]))
        except Exception:
            pass
        db.execute("DELETE FROM pending_deletions WHERE id=?", (row["id"],))
        n += 1
    return n


async def _backup_once(batch=5) -> int:
    if repo.get_setting_bool("backup_paused"):
        return 0
    backups = repo.get_backup_channels()
    if not backups:
        return 0
    done = 0
    for ch in backups:
        cid = int(ch["telegram_chat_id"])
        pending = db.query_all(
            "SELECT p.* FROM posts p WHERE p.is_deleted=0 "
            "AND NOT EXISTS (SELECT 1 FROM backup_copies b WHERE b.post_id=p.id AND b.backup_chat_id=?) "
            "ORDER BY p.position ASC LIMIT ?", (cid, batch))
        for post in pending:
            await posting.mirror_post_to_backup(post, cid)
            done += 1
    return done


async def _backfill_once() -> int:
    job = backfill.get_running_job()
    if not job:
        return 0
    await backfill.run_chunk(chunk_size=5)
    return 1


async def scheduler_loop() -> None:
    tick = 0
    while True:
        tick += 1
        try:
            await asyncio.gather(
                _schedule_posts_once(batch=5),
                _autodelete_once(batch=50),
                _backfill_once(),
                _slot_tick(),
                return_exceptions=True)
            if _slot_config() is None:
                drip_minutes = max(1, int(_drip_config().get("minutes", 5)))
                if tick % max(1, drip_minutes * 4) == 0:
                    await _drip_once()
            if tick % 8 == 0:
                await _backup_once(batch=5)
        except Exception as exc:
            print(f"[scheduler] tick error: {exc}")
        await asyncio.sleep(15)
