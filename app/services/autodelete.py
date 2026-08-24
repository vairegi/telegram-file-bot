"""Auto-delete: delivered DM content self-destructs after a configured duration.

/autodelete 8h | 12h | 2m | 30s | 1day | off

Design: the message ids are returned by every send in posting.deliver_to_user
and queued here with an asyncio task per batch (one timer per delivery, not
per file — cheaper). Duration lives in settings as milliseconds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

from . import repo

log = logging.getLogger("autodelete")

DEFAULT_MS = 0  # 0 = disabled


def get_ms() -> int:
    return repo.get_setting_int("autodelete_ms", DEFAULT_MS)


def set_ms(ms: int) -> None:
    if ms and ms > 0:
        repo.set_setting("autodelete_ms", str(int(ms)))
    else:
        repo.set_setting("autodelete_ms", None)


def humanize(ms: int) -> str:
    if not ms:
        return "off"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}day(s)"


def parse_duration_ms(text: str) -> Optional[int]:
    """'30s' '2m' '8h' '12h' '1day' '1d' -> ms. None if unparseable."""
    import re
    m = re.match(r"^\s*(\d+)\s*(s|sec|m|min|h|hr|d|day|days)?\s*$", (text or "").lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "s")
    mult = {"s": 1000, "sec": 1000, "m": 60_000, "min": 60_000,
            "h": 3_600_000, "hr": 3_600_000,
            "d": 86_400_000, "day": 86_400_000, "days": 86_400_000}[unit]
    return n * mult


async def _delete_later(bot, user_id: int, message_ids: list[int], ms: int) -> None:
    try:
        await asyncio.sleep(ms / 1000)
        for mid in message_ids:
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("autodelete task failed")


def schedule(bot, user_id: int, message_ids: Iterable[int]) -> None:
    """Queue deletion of the given messages after the configured duration."""
    ms = get_ms()
    ids = [int(m) for m in message_ids if m]
    if not ms or not ids:
        return
    asyncio.create_task(_delete_later(bot, user_id, ids, ms))
