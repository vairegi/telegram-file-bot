"""Force-subscribe gate: users must join configured channels before Get File.

  /fsub <chat_id> <invite_link>   → add a required channel
  /fsublist                       → list with title-as-link
  /fsubremove <chat_id>           → remove

Storage: settings['fsub_channels'] = [{"chat_id": -100…, "link": "https://t.me/+…",
                                       "title": "…"}]
Membership check: bot.get_chat_member(channel, user) — bot must be admin in
the fsub channel. 'member'/'administrator'/'creator' pass; 'left'/'kicked'
fail; any API error (e.g. bot not in channel) FAILS OPEN with a log warning
so a misconfigured channel never blocks all users.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from . import repo

log = logging.getLogger("fsub")

_PASS_STATUSES = {"member", "administrator", "creator", "owner", "restricted"}


async def list_fsub() -> List[dict]:
    return (await repo.get_setting_json("fsub_channels", [])) or []


async def add_fsub(chat_id: int, invite_link: str, title: Optional[str] = None) -> None:
    rows = await list_fsub()
    rows = [r for r in rows if int(r.get("chat_id") or 0) != int(chat_id)]
    rows.append({"chat_id": int(chat_id), "link": invite_link, "title": title or ""})
    await repo.set_setting_json("fsub_channels", rows)


async def remove_fsub(chat_id: int) -> bool:
    rows = await list_fsub()
    new = [r for r in rows if int(r.get("chat_id") or 0) != int(chat_id)]
    if len(new) == len(rows):
        return False
    await repo.set_setting_json("fsub_channels", new)
    return True


async def set_title(chat_id: int, title: str) -> None:
    rows = await list_fsub()
    for r in rows:
        if int(r.get("chat_id") or 0) == int(chat_id):
            r["title"] = title
    await repo.set_setting_json("fsub_channels", rows)


async def unjoined_channels(bot, user_id: int) -> List[dict]:
    """Return the subset of fsub channels the user has NOT joined."""
    missing = []
    for ch in await list_fsub():
        cid = int(ch.get("chat_id") or 0)
        try:
            m = await bot.get_chat_member(chat_id=cid, user_id=int(user_id))
            status = getattr(m, "status", "") or ""
            status = getattr(status, "value", status)  # enum → str
            if str(status) not in _PASS_STATUSES:
                missing.append(ch)
        except Exception as e:
            log.warning("fsub check failed chat=%s user=%s: %s (failing open)",
                        cid, user_id, e)
    return missing


async def check_or_gate(bot, user_id: int, code: str) -> bool:
    """True = user may proceed. False = gate message sent (with Retry button)."""
    missing = await unjoined_channels(bot, user_id)
    if not missing:
        return True
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from ..utils import esc
    rows = []
    for ch in missing:
        label = ch.get("title") or "📢 Join Channel"
        rows.append([InlineKeyboardButton(text=label, url=ch.get("link") or "https://t.me/")])
    rows.append([InlineKeyboardButton(text="🔄 Retry", callback_data=f"fsub_retry:{code}")])
    await bot.send_message(
        chat_id=user_id,
        text=("🔒 <b>Join required</b>\n\n"
              "To receive the files, please join the channel(s) below, "
              "then tap <b>🔄 Retry</b>."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return False
