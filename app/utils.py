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
