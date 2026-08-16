"""Resumable backfill — republish stored posts into a channel that missed them."""
from __future__ import annotations

import json

from .. import db
from ..utils import now_iso
from . import repo
from .posting import post_to_main_channel


def get_running_job() -> dict | None:
    return db.query_one("SELECT * FROM backfill_jobs WHERE status='running' ORDER BY id DESC LIMIT 1")


def start_job(chat_ids, from_pos, to_pos, created_by) -> dict:
    total = repo.total_posts()
    if to_pos is None or to_pos > total:
        to_pos = total
    from_pos = max(1, from_pos)
    db.insert(
        "INSERT INTO backfill_jobs (chat_ids, from_pos, to_pos, next_pos, created_by) "
        "VALUES (?,?,?,?,?)",
        (json.dumps(chat_ids), from_pos, to_pos, from_pos, created_by),
    )
    return get_running_job() or {}


def cancel_job() -> bool:
    job = get_running_job()
    if not job:
        return False
    db.execute("UPDATE backfill_jobs SET status='cancelled', updated_at=? WHERE id=?",
               (now_iso(), job["id"]))
    return True


async def run_chunk(chunk_size: int = 5) -> dict | None:
    job = get_running_job()
    if not job:
        return None
    if job["next_pos"] > job["to_pos"]:
        db.execute("UPDATE backfill_jobs SET status='done', updated_at=? WHERE id=?",
                   (now_iso(), job["id"]))
        return job
    chat_ids = json.loads(job["chat_ids"] or "[]")
    take = min(chunk_size, job["to_pos"] - job["next_pos"] + 1)
    batch = db.query_all(
        "SELECT * FROM posts WHERE position BETWEEN ? AND ? AND is_deleted=0 "
        "ORDER BY position ASC LIMIT ?",
        (job["next_pos"], job["to_pos"], take),
    )
    posted = int(job["posted"] or 0)
    skipped = int(job["skipped"] or 0)
    failed = int(job["failed"] or 0)
    next_pos = int(job["next_pos"])
    last_err = job.get("last_error")
    for post in batch:
        for cid in chat_ids:
            exists = db.query_scalar(
                "SELECT 1 FROM post_copies WHERE post_id=? AND target_chat_id=?",
                (post["id"], int(cid)),
            )
            if exists:
                skipped += 1
                continue
            try:
                await post_to_main_channel(post, int(cid))
                posted += 1
            except Exception as exc:
                failed += 1
                last_err = str(exc)[:500]
        next_pos = int(post["position"]) + 1
    status = 'done' if next_pos > job["to_pos"] else 'running'
    db.execute(
        "UPDATE backfill_jobs SET posted=?, skipped=?, failed=?, next_pos=?, last_error=?, "
        "status=?, updated_at=? WHERE id=?",
        (posted, skipped, failed, next_pos, last_err, status, now_iso(), job["id"]),
    )
    return db.query_one("SELECT * FROM backfill_jobs WHERE id=?", (job["id"],))


def status_text(job: dict) -> str:
    total = int(job["to_pos"]) - int(job["from_pos"]) + 1
    done = max(0, min(total, int(job["next_pos"]) - int(job["from_pos"])))
    pct = round(done / total * 100) if total > 0 else 100
    bar = "▓" * max(0, min(10, round(pct / 10))) + "░" * max(0, 10 - round(pct / 10))
    err = f"\n⚠️ {job['last_error']}" if job.get("last_error") else ""
    return (f"♻️ <b>Backfill</b> [{job['status']}]\n"
            f"Range: #{job['from_pos']} → #{job['to_pos']}\n"
            f"{bar}  <b>{pct}%</b> ({done}/{total})\n"
            f"Posted: <b>{job['posted']}</b> · Skipped: {job['skipped']} · Failed: {job['failed']}{err}")
