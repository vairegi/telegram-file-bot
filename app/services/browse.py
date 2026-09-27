"""v4.5 — /browse: rich-message multi-level tag browser (Section → Tag → Items).

Data comes 100% from EXISTING cover captions — nothing new is stored for the
menu itself. Captions carry sections like:
    ➤ Parodies: #nijisanji #vtuber
    ➤ Tags: #sole_female #glasses
parse_tags() from recommend.py already extracts them; this module aggregates
those across ALL published covers into a per-section tag index.

Rendering uses Telegram Rich Messages (Bot API 10.1+/10.2+/10.3+):
  * Buttons are InputRichBlockButtons blocks embedded in the rich message —
    NEVER InlineKeyboardMarkup reply_markup.
  * RichText leaf nodes are bare JSON strings — NEVER {"type":"plain", ...}.
  * aiogram 3.13 has no Rich Message helpers, so we call the raw Bot API via
    aiohttp (bot._post equivalent). Real Telegram errors are raised verbatim.

Callback protocol (all <= 64 BYTES, enforced by tests + runtime guard):
    brw:s:<section>            level 0 -> tag list of a section
    brw:t:<section>:<tag_id>   level 1 -> items for a tag (tag_id is a short
                               hash, resolved via the cached index)
    brw:p:<section>:<tag_id>:<page>   paginate items
    brw:home                   back to section list

Chat-type split:
  * DM     -> sendRichMessage, then editMessageText(rich_message=...) in place.
  * Groups -> sendRichMessage with ephemeral_message_parameters
              {receiver_user_id, callback_query_id?}, then
              editEphemeralMessageText(chat_id, receiver_user_id,
              ephemeral_message_id, rich_message=...) for navigation.

Pinned tags: /browse_pin #tag [section], /browse_unpin #tag, /browse_pins.
Pinned tags float to the top of their section's Level-1 list (⭐ prefix).

Suggest link: set SUGGEST_URL in env (e.g. https://t.me/<owner_username>) and
a "💡 Suggest a tag" URL button is appended at the bottom of every window.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

import aiohttp

from . import repo
from .recommend import parse_tags
from ..utils import clean_caption, first_line

log = logging.getLogger("browse")

PAGE_SIZE = 20                 # tags per page (Level 1) and items per page (Level 2)
ITEMS_PAGE_SIZE = 10           # item rows are long (title + button) — keep tight
MAX_BUTTONS_PER_ROW = 8        # InputRichBlockButtons hard limit
CALLBACK_MAX_BYTES = 64        # RichMessageButton.callback_data limit
TAG_ID_LEN = 10                # hex chars of sha1 for callback tag ids

# Sections we expose, in menu order. 'languages'/'categories' stay excluded —
# they match everything (english/translated/doujinshi) and would be noise.
BROWSE_SECTIONS = ["parodies", "characters", "artists", "groups", "tags"]
_SECTION_TITLES = {
    "parodies": "📚 Parodies",
    "characters": "🧑 Characters",
    "artists": "🎨 Artists",
    "groups": "👥 Groups",
    "tags": "🏷 Tags",
}

# In-memory aggregate cache: {section: {tag: count}} + covers per tag.
_INDEX_TTL = 300.0  # seconds — the menu doesn't need second-fresh data
_index_cache: dict = {"ts": 0.0, "by_section": {}, "covers_by_tag": {}}


class BrowseError(Exception):
    """Raised with the REAL Telegram error text (never masked)."""


# ============================================================================
# Raw Bot API transport (aiogram has no rich-message methods)
# ============================================================================
async def api_post(bot, method: str, payload: dict) -> dict:
    """POST /bot<token>/<method> with a JSON body. Raises BrowseError with
    Telegram's verbatim description on failure — MISTAKE 5 guard."""
    token = getattr(bot, "token", None)
    if not token:
        raise BrowseError("bot token unavailable")
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
    if not data.get("ok"):
        desc = data.get("description") or "unknown error"
        code = data.get("error_code") or resp.status
        log.error("%s failed [%s]: %s | payload=%s", method, code, desc,
                  json.dumps(payload)[:500])
        raise BrowseError(f"{method} failed [{code}]: {desc}")
    return data.get("result") or {}


# ============================================================================
# RichText / block builders — leaf nodes are ALWAYS bare strings
# ============================================================================
def rt(text: str) -> str:
    """Plain-text RichText leaf. MUST stay a bare str (never a dict)."""
    return str(text)


def rt_bold(text: str) -> dict:
    return {"type": "bold", "text": str(text)}


def rt_url(text: str, url: str) -> dict:
    return {"type": "url", "text": str(text), "url": str(url)}


def block_heading(text: str, size: int = 3) -> dict:
    return {"type": "heading", "text": rt(text), "size": int(size)}


def block_paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": rt(text)}


def block_buttons_row(buttons: list[dict], align: str = "center") -> dict:
    """One ROW of rich buttons. 1-8 per row enforced."""
    if not 1 <= len(buttons) <= MAX_BUTTONS_PER_ROW:
        raise BrowseError(f"button row must have 1-{MAX_BUTTONS_PER_ROW} buttons")
    return {"type": "buttons", "buttons": buttons, "align": align}


