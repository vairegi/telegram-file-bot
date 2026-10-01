"""v4.9 — /banlist, /verified_users, /banmessage + daily download logging.

v4.9 changes to this module:
  * record_download() — counts how many POSTS a user actually fetched today.
  * build_verified_list() columns are now:
        # · Name · Elapsed · File · Link Type · Count · Tokens left
    (Category removed per owner request). Name is a RichTextUrl linking to the
    user's Telegram profile (tg://user?id=<uid>), not just a bare id.
  * Tokens column comes from services/tokens.py (balance for today's wallet).

Logging happens at REDEMPTION/download time, not in repo.verified_set():
  setup_cmds.py (deep-link) + callbacks.py -> record_verification()
  posting.py (every delivered post)         -> record_download()
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import browse, repo, tokens
from .recommend import parse_tags

log = logging.getLogger("richlists")

IST = ZoneInfo("Asia/Kolkata")
TABLE_CELL_LIMIT = 100
_NAME_MAX = 18

VCOLUMNS = ["#", "Name", "Elapsed", "File", "Link Type", "Count", "Tokens left"]


# ============================================================================
# Rich-table helpers
# ============================================================================
def _cell(text: str, header: bool = False) -> dict:
    c: dict = {"text": browse.rt(text)}
    if header:
        c["is_header"] = True
    return c


def rich_table(headers: list[str], rows: list[list[dict]],
               caption: str = "", striped: bool = True) -> dict:
    cells = [[_cell(h, header=True) for h in headers]] + rows
    blk: dict = {"type": "table", "cells": cells, "is_striped": bool(striped)}
    if caption:
        blk["caption"] = browse.rt(caption)
    return blk


def _wrap(title: str, table_block: dict, note: str = "") -> dict:
    blocks = [browse.block_heading(title, size=2), table_block]
    if note:
        blocks.append(browse.block_paragraph(note))
    return browse.rich_message(blocks)


async def send_rich_or_plain(bot, chat_id: int, rm: dict, plain_html: str,
                             reply_to: int = 0) -> None:
    try:
        payload = {"chat_id": chat_id, "rich_message": rm}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to}
        await browse.api_post(bot, "sendRichMessage", payload)
    except browse.BrowseError as e:
        log.warning("rich table rejected (%s) — falling back to plain", e)
        await bot.send_message(chat_id, plain_html, parse_mode="HTML",
                               disable_web_page_preview=True)


# ============================================================================
# /banlist
# ============================================================================
async def _banned_rows() -> list[dict]:
    out: list[dict] = []
    try:
        from .. import mongo_db  # type: ignore
        if repo._mongo():
            async def _op(db):
                cur = db.user_directory.find(
                    {"banned": True},
                    {"user_id": 1, "username": 1, "first_name": 1,
                     "ban_reason": 1, "banned_at": 1})
                return await cur.to_list(length=None)
            docs = await mongo_db.with_retry(_op)
            for d in docs:
                out.append({"user_id": int(d.get("user_id") or d.get("_id")),
                            "username": d.get("username"),
                            "first_name": d.get("first_name"),
                            "reason": d.get("ban_reason") or "manual ban",
                            "banned_at": d.get("banned_at") or ""})
    except Exception as e:
        log.warning("mongo ban list failed (%s) — settings fallback", e)
    if not out:
        data = await repo.get_setting_json("banned_users", {}) or {}
        for uid, reason in data.items():
            out.append({"user_id": int(uid), "username": None,
                        "first_name": None,
                        "reason": ("manual ban" if reason is True
                                   else str(reason)),
                        "banned_at": ""})
    out.sort(key=lambda r: r.get("banned_at") or "", reverse=True)
    return out


def _user_label(r: dict) -> str:
    if r.get("username"):
        return "@" + str(r["username"])
    if r.get("first_name"):
        return str(r["first_name"])[:_NAME_MAX]
    return f"id:{r['user_id']}"


async def build_ban_list() -> tuple[dict, str, int]:
    rows = await _banned_rows()
    total = len(rows)
    headers = ["#", "User", "Detail", "Tap to copy"]
    cells: list[list[dict]] = []
    plain = [f"🚫 <b>Banned users — {total}</b>", "<pre>",
             f"{'#':<3} {'User':<20} {'Detail':<22} /unban"]
    for i, r in enumerate(rows[:TABLE_CELL_LIMIT], 1):
        label = _user_label(r)
        detail = (r.get("reason") or "manual ban")[:22]
        cells.append([_cell(str(i)), _cell(label), _cell(detail),
                      {"text": f"/unban {r['user_id']}"}])
        plain.append(f"{i:<3} {label:<20} {detail:<22} /unban {r['user_id']}")
    plain.append("</pre>")
    if total > TABLE_CELL_LIMIT:
        plain.append(f"… and {total - TABLE_CELL_LIMIT} more.")
    rm = _wrap(f"🚫 Banned users ({total})",
               rich_table(headers, cells),
               "Tap a row's /unban command to copy it.")
    return rm, "\n".join(plain), total


# ============================================================================
# Daily log (IST day key)
# ============================================================================
def _ist_day(ts: Optional[float] = None) -> str:
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(ts or time.time(), tz=IST)
    return t.strftime("%Y-%m-%d")


def _log_key(day: Optional[str] = None) -> str:
    return f"verified_log:{day or _ist_day()}"


def detect_link_type(api_base: str) -> str:
    b = (api_base or "").lower()
    for name in ("vplink", "arolink", "shorturllink", "linkshortify",
                 "shortink", "gplinks"):
        if name in b:
            return name
    return "other" if b else "unknown"


async def _link_type_now() -> str:
    try:
        base = await repo.get_setting("shortener_api") or ""
    except Exception:
        base = ""
    return detect_link_type(base)


async def category_for_code(code: str) -> str:
    """Caption tags of a post (kept for other callers/logging)."""
    try:
        post = await repo.get_post_by_code(code)
        if not post:
            return ""
        tags = parse_tags(post.get("caption") or "")
        parts = []
        for sec in ("parodies", "tags", "artists"):
            for t in sorted(tags.get(sec, [])):
                if t not in parts:
                    parts.append(t)
                    if len(parts) >= 3:
                        return ", ".join(parts)
        return ", ".join(parts)
    except Exception:
        return ""


async def _bump(user_id: int, field: str, **extra) -> None:
    key = _log_key()
    data = await repo.get_setting_json(key, {}) or {}
    rec = data.get(str(user_id)) or {"count": 0, "downloads": 0,
                                     "link_type": ""}
    rec[field] = int(rec.get(field, 0)) + 1
    for k, v in extra.items():
        if v not in (None, ""):
            rec[k] = v
    rec["ts"] = time.time()
    data[str(user_id)] = rec
    await repo.set_setting_json(key, data)


async def record_verification(user_id: int, elapsed: Optional[float] = None,
                              category: str = "",
                              link_type: str = "") -> None:
    """Called once per successful shortener solve."""
    try:
        if not link_type:
            link_type = await _link_type_now()
        key = _log_key()
        data = await repo.get_setting_json(key, {}) or {}
        rec = data.get(str(user_id)) or {"count": 0, "downloads": 0,
                                         "link_type": ""}
        rec["count"] = int(rec.get("count", 0)) + 1
        if elapsed is not None:
            rec["elapsed"] = round(float(elapsed), 1)
        if category:
            rec["category"] = category[:60]
        if link_type:
            lt = set(filter(None, (rec.get("link_type") or "").split(",")))
            lt.add(link_type)
            rec["link_type"] = ",".join(sorted(lt))
        rec["ts"] = time.time()
        data[str(user_id)] = rec
        await repo.set_setting_json(key, data)
    except Exception as e:
        log.info("record_verification skipped: %s", e)


async def record_download(user_id: int, code: str = "") -> None:
    """Called for EVERY delivered post (1 post = 1 token spent)."""
    try:
        await _bump(int(user_id), "downloads")
    except Exception as e:
        log.info("record_download skipped: %s", e)


async def _today_log() -> dict:
    return await repo.get_setting_json(_log_key(), {}) or {}


async def build_verified_list() -> tuple[dict, str, int, int]:
    data = await _today_log()
    uids = [int(u) for u in data.keys()]
    directory: dict = {}
    try:
        directory = await repo.get_directory_users(uids) if uids else {}
    except Exception:
        directory = {}
    wallets: dict = {}
    try:
        wallets = await tokens.get_many(uids) if uids else {}
    except Exception:
        wallets = {}

    rows: list[dict] = []
    for uid_s, rec in data.items():
        uid = int(uid_s)
        d = directory.get(uid) or directory.get(str(uid)) or {}
        display = (d.get("first_name")
                   or (("@" + d["username"]) if d.get("username")
                       else f"id:{uid}"))
        w = wallets.get(uid) or {}
        rows.append({
            "uid": uid,
            "name": str(display)[:_NAME_MAX],
            "url": f"tg://user?id={uid}",
            "elapsed": rec.get("elapsed"),
            "downloads": int(rec.get("downloads", 0) or 0),
            "link_type": rec.get("link_type") or "—",
            "count": int(rec.get("count", 0) or 0),
            "tokens": int(w.get("tokens", 0) or 0),
            "expiry": float(w.get("expiry", 0) or 0),
            "ts": float(rec.get("ts", 0)),
        })
    rows.sort(key=lambda r: r["ts"])
    users = len(rows)
    solves = sum(r["count"] for r in rows)

    cells: list[list[dict]] = []
    plain = [f"✅ <b>Verified users — {_ist_day()} (since midnight IST): "
             f"{users} users · {solves} solves</b>", "<pre>",
             f"{'#':<3} {'Name':<18} {'Elap':<6} {'File':<5} {'Link':<9} "
             f"{'Solve':<6} {'Tok':<4}"]
    for i, r in enumerate(rows[:TABLE_CELL_LIMIT], 1):
        el = f"{int(r['elapsed'])}s" if r.get("elapsed") is not None else "—"
        cells.append([
            _cell(str(i)),
            {"text": browse.rt_url(r["name"], r["url"])},   # profile-linked
            _cell(el),
            _cell(str(r["downloads"])),
            _cell(r["link_type"]),
            _cell(str(r["count"])),
            _cell(str(r["tokens"])),
        ])
        plain.append(f"{i:<3} {r['name']:<18} {el:<6} {r['downloads']:<5} "
                     f"{r['link_type']:<9} {r['count']:<6} {r['tokens']:<4}")
    plain.append("</pre>")
    if users > TABLE_CELL_LIMIT:
        plain.append(f"… and {users - TABLE_CELL_LIMIT} more.")
    rm = _wrap(f"✅ Verified users — {_ist_day()}",
               rich_table(VCOLUMNS, cells),
               f"Since midnight IST · {users} users · {solves} solves · "
               f"File = posts fetched · Count = solves · Tokens expire "
               f"4:00 AM IST.")
    return rm, "\n".join(plain), users, solves


# ============================================================================
# Ban message template
# ============================================================================
DEFAULT_BAN_MESSAGE = (
    "🚫 <b>You have been banned</b>\n\n"
    "<blockquote>Bypass tools are strictly prohibited. Please solve the "
    "shortener manually.</blockquote>\n"
    "<i>Last elapsed:</i> <code>{elapsed}s</code>")


async def get_ban_message() -> str:
    raw = (await repo.get_setting("ban_message")) or ""
    return raw.strip() or DEFAULT_BAN_MESSAGE


def render_ban_message(template: str, elapsed: float, user_id: int) -> str:
    try:
        return template.format(elapsed=f"{elapsed:.1f}", user_id=user_id)
    except (KeyError, IndexError, ValueError):
        return template
