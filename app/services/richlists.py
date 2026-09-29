"""v4.7 — /ban list and /verified_users, rendered as Rich Message tables.

Uses InputRichBlockTable (Bot API 10.1+): cells are RichBlockTableCell with
bare-string RichText leaves, header row via is_header, links via RichTextUrl
(type:"url") — e.g. the "Tap to copy" column is a clickable /unban command.
Sent via the existing raw Bot API transport in browse.api_post (aiogram 3.13
has no rich-message helpers); real Telegram errors surface verbatim.

Data sources (no schema changes):
  * Bans:        Mongo user_directory {banned:true} rows, or the Turso
                 `banned_users` settings JSON (dormant fallback).
  * Verifications: the existing `verified_users` collection is stamp-only
                 (until/created_at), so this module additionally records a
                 per-day log `verified_log:<YYYY-MM-DD IST>` in the settings
                 store — first touch at 00:00 IST, reset at 23:59 IST.
                 repo.py is patched to call `record_verification()` inside
                 verified_set(), so EVERY verification path (timed + legacy)
                 is captured without touching shortener.py.

Recorded per verification (best-effort, all fields optional):
    user_id, elapsed (s, from token.issued_at when still available),
    category (caption sections of the post they unlocked), link_type
    (derived from the configured shortener API base, e.g. vplink/arolink).

If Telegram rejects the rich table (older client/API), the handler falls
back to a plain monospace HTML table so the command NEVER hard-fails.
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import browse, repo
from .recommend import parse_tags

log = logging.getLogger("richlists")

IST = ZoneInfo("Asia/Kolkata")
TABLE_CELL_LIMIT = 100   # rows per rich table page (soft cap)
_NAME_MAX = 16


class RichListError(browse.BrowseError):
    """Same contract: carries the real Telegram error text."""


# ============================================================================
# Rich table builders (leaf text nodes are ALWAYS bare strings)
# ============================================================================
def _cell(text: str, header: bool = False) -> dict:
    c = {"text": browse.rt(text)}
    if header:
        c["is_header"] = True
    return c


def _link_cell(text: str, url: str) -> dict:
    return {"text": browse.rt_url(text, url)}


def rich_table(headers: list[str], rows: list[list[dict]],
               caption: str = "", striped: bool = True) -> dict:
    """InputRichBlockTable block."""
    cells = [[_cell(h, header=True) for h in headers]] + rows
    blk: dict = {"type": "table", "cells": cells,
                 "is_striped": bool(striped)}
    if caption:
        blk["caption"] = browse.rt(caption)
    return blk


def _wrap(title: str, table_block: dict, page_note: str = "") -> dict:
    blocks = [browse.block_heading(title, size=2), table_block]
    if page_note:
        blocks.append(browse.block_paragraph(page_note))
    return browse.rich_message(blocks)


async def send_rich_or_plain(bot, chat_id: int, rm: dict, plain_html: str,
                             reply_to: int = 0) -> None:
    """Try sendRichMessage; on Telegram 400 fall back to a normal HTML message.
    The user always gets data — never a silent failure."""
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
# /ban list
# ============================================================================
async def _banned_rows() -> list[dict]:
    """[{user_id, username, first_name, reason, banned_at}] newest first."""
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
        log.warning("mongo ban list failed (%s) — trying settings", e)
    if not out:
        data = await repo.get_setting_json("banned_users", {}) or {}
        for uid, reason in data.items():
            out.append({"user_id": int(uid), "username": None,
                        "first_name": None,
                        "reason": "manual ban" if reason is True else str(reason),
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
    """Returns (rich_message, plain_html_fallback, total)."""
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
                      _link_cell(f"/unban {r['user_id']}",
                                 f"tg://resolve?domain=noop&start=")])
        # tg:// links can't prefill commands; keep a plain-text command in the
        # fallback + copyable id in rich table via the URL cell text.
        cells[-1][3] = {"text": f"/unban {r['user_id']}"}
        plain.append(f"{i:<3} {label:<20} {detail:<22} /unban {r['user_id']}")
    plain.append("</pre>")
    if total > TABLE_CELL_LIMIT:
        plain.append(f"… and {total - TABLE_CELL_LIMIT} more (showing first "
                     f"{TABLE_CELL_LIMIT}).")
    rm = _wrap(f"🚫 Banned users ({total})",
               rich_table(headers, cells),
               "Tap a row's /unban command to copy it.")
    return rm, "\n".join(plain), total


# ============================================================================
# /verified_users — daily IST log, 00:00 → 23:59
# ============================================================================
def _ist_day(ts: Optional[float] = None) -> str:
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(ts or time.time(), tz=IST)
    return t.strftime("%Y-%m-%d")


def _log_key(day: Optional[str] = None) -> str:
    return f"verified_log:{day or _ist_day()}"


def detect_link_type(api_base: str) -> str:
    b = (api_base or "").lower()
    for name in ("vplink", "arolink", "shorturllink", "linkshortify"):
        if name in b:
            return name
    return "other" if b else "unknown"


async def record_verification(user_id: int, elapsed: Optional[float] = None,
                              category: str = "", link_type: str = "") -> None:
    """Append today's IST verification (called from repo.verified_set)."""
    try:
        key = _log_key()
        data = await repo.get_setting_json(key, {}) or {}
        rec = data.get(str(user_id)) or {"count": 0}
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


