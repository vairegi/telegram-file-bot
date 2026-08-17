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


def random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def to_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except Exception:
        return default


_DUR_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)?\s*$", re.IGNORECASE)


def parse_duration_ms(text: str) -> Optional[int]:
    m = _DUR_RE.match(text or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    mult = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return n * mult


def format_duration_ms(ms: int) -> str:
    if ms < 60_000:
        return f"{ms // 1000}s"
    if ms < 3_600_000:
        return f"{ms // 60_000}m"
    if ms < 86_400_000:
        return f"{ms // 3_600_000}h"
    return f"{ms // 86_400_000}d"


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(text: str, n: int = 200) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def source_link(chat_id: int, message_id: int) -> str:
    cid = str(chat_id).replace("-100", "", 1) if str(chat_id).startswith("-100") else str(chat_id)
    return f"https://t.me/c/{cid}/{message_id}"


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
        # t.me/c/<X>/... links strip the "-100" prefix. Re-add it for the API chat id.
        if cid > 0:
            cid = int(f"-100{cid}")
        return (cid, None, mid)
    return (None, uname, mid)


def parse_channel_id(text: str) -> Optional[int]:
    """Parse a raw chat id like '-1002298797194' or '2298797194' (adds -100)."""
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
