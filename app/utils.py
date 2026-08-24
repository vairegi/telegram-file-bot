"""Shared helpers."""
from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone
from typing import Optional, Tuple


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def random_code(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(text: str, n: int = 200) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def to_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except Exception:
        return default


_TME_RE = re.compile(
    r"(?:https?://)?t\.me/(?:c/(?P<cid>-?\d+)|(?P<uname>[A-Za-z0-9_]+))/(?P<mid>\d+)"
)


def parse_tme_link(text: str) -> Optional[Tuple[Optional[int], Optional[str], int]]:
    """Parse a t.me link. Returns (chat_id_or_None, username_or_None, message_id) or None."""
    if not text:
        return None
    m = _TME_RE.search(text.strip())
    if not m:
        return None
    mid = int(m.group("mid"))
    cid_raw = m.group("cid")
    uname = m.group("uname")
    if cid_raw:
        cid = int(cid_raw)
        if cid > 0:
            cid = int(f"-100{cid}")
        return (cid, None, mid)
    return (None, uname, mid)


def parse_channel_id(text: str) -> Optional[int]:
    s = (text or "").strip()
    if not s:
        return None
    try:
        n = int(s)
    except Exception:
        return None
    if str(n).startswith("-100"):
        return n
    if n > 0:
        return int(f"-100{n}")
    return n


def parse_hash_number(text: str) -> Optional[int]:
    """Parse '#721', '721', '#N721' -> 721."""
    s = (text or "").strip().lstrip("#").lstrip("Nn")
    return to_int(s)


def source_link(chat_id: int, message_id: int) -> str:
    cid = str(chat_id).replace("-100", "", 1) if str(chat_id).startswith("-100") else str(chat_id)
    return f"https://t.me/c/{cid}/{message_id}"


def first_line(text: Optional[str], n: int = 60) -> str:
    """Extract the first non-empty line, truncated."""
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return truncate(line, n)
    return ""


def plain_from_entities(text: str, entities) -> str:
    """Convert aiogram Message.caption_entities / text_entities formatted text
    into clean plain text (markdown markers removed, content kept verbatim).

    entities may be a list of MessageEntity-like objects with
    (type, offset, length). Bot API encodes offsets in UTF-16 code units;
    aiogram normalises text to str, so we work on the UTF-16 view.
    """
    if not text:
        return ""
    if not entities:
        return text
    b = text.encode("utf-16-le")
    chars = [b[i:i+2] for i in range(0, len(b), 2)]
    drop = set()
    for e in entities:
        etype = getattr(e, "type", "") or ""
        off = int(getattr(e, "offset", 0) or 0)
        length = int(getattr(e, "length", 0) or 0)
        if etype in ("bold", "italic", "underline", "strikethrough",
                     "spoiler", "code", "blockquote", "expandable_blockquote"):
            # Detect marker width from the ACTUAL characters at the offset:
            # look at up to 3 leading code units; the marker is the run of
            # identical non-alphanumeric chars (e.g. "**", "__", "~~", "||").
            def _marker_width(start, end):
                if start >= end:
                    return 0
                try:
                    c0 = chars[start].decode("utf-16-le", errors="ignore")
                except Exception:
                    return 0
                if c0.isalnum():
                    return 0
                w = 1
                for j in range(start + 1, min(start + 3, end)):
                    try:
                        cj = chars[j].decode("utf-16-le", errors="ignore")
                    except Exception:
                        break
                    if cj == c0:
                        w += 1
                    else:
                        break
                return w
            marker = _marker_width(off, off + length)
            if marker == 0:
                marker = 2 if etype in ("bold", "underline", "spoiler") else 1
            for i in range(off, min(off + marker, off + length)):
                drop.add(i)
            for i in range(max(off, off + length - marker), off + length):
                drop.add(i)
        elif etype == "pre":
            for i in range(off, min(off + 3, off + length)):
                drop.add(i)
            for i in range(max(off, off + length - 3), off + length):
                drop.add(i)
        # custom_emoji / text_link / url / hashtag / mention: keep text as-is
    out = "".join(ch.decode("utf-16-le", errors="ignore")
                  for i, ch in enumerate(chars) if i not in drop)
    return out


_MD_MARKERS_RE = re.compile(r"(\*\*|__|~~|\|\|)")


def strip_markdown_markers(text: str) -> str:
    """Safety net: remove literal markdown markers left over after entity
    stripping (unclosed ** / __ etc. common in this channel's captions).
    Keeps single '*' or '_' untouched."""
    if not text:
        return ""
    return _MD_MARKERS_RE.sub("", text)


def caption_plain(msg) -> str:
    """Best-effort PLAIN caption from an aiogram Message."""
    text = getattr(msg, "caption", None) or getattr(msg, "text", None) or ""
    ents = (getattr(msg, "caption_entities", None)
            or getattr(msg, "entities", None) or [])
    return strip_markdown_markers(plain_from_entities(text, ents))


def clean_caption(text: str) -> str:
    """Strip literal markdown markers from stored caption text.

    The DB channel captions carry raw '**', '__', '~~', '||' sequences as
    PLAIN TEXT (no caption_entities — they were posted by scraper clients).
    We can't rely on entity offsets; just remove the marker sequences.
    Single '*' / '_' are preserved (hashtags like #big_breasts survive).
    """
    return strip_markdown_markers(text or "")
