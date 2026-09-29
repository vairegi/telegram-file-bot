"""v4.8 — /banlist, /verified_users, /banmessage, instant-ban.

Fixes vs v4.7:
  * Logging moved from repo.verified_set() to the redemption sites in
    setup_cmds.py (deep-link path) and callbacks.py (I've Verified button).
    Only there do we have (elapsed, code, link_type) at the same time, so
    Count / Elapsed / Category / Link Type all fill in correctly.
  * /banlist (single word) is now the ban-table command — /ban <user_id>
    keeps working. The old placeholder /banlist in shortener_cmds is
    disabled by the new router taking precedence (registered FIRST) and
    the fallback text below.
  * Instant ban: setup_cmds.py's bypass branch calls repo.ban_user() on the
    FIRST bypass (no 3-strike wait). Message is admin-tunable via
    /banmessage — HTML supported (bold, quotes, blockquote, code, links).
    Two placeholders: {elapsed} and {user_id}.

Rich tables use InputRichBlockTable; automatic plain-HTML fallback if the
client can't render them.
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
TABLE_CELL_LIMIT = 100
_NAME_MAX = 16


# ============================================================================
# Rich-table helpers (leaf text nodes are ALWAYS bare strings)
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
    """Try sendRichMessage; on Telegram 400 fall back to a plain HTML message."""
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
# Ban list  (/banlist)
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
# /verified_users — daily IST log
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
    """Top caption tags of the delivered post — Category column value."""
    try:
        post = await repo.get_post_by_code(code)
        if not post:
            return ""
        tags = parse_tags(post.get("caption") or "")
        parts = []
        # priority: parodies > tags > artists — first two of each
        for sec in ("parodies", "tags", "artists"):
            for t in sorted(tags.get(sec, [])):
                if t not in parts:
                    parts.append(t)
                    if len(parts) >= 3:
                        return ", ".join(parts)
        return ", ".join(parts)
    except Exception:
        return ""


async def record_verification(user_id: int,
                              elapsed: Optional[float] = None,
                              category: str = "",
                              link_type: str = "") -> None:
    """Called at REDEMPTION (deep-link + I've Verified button)."""
    try:
        if not link_type:
            link_type = await _link_type_now()
        key = _log_key()
        data = await repo.get_setting_json(key, {}) or {}
        rec = data.get(str(user_id)) or {"count": 0, "link_type": ""}
        rec["count"] = int(rec.get("count", 0)) + 1
        if elapsed is not None:
            # keep the LAST elapsed (most recent is most representative)
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
    data = await _today_log()
    uids = [int(u) for u in data.keys()]
    directory: dict = {}
    try:
        directory = await repo.get_directory_users(uids) if uids else {}
    except Exception:
        directory = {}

    rows: list[dict] = []
    for uid_s, rec in data.items():
        uid = int(uid_s)
        d = directory.get(uid) or directory.get(str(uid)) or {}
        name = (d.get("first_name")
                or (("@" + d["username"]) if d.get("username") else f"id:{uid}"))
        rows.append({
            "name": str(name)[:_NAME_MAX],
            "elapsed": rec.get("elapsed"),
            "category": rec.get("category") or "—",
            "link_type": rec.get("link_type") or "—",
            "count": int(rec.get("count", 1)),
            "ts": float(rec.get("ts", 0)),
        })
    rows.sort(key=lambda r: r["ts"])
    users = len(rows)
    verifs = sum(r["count"] for r in rows)

    headers = ["#", "Name", "Elapsed", "Category", "Link Type", "Count"]
    cells: list[list[dict]] = []
    plain = [f"✅ <b>Verified users — {_ist_day()} (since midnight IST): "
             f"{users} users · {verifs} verifications</b>", "<pre>",
             f"{'#':<3} {'Name':<16} {'Elap':<6} {'Category':<14} "
             f"{'Link':<10} {'#':<2}"]
    for i, r in enumerate(rows[:TABLE_CELL_LIMIT], 1):
        el = f"{int(r['elapsed'])}s" if r.get("elapsed") is not None else "—"
        cells.append([_cell(str(i)), _cell(r["name"]), _cell(el),
                      _cell(r["category"][:20]), _cell(r["link_type"]),
                      _cell(str(r["count"]))])
        plain.append(f"{i:<3} {r['name']:<16} {el:<6} "
                     f"{r['category'][:14]:<14} {r['link_type']:<10} "
                     f"{r['count']:<2}")
    plain.append("</pre>")
    if users > TABLE_CELL_LIMIT:
        plain.append(f"… and {users - TABLE_CELL_LIMIT} more.")
    rm = _wrap(f"✅ Verified users — {_ist_day()}",
               rich_table(headers, cells),
               f"Since midnight IST · {users} users · {verifs} verifications. "
               f"Resets 23:59 IST.")
    return rm, "\n".join(plain), users, verifs


# ============================================================================
# Ban message template — /banmessage supports HTML
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
        # broken placeholders shouldn't hide the ban — return raw template
        return template
