"""In-process scheduler (IST clock-time slots ONLY).

- /setschedule 07:00,19:00 15  -> at 07:00 IST post 15 covers; at 19:00 IST post 15 covers.
- Interval form (e.g. `/setschedule 5 2`) is REMOVED per spec.
- /dripnow [N] bypasses the schedule and publishes N covers immediately (default 1).

The loop wakes every ~20s. Each IST slot fires at most once per calendar day
(guarded by drip_slot_fired = {"YYYY-MM-DD": ["HH:MM", ...]}).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from typing import List, Optional, Tuple

from aiogram import Bot

from . import repo, posting

log = logging.getLogger("scheduler")

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
SLOT_KEY = "drip_schedule"        # {"slots":[{"time":"07:00","batch":15}, ...]}
FIRED_KEY = "drip_slot_fired"     # {"YYYY-MM-DD":["07:00", ...]}
_SLOT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --------------------- parsing / storage --------------------------
def parse_setschedule(args: str) -> Optional[dict]:
    """Parse '<t1,t2,...> <batch>' into {'slots':[{'time':HH:MM,'batch':N}, ...]}.

    Interval form (e.g. '5 2') is rejected — clock-time only.
    """
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) < 2:
        return None
    times_raw, batch_raw = parts[0], parts[1]
    try:
        batch = int(batch_raw)
        if batch <= 0:
            return None
    except Exception:
        return None
    times = [t.strip() for t in times_raw.split(",") if t.strip()]
    slots = []
    seen = set()
    for t in times:
        if not _SLOT_RE.match(t):
            return None
        # normalize to HH:MM (two-digit hour)
        hh, mm = t.split(":")
        norm = f"{int(hh):02d}:{mm}"
        if norm in seen:
            continue
        seen.add(norm)
        slots.append({"time": norm, "batch": batch})
    if not slots:
        return None
    return {"slots": slots}


def format_setschedule_reply(cfg: dict) -> str:
    slots = cfg.get("slots") or []
    if not slots:
        return "❌ No slots configured."
    parts = [f"{s['time']} × {s['batch']}" for s in slots]
    total = sum(int(s.get("batch") or 0) for s in slots)
    return f"✅ Schedule saved: {', '.join(parts)} (IST). Total: {total} post(s)/day."


def get_schedule() -> Optional[dict]:
    cfg = repo.get_setting_json(SLOT_KEY, None)
    if isinstance(cfg, dict) and cfg.get("slots"):
        return cfg
    return None


def set_schedule(cfg: dict) -> None:
    repo.set_setting_json(SLOT_KEY, cfg)
    repo.set_setting_json(FIRED_KEY, {})  # reset the fired-today ledger


def clear_schedule() -> None:
    repo.set_setting(SLOT_KEY, None)
    repo.set_setting(FIRED_KEY, None)


# --------------------- fired ledger -------------------------------
def _today_ist(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(tz=IST)
    return now.astimezone(IST).strftime("%Y-%m-%d")


def _now_hhmm_ist(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(tz=IST)
    return now.astimezone(IST).strftime("%H:%M")


def _fired_today() -> List[str]:
    fired = repo.get_setting_json(FIRED_KEY, {}) or {}
    if not isinstance(fired, dict):
        return []
    return list(fired.get(_today_ist(), []) or [])


def _mark_fired(slot_time: str) -> None:
    fired = repo.get_setting_json(FIRED_KEY, {}) or {}
    if not isinstance(fired, dict):
        fired = {}
    day = _today_ist()
    lst = list(fired.get(day, []) or [])
    if slot_time not in lst:
        lst.append(slot_time)
    fired[day] = lst
    # Keep only the last 3 days
    for k in list(fired.keys()):
        try:
            d = _dt.datetime.strptime(k, "%Y-%m-%d").date()
            if (_dt.date.today() - d).days > 3:
                del fired[k]
        except Exception:
            del fired[k]
    repo.set_setting_json(FIRED_KEY, fired)


# --------------------- loop ---------------------------------------
def _paused() -> bool:
    return repo.get_setting_bool("posting_paused", False) or repo.get_setting_bool("schedule_paused", False)


async def _run_slot(bot: Bot, batch: int) -> int:
    """Publish `batch` covers respecting the /reset floor if any."""
    published = await posting.publish_batch(bot, int(batch))
    return len(published)


async def tick(bot: Bot) -> Tuple[int, Optional[str]]:
    """One scheduler tick. Returns (published_count, fired_slot_or_None)."""
    cfg = get_schedule()
    if not cfg or _paused():
        return (0, None)
    now = _dt.datetime.now(tz=IST)
    hhmm = _now_hhmm_ist(now)
    fired = set(_fired_today())
    for slot in cfg.get("slots") or []:
        t = slot.get("time")
        if not t or t in fired:
            continue
        if t == hhmm:
            batch = int(slot.get("batch") or 1)
            n = await _run_slot(bot, batch)
            _mark_fired(t)
            log.info("scheduler slot %s fired: published %s cover(s)", t, n)
            return (n, t)
    return (0, None)


async def scheduler_loop(bot: Bot) -> None:
    """Long-running task. Wakes every 20s and fires due IST slots at most once/day."""
    log.info("scheduler loop started")
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(20)
