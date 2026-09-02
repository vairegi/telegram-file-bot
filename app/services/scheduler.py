"""IST-slot scheduler: single indexed queue read per minute tick.

Schedule stored as JSON in settings['schedule']:
    {"slots": ["07:00", "19:00"], "batch": 15, "tz": "Asia/Kolkata"}

The tick runs once per minute; if the current IST HH:MM matches a slot and
we haven't fired it today, publish `batch` covers via posting.publish_batch.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import posting, repo

log = logging.getLogger("scheduler")

_task: Optional[asyncio.Task] = None
IST = timezone(timedelta(hours=5, minutes=30))


async def get_schedule() -> Optional[dict]:
    return await repo.get_setting_json("schedule", None)


async def set_schedule(slots: list[str], batch: int) -> None:
    await repo.set_setting_json("schedule", {
        "slots": slots, "batch": int(batch), "tz": "Asia/Kolkata",
    })


async def clear_schedule() -> None:
    await repo.set_setting("schedule", None)


async def _mark_slot_fired(slot: str, day: str) -> None:
    await repo.set_setting(f"sched_fired:{day}:{slot}", "1")


async def _slot_already_fired(slot: str, day: str) -> bool:
    return (await repo.get_setting(f"sched_fired:{day}:{slot}")) == "1"


async def _tick(bot):
    cfg = await get_schedule()
    if not cfg:
        return
    slots = cfg.get("slots") or []
    batch = int(cfg.get("batch") or 1)
    now = datetime.now(IST)
    hhmm = now.strftime("%H:%M")
    day = now.strftime("%Y-%m-%d")
    if hhmm in slots and not await _slot_already_fired(hhmm, day):
        await _mark_slot_fired(hhmm, day)
        log.info("[scheduler] firing slot %s (batch=%s)", hhmm, batch)
        try:
            await posting.publish_batch(bot, batch)
        except Exception:
            log.exception("[scheduler] publish_batch failed")


async def _loop(bot):
    while True:
        try:
            await _tick(bot)
        except Exception:
            log.exception("[scheduler] tick error")
        await asyncio.sleep(60)


def start(bot):
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(bot))
        log.info("[scheduler] started")


def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
