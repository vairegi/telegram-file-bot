"""Small shared helpers: id/code generation, time parsing, safe conversion."""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone

_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def random_code(nbytes: int = 6) -> str:
    """URL-safe short code used in deep links and file mapping."""
    return base64.urlsafe_b64encode(os.urandom(nbytes)).rstrip(b"=").decode("ascii")


def random_token(nbytes: int = 18) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).rstrip(b"=").decode("ascii")


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_duration_ms(text: str) -> int | None:
    """Parse '5h 2m', '10m', '2d 3h' into milliseconds. None if invalid."""
    if not text:
        return None
    parts = re.findall(r"(\d+)\s*([smhd])", text.strip().lower())
    if not parts:
        return None
    total = sum(int(n) * _UNIT_MS[u] for n, u in parts)
    return total if total > 0 else None


def format_duration_ms(ms: int) -> str:
    if ms < 0:
        ms = 0
    s = ms // 1000
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    out = []
    if d:
        out.append(f"{d}d")
    if h:
        out.append(f"{h}h")
    if m:
        out.append(f"{m}m")
    return " ".join(out) or f"{s}s"


def esc(value: str | None) -> str:
    """Minimal HTML-escape for captions/logs."""
    if not value:
        return ""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(value: str | None, n: int = 200) -> str:
    value = value or ""
    return value if len(value) <= n else value[: n - 1] + "…"


def source_link(chat_id: int, message_id: int) -> str | None:
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None
