"""Force-subscribe gate."""
from __future__ import annotations

from .. import db
from ..utils import now_iso
from . import repo
from .tg import get_chat, get_chat_member


async def _is_member(chat_id: int, user_id: int) -> bool:
    try:
        res = await get_chat_member(chat_id=chat_id, user_id=user_id)
        status = res.get("status") if isinstance(res, dict) else None
        return status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


def mark_join_requested(user_id: int, chat_id: int) -> None:
    db.execute(
        "INSERT INTO fsub_satisfied (user_id, channel_chat_id, satisfied_at) VALUES (?,?,?) "
        "ON CONFLICT DO NOTHING",
        (user_id, chat_id, now_iso()),
    )


async def _resolve_title(chat_id: int, cached) -> str:
    if cached:
        return cached
    try:
        chat = await get_chat(chat_id=chat_id)
        title = chat.get("title") if isinstance(chat, dict) else None
        if title:
            db.execute("UPDATE channels SET title=? WHERE telegram_chat_id=?", (title, chat_id))
            return title
    except Exception:
        pass
    return str(chat_id)


async def unmet_forcesubs(user_id: int) -> list[dict]:
    channels = repo.get_forcesub_channels()
    if not channels:
        return []
    satisfied = {int(r["channel_chat_id"]) for r in db.query_all(
        "SELECT channel_chat_id FROM fsub_satisfied WHERE user_id=?", (user_id,))}
    out = []
    for c in channels:
        cid = int(c["telegram_chat_id"])
        if cid in satisfied:
            continue
        if await _is_member(cid, user_id):
            continue
        title = await _resolve_title(cid, c.get("title"))
        url = c.get("invite_link") or f"https://t.me/c/{str(cid).replace('-100','',1)}"
        out.append({"chat_id": cid, "url": url, "title": title})
    return out


def build_join_keyboard(unmet: list[dict], retry_code: str, bot_username: str) -> dict:
    rows = [[{"text": f"📢 Join {c['title']}", "url": c["url"]}] for c in unmet]
    rows.append([{"text": "✅ I've Joined — Try Again",
                  "url": f"https://t.me/{bot_username}?start={retry_code}"}])
    return {"inline_keyboard": rows}