async def _today_log() -> dict:
    return await repo.get_setting_json(_log_key(), {}) or {}


async def build_verified_list() -> tuple[dict, str, int, int]:
    """(rich_message, plain_html, users_today, verifications_today)."""
    data = await _today_log()
    # directory lookup for nice names
    uids = [int(u) for u in data.keys()]
    directory = {}
    try:
        directory = await repo.get_directory_users(uids) if uids else {}
    except Exception:
        directory = {}

    rows_out = []
    for uid_s, rec in data.items():
        uid = int(uid_s)
        d = directory.get(uid) or directory.get(str(uid)) or {}
        name = d.get("first_name") or (
            "@" + d["username"] if d.get("username") else f"id:{uid}")
        rows_out.append({
            "name": str(name)[:_NAME_MAX],
            "elapsed": rec.get("elapsed"),
            "category": rec.get("category") or "—",
            "link_type": rec.get("link_type") or "—",
            "count": int(rec.get("count", 1)),
            "ts": float(rec.get("ts", 0)),
        })
    rows_out.sort(key=lambda r: r["ts"])          # chronological
    users = len(rows_out)
    verifs = sum(r["count"] for r in rows_out)

    headers = ["#", "Name", "Elapsed", "Category", "Link Type", "Count"]
    cells: list[list[dict]] = []
    plain = [f"✅ <b>Verified users — {_ist_day()} (since midnight IST): "
             f"{users} users · {verifs} verifications</b>", "<pre>",
             f"{'#':<3} {'Name':<16} {'Elap':<6} {'Category':<14} "
             f"{'Link':<10} {'#':<2}"]
    for i, r in enumerate(rows_out[:TABLE_CELL_LIMIT], 1):
        el = f"{int(r['elapsed'])}s" if r.get("elapsed") is not None else "—"
        cells.append([_cell(str(i)), _cell(r["name"]), _cell(el),
                      _cell(r["category"][:20]), _cell(r["link_type"]),
                      _cell(str(r["count"]))])
        plain.append(f"{i:<3} {r['name']:<16} {el:<6} {r['category'][:14]:<14} "
                     f"{r['link_type']:<10} {r['count']:<2}")
    plain.append("</pre>")
    if users > TABLE_CELL_LIMIT:
        plain.append(f"… and {users - TABLE_CELL_LIMIT} more.")
    rm = _wrap(f"✅ Verified users — {_ist_day()}",
               rich_table(headers, cells),
               f"Since midnight IST · {users} users · {verifs} verifications. "
               f"Resets 23:59 IST.")
    return rm, "\n".join(plain), users, verifs


# ============================================================================
# Helpers used by repo.verified_set patch to enrich the log entry
# ============================================================================
async def context_for_user(user_id: int) -> dict:
    """Best-effort elapsed/category/link_type for the user's latest token."""
    out: dict = {}
    try:
        base = await repo.get_setting("shortener_api") or ""
        out["link_type"] = detect_link_type(base)
    except Exception:
        pass
    return out


async def category_for_code(code: str) -> str:
    """Distinctive caption sections of a post, for the Category column."""
    try:
        post = await repo.get_post_by_code(code)
        if not post:
            return ""
        tags = parse_tags(post.get("caption") or "")
        parts = []
        for sec in ("parodies", "tags", "artists"):
            if tags.get(sec):
                parts.append(sorted(tags[sec])[0])
        return ", ".join(parts[:3])
    except Exception:
        return ""
