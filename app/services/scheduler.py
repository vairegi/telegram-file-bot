"""In-process scheduler replacing Supabase pg_cron.

Runs recurring jobs inside the same process (Render free tier sleeps, so keep
it externally warm — see README). Jobs:
  * drip            — publish queued posts on a fixed loop.
  * schedule_posts  — fire due scheduled_posts.
  * schedule_bcast  — promote scheduled broadcasts.
  * autodelete      — delete messages whose TTL expired.
"""
from __future__ import annotations

import asyncio
import json

from .. import db
from ..utils import now_iso, parse_duration_ms
from . import repo, posting
from .tg import delete_message


async def _drip_once(limit: int = 1) -> int:
    queued = db.query_all(
        "SELECT * FROM posts WHERE posted_at IS NULL AND is_deleted = 0 "
        "ORDER BY position ASC LIMIT ?",
        (limit,),
    )
    done = 0
    for post in queued:
        try:
            await posting.publish_post_to_mains(post)
            done += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[drip] post {post['id']} failed: {exc}")
    return done


async def _schedule_posts_once(batch: int = 5) -> int:
    due = db.query_all(
        "SELECT * FROM scheduled_posts WHERE status = 'pending' AND scheduled_for <= ? "
        "ORDER BY scheduled_for ASC LIMIT ?",
        (now_iso(), batch),
    )
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
            db.execute(
                "UPDATE scheduled_posts SET status='done', processed_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            db.execute(
                "UPDATE scheduled_posts SET status='failed', last_error=?, processed_at=? WHERE id=?",
                (str(exc)[:500], now_iso(), row["id"]),
            )
    return processed


async def _publish_oneshot(chat_id: int, media: dict, caption: str | None) -> None:
    from .tg import send_photo, send_video, send_document, send_audio, send_message
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


async def _autodelete_once(batch: int = 50) -> int:
    due = db.query_all(
        "SELECT * FROM pending_deletions WHERE delete_at <= ? LIMIT ?",
        (now_iso(), batch),
    )
    count = 0
    for row in due:
        try:
            await delete_message(int(row["chat_id"]), int(row["message_id"]))
        except Exception:  # noqa: BLE001
            pass
        db.execute("DELETE FROM pending_deletions WHERE id = ?", (row["id"],))
        count += 1
    return count


async def scheduler_loop() -> None:
    """Run forever; each job has its own period."""
    while True:
        try:
            await asyncio.gather(
                _drip_once(limit=1),
                _schedule_posts_once(batch=5),
                _autodelete_once(batch=50),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] tick error: {exc}")
        await asyncio.sleep(15)