def nav_button(text: str, callback_data: str, style: Optional[str] = None) -> dict:
    b: dict[str, Any] = {"text": rt(text), "callback_data": callback_data}
    if len(callback_data.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise BrowseError(f"callback_data too long: {callback_data!r}")
    if style:
        b["style"] = style
    return b


def url_button(text: str, url: str, style: Optional[str] = None) -> dict:
    b: dict[str, Any] = {"text": rt(text), "url": str(url)}
    if style:
        b["style"] = style
    return b


def rich_message(blocks: list[dict]) -> dict:
    return {"blocks": blocks, "skip_entity_detection": True}


def _button_rows(buttons: list[dict], per_row: int = 2) -> list[dict]:
    """Chunk buttons into InputRichBlockButtons rows of <= per_row."""
    rows = []
    for i in range(0, len(buttons), per_row):
        rows.append(block_buttons_row(buttons[i:i + per_row]))
    return rows


# ============================================================================
# Tag index — aggregated from published cover captions
# ============================================================================
def tag_id(section: str, tag: str) -> str:
    return hashlib.sha1(f"{section}:{tag}".encode()).hexdigest()[:TAG_ID_LEN]


async def _build_index() -> tuple[dict, dict]:
    """Scan ALL published covers once and aggregate:
        by_section:   {section: {tag: cover_count}}
        covers_by_tag: {(section, tag): [cover rows sorted by post_number desc]}
    """
    by_section: dict[str, dict[str, int]] = {s: {} for s in BROWSE_SECTIONS}
    covers_by_tag: dict[tuple[str, str], list] = {}
    offset_pool = await repo.recent_published_covers(limit=10000, exclude_id=0)
    for cover in offset_pool:
        tags = parse_tags(cover.get("caption") or "")
        seen_here: set[tuple[str, str]] = set()  # count each cover once per tag
        for section, tagset in tags.items():
            if section not in by_section:
                continue
            for tag in tagset:
                key = (section, tag)
                if key in seen_here:
                    continue
                seen_here.add(key)
                by_section[section][tag] = by_section[section].get(tag, 0) + 1
                covers_by_tag.setdefault(key, []).append(cover)
    for key in covers_by_tag:
        covers_by_tag[key].sort(key=lambda c: c.get("post_number") or 0,
                                reverse=True)
    return by_section, covers_by_tag


async def get_index(force: bool = False) -> tuple[dict, dict]:
    if (not force) and _index_cache["ts"] and \
            time.time() - _index_cache["ts"] < _INDEX_TTL:
        return _index_cache["by_section"], _index_cache["covers_by_tag"]
    by_section, covers_by_tag = await _build_index()
    _index_cache.update(ts=time.time(), by_section=by_section,
                        covers_by_tag=covers_by_tag)
    return by_section, covers_by_tag


def resolve_tag(by_section: dict, section: str, tid: str) -> Optional[str]:
    for tag in by_section.get(section, {}):
        if tag_id(section, tag) == tid:
            return tag
    return None


async def _pinned() -> dict[str, list[str]]:
    """{section: [tag, ...]} — pinned tags float to the top of Level 1."""
    data = await repo.get_setting_json("browse_pins", {}) or {}
    return {str(k): [str(t) for t in v] for k, v in data.items()
            if isinstance(v, list)}


def _sorted_tags(tags: dict[str, int], pins: list[str]) -> list[str]:
    pinned = [t for t in pins if t in tags]
    rest = sorted((t for t in tags if t not in pinned),
                  key=lambda t: (-tags[t], t))
    return pinned + rest


# ============================================================================
# Window renderers — each returns an InputRichMessage dict
# ============================================================================
def render_sections(by_section: dict, pins: dict) -> dict:
    blocks: list[dict] = [
        block_heading("📖 Browse the library", size=2),
        block_paragraph("Pick a category — tags are read live from cover captions."),
    ]
    btns = []
    for section in BROWSE_SECTIONS:
        count = len(by_section.get(section, {}))
        if not count:
            continue
        btns.append(nav_button(f"{_SECTION_TITLES[section]} ({count})",
                               f"brw:s:{section}", style="primary"))
    blocks.extend(_button_rows(btns, per_row=2))
    blocks.extend(_suggest_row())
    return rich_message(blocks)


def render_tags(by_section: dict, pins: dict, section: str,
                page: int = 0) -> dict:
    tags = by_section.get(section, {})
    ordered = _sorted_tags(tags, pins.get(section, []))
    total_pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    window = ordered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    blocks: list[dict] = [
        block_heading(f"{_SECTION_TITLES[section]}", size=2),
        block_paragraph(
            f"Page {page + 1}/{total_pages} — {len(tags)} tag(s). ⭐ = pinned."),
    ]
    btns = []
    pin_set = set(pins.get(section, []))
    for tag in window:
        star = "⭐ " if tag in pin_set else ""
        btns.append(nav_button(f"{star}#{tag} ({tags[tag]})",
                               f"brw:t:{section}:{tag_id(section, tag)}"))
    blocks.extend(_button_rows(btns, per_row=2))

    nav = [nav_button("« Back", "brw:home")]
    if page > 0:
        nav.append(nav_button("‹ Prev", f"brw:sp:{section}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(nav_button("Next ›", f"brw:sp:{section}:{page + 1}"))
    blocks.append(block_buttons_row(nav))
    blocks.extend(_suggest_row())
    return rich_message(blocks)


def render_items(covers: list[dict], section: str, tag: str,
                 page: int = 0, bot_name: str = "") -> dict:
    total_pages = max(1, (len(covers) + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    window = covers[page * ITEMS_PAGE_SIZE:(page + 1) * ITEMS_PAGE_SIZE]

    blocks: list[dict] = [
        block_heading(f"#{tag}", size=2),
        block_paragraph(
            f"{_SECTION_TITLES[section]} · {len(covers)} item(s) · "
            f"page {page + 1}/{total_pages}. Tap to get the file."),
    ]
    for cover in window:
        title = first_line(clean_caption(cover.get("caption")), 70) or "Untitled"
        n = cover.get("post_number") or "?"
        code = cover.get("code") or ""
        # The item label is a CLICKABLE RichTextUrl (deep link), and a matching
        # 📥 button below it — both open /start get_<code> so every existing
        # gate (ban → shortener → fsub) and delivery logic is reused as-is.
        blocks.append({"type": "paragraph",
                       "text": [rt(f"#{n} · "),
                                rt_url(title, f"https://t.me/{bot_name}?start=get_{code}")]})
        blocks.append(block_buttons_row(
            [url_button(f"📥 Get File #{n}",
                        f"https://t.me/{bot_name}?start=get_{code}",
                        style="success")],
            align="left"))

    tid = tag_id(section, tag)
    nav = [nav_button("« Tags", f"brw:s:{section}")]
    if page > 0:
        nav.append(nav_button("‹ Prev", f"brw:p:{section}:{tid}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(nav_button("Next ›", f"brw:p:{section}:{tid}:{page + 1}"))
    blocks.append(block_buttons_row(nav))
    blocks.extend(_suggest_row())
    return rich_message(blocks)


def _suggest_row() -> list[dict]:
    url = (os.environ.get("SUGGEST_URL") or "").strip()
    if not url:
        return []
    return [block_buttons_row(
        [url_button("💡 Suggest a tag", url, style="primary")], align="center")]


# ============================================================================
# Send / edit orchestration — DM rich path vs group ephemeral path
# ============================================================================
def _is_private(msg_or_chat) -> bool:
    chat = getattr(msg_or_chat, "chat", msg_or_chat)
    return getattr(chat, "type", "private") == "private"


async def send_browse(bot, msg, rm: dict) -> None:
    """Send the top-level window. DM: sendRichMessage. Group: ephemeral."""
    chat_id = msg.chat.id
    if _is_private(msg):
        await api_post(bot, "sendRichMessage",
                       {"chat_id": chat_id, "rich_message": rm})
        return
    await api_post(bot, "sendRichMessage", {
        "chat_id": chat_id,
        "rich_message": rm,
        "ephemeral_message_parameters": {"receiver_user_id": msg.from_user.id},
    })


async def edit_browse(bot, cb, rm: dict) -> None:
    """Navigate: edit the existing window in place (both chat types)."""
    message = getattr(cb, "message", None)
    chat_id = getattr(getattr(cb.message, "chat", None), "id", None)
    if message is None or chat_id is None:
        raise BrowseError("callback has no message to edit")

    eph_id = getattr(message, "ephemeral_message_id", None)
    if eph_id is not None:
        # Group ephemeral path — editEphemeralMessageText with receiver_user_id.
        await api_post(bot, "editEphemeralMessageText", {
            "chat_id": chat_id,
            "receiver_user_id": cb.from_user.id,
            "ephemeral_message_id": eph_id,
            "rich_message": rm,
        })
        return
    # DM rich path — normal editMessageText carrying rich_message.
    await api_post(bot, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message.message_id,
        "rich_message": rm,
    })


# ============================================================================
# State machine — parses brw: callbacks and renders the right window
# ============================================================================
async def window_for(data: str, bot_name: str = "") -> Optional[dict]:
    """Pure-ish renderer: brw: callback data -> InputRichMessage dict."""
    by_section, covers_by_tag = await get_index()
    pins = await _pinned()
    parts = data.split(":")
    if data == "brw:home":
        return render_sections(by_section, pins)
    if len(parts) >= 3 and parts[1] == "s":
        return render_tags(by_section, pins, parts[2], page=0)
    if len(parts) >= 4 and parts[1] == "sp":
        return render_tags(by_section, pins, parts[2], page=int(parts[3]))
    if len(parts) >= 4 and parts[1] in ("t", "p"):
        section, tid = parts[2], parts[3]
        tag = resolve_tag(by_section, section, tid)
        if tag is None:
            return None
        page = int(parts[4]) if len(parts) >= 5 else 0
        covers = covers_by_tag.get((section, tag), [])
        return render_items(covers, section, tag, page=page, bot_name=bot_name)
    return None
