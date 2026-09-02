"""Backup / mirror engine (v2.9).

Mirrors every non-skipped post from the DB channel(s) to registered backup
channels via bot.copy_message. Progress is per-(backup_chat_id, db_chat_id,
source_message_id) so each backup channel tracks its own cursor and a new
backup starts from message #1 without disturbing the others.

Commands that drive this module:
  /backup <chan>    → full catch-up pass on that backup channel
  /backup10 <chan>  → mirror at most 10 pending messages (smoke test)
  /pausebackup      → stop the auto-loop + block manual runs
  /resumebackup     → resume; loop always catches up from stored progress,
                      so anything posted while paused is picked up.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from . import repo

log = logging.getLogger("backup")


@dataclass
class RunState:
    backup_chat_id: int
    running: bool = False
    started_at: float = 0.0
    mirrored: int = 0
    errors: int = 0
    last_error: str = ""
    stop: bool = False


_active: dict[int, RunState] = {}


def is_running(backup_chat_id: int) -> bool:
    s = _active.get(int(backup_chat_id))
    return bool(s and s.running)


def state(backup_chat_id: int) -> Optional[RunState]:
    return _active.get(int(backup_chat_id))


def stop_all() -> None:
    for s in _active.values():
        s.stop = True


def stop_one(backup_chat_id: int) -> bool:
    s = _active.get(int(backup_chat_id))
    if not s or not s.running:
        return False
    s.stop = True
    return True


async def _mirror_one(bot, backup_chat_id: int, db_chat_id: int,
                      source_message_id: int) -> tuple[bool, Optional[int], str]:
    try:
        res = await bot.copy_message(
            chat_id=int(backup_chat_id),
            from_chat_id=int(db_chat_id),
            message_id=int(source_message_id),
        )
        mid = getattr(res, "message_id", None) or getattr(res, "id", None)
        return (True, int(mid) if mid else None, "")
    except Exception as e:
        return (False, None, f"{type(e).__name__}: {e}")


def _flood_wait_seconds(err: str) -> Optional[int]:
    m = re.search(r"(?:wait of |retry after |Flood wait of )(\d+)", err, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if "FloodWait" in err or "Too Many Requests" in err or "retry_after" in err.lower():
        return 5
    return None


async def run_backup(bot, backup_chat_id: int, limit: int = 0,
                     admin_chat_id: Optional[int] = None) -> dict:
    """Catch up one backup channel. limit=0 means mirror everything pending."""
    backup_chat_id = int(backup_chat_id)
    if is_running(backup_chat_id):
        return {"ok": False, "error": "already_running"}
    if await repo.backup_is_paused():
        return {"ok": False, "error": "paused"}

    s = RunState(backup_chat_id=backup_chat_id, running=True,
                 started_at=time.time())
    _active[backup_chat_id] = s

    try:
        all_msgs = await repo.all_db_source_messages()
        mirrored_set = await repo.backup_mirrored_set(backup_chat_id)
        pending = [m for m in all_msgs
                   if (int(m["source_chat_id"]), int(m["source_message_id"]))
                   not in mirrored_set]

        target = len(pending) if limit == 0 else min(limit, len(pending))
        if target == 0:
            return {"ok": True, "mirrored": 0, "already_up_to_date": True}

        for i, m in enumerate(pending[:target], start=1):
            if s.stop:
                break
            if await repo.backup_is_paused():
                s.last_error = "paused mid-run"
                break

            db_cid = int(m["source_chat_id"])
            smid = int(m["source_message_id"])
            while True:
                ok, tmid, err = await _mirror_one(bot, backup_chat_id, db_cid, smid)
                if ok:
                    try:
                        await repo.backup_record(backup_chat_id, db_cid, smid, tmid)
                    except Exception:
                        pass
                    s.mirrored += 1
                    break
                wait_s = _flood_wait_seconds(err)
                if wait_s is not None:
                    log.warning("[backup] flood wait %ss", wait_s)
                    if admin_chat_id and wait_s >= 10:
                        try:
                            await bot.send_message(admin_chat_id,
                                                   f"⏳ Backup FloodWait: pausing {wait_s}s…")
                        except Exception:
                            pass
                    await asyncio.sleep(min(wait_s + 1, 90))
                    continue
                s.errors += 1
                s.last_error = err
                log.warning("[backup] mirror mid=%s -> %s failed: %s",
                            smid, backup_chat_id, err)
                break

            # ~3 messages/sec — gentle on Bot API limits.
            await asyncio.sleep(0.35)

            if admin_chat_id and s.mirrored and s.mirrored % 50 == 0:
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"💾 mirroring to <code>{backup_chat_id}</code>: "
                        f"{s.mirrored}/{target}  err={s.errors}",
                        parse_mode="HTML")
                except Exception:
                    pass

        return {"ok": True, "mirrored": s.mirrored, "errors": s.errors,
                "last_error": s.last_error}
    finally:
        s.running = False


async def auto_loop(bot) -> None:
    """Background sweeper — every 60s mirrors new DB posts into each backup
    channel (unless paused)."""
    while True:
        try:
            if not await repo.backup_is_paused():
                for ch in await repo.get_backup_channels():
                    cid = int(ch["chat_id"])
                    if not is_running(cid):
                        try:
                            await run_backup(bot, cid, limit=0)
                        except Exception:
                            log.exception("[backup] auto pass failed for %s", cid)
        except Exception:
            log.exception("[backup] auto_loop error")
        await asyncio.sleep(60)


_auto_task: Optional[asyncio.Task] = None


def start_auto(bot) -> None:
    global _auto_task
    if _auto_task is None or _auto_task.done():
        _auto_task = asyncio.create_task(auto_loop(bot))
        log.info("[backup] auto_loop started")


def stop_auto() -> None:
    global _auto_task
    if _auto_task and not _auto_task.done():
        _auto_task.cancel()
    _auto_task = None
