"""v4.3.3: VPLINK shortener verification gate.

Flow: unverified user taps Get File -> bot DMs a gate message with the
hardcoded "🔓 Verify & Unlock" button pointing to an auto-generated VPLINK
short URL that wraps  <BASE_WEBHOOK_URL>/verify?t=<TOKEN> . When the user
finishes the shortener, VPLINK redirects to the Render /verify landing page,
which marks the token completed and redirects them to
t.me/<bot>?start=verify_<TOKEN> . The bot validates the token atomically
(single-use, user-bound), stamps a DB-backed unlock good for
verify_ttl_hours (default 6), then auto-delivers the file they tapped.

Security model (v4.3.3):
  * Tokens live in MongoDB `verify_tokens` — single-use (find_one_and_delete),
    user-bound, TTL index on created_at (900s) auto-cleans abandoned tokens.
  * The "✅ I've Verified" button was REMOVED — it leaked the raw verify_TOKEN
    deep-link and let users skip the shortener. The ONLY way a token becomes
    redeemable is the /verify landing page (reachable only via the short link).
  * Verification stamps live in MongoDB `verified_users` (restart-proof).

Admin commands live in handlers/shortener_cmds.py.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
import urllib.parse

from . import repo

log = logging.getLogger("shortener")

DEFAULT_TTL_HOURS = 6            # /setverifytime can override (1..168)
MAX_SECONDS_PER_VERIFY = 30      # aiohttp budget for the VPLINK API call


# ---------------------------------------------------------------------------
# Settings helpers (shared settings store — works on Mongo and Turso)
# ---------------------------------------------------------------------------
async def is_enabled() -> bool:
    return await repo.get_setting_bool("shortener_enabled", False)


async def get_api_base() -> str:
    return ((await repo.get_setting("shortener_api")) or "").strip()


async def get_ttl_hours() -> float:
    raw = (await repo.get_setting("verify_ttl_hours")) or ""
    try:
        h = float(raw)
        return h if 1 <= h <= 168 else DEFAULT_TTL_HOURS
    except (TypeError, ValueError):
        return DEFAULT_TTL_HOURS


async def get_gate_text() -> str:
    return (((await repo.get_setting("shortener_bot_msg")) or "").strip()
            or _default_gate_text())


async def get_overlay_text() -> str:
    return (((await repo.get_setting("shortener_msg")) or "").strip()
            or _default_gate_text())


async def get_verify_text() -> str:
    return (((await repo.get_setting("verify_msg")) or "").strip()
            or _default_verify_text())


async def get_secondary_buttons() -> list:
    rows = (await repo.get_setting_json("shortener_buttons", [])) or []
    out = []
    for pair in rows:
        try:
            label, url = str(pair[0])[:60], str(pair[1])
        except Exception:
            continue
        if label and url:
            out.append([label, url])
    return out


def _default_gate_text() -> str:
    return ("🔒 <b>Verification required</b>\n\n"
            "To receive this file, please complete a quick one-time "
            "verification.\n"
            "<blockquote>Tap <b>🔓 Verify & Unlock</b>, finish the short link, "
            "then come back — your file will be sent automatically.</blockquote>")


def _default_verify_text() -> str:
    return "✅ <b>Verified!</b> You're unlocked — your file is on its way."


# ---------------------------------------------------------------------------
# Tokens (Mongo-backed via repo.token_*) + verification stamps (repo.verified_*)
# ---------------------------------------------------------------------------
async def is_verified(user_id: int) -> bool:
    """DB-backed: survives restarts. Reads the stored wall-clock expiry."""
    exp = await repo.verified_get(int(user_id))
    return bool(exp and exp > time.time())


async def _mark_verified(user_id: int) -> None:
    await repo.verified_set(int(user_id),
                            time.time() + (await get_ttl_hours()) * 3600)


async def new_token(user_id: int, code: str) -> str:
    """Mint a one-time, user-bound token (Mongo; TTL-cleans after 15 min)."""
    tok = secrets.token_urlsafe(18)
    await repo.token_create(tok, int(user_id), code)
    return tok


async def peek_token(token: str) -> dict | None:
    """Read a token WITHOUT consuming it (used by the /verify landing page)."""
    return await repo.token_get(token)


async def mark_completed(token: str) -> bool:
    """Called by the /verify landing page AFTER the shortener redirects here.
    This is the ONLY way a token becomes redeemable."""
    return await repo.token_mark_completed(token)


async def consume_token(token: str, user_id: int):
    """Strict, atomic, single-use redemption (Mongo findOneAndDelete):
    token must exist, be completed by the landing page, and belong to THIS
    user. The doc is DELETED in the same operation -> cannot be reused, and
    a different user can never redeem it. Returns the cover code or None."""
    code = await repo.token_consume(token, int(user_id))
    if code is None:
        return None
    await _mark_verified(user_id)
    return code


async def token_count() -> int:
    return await repo.token_count()


async def unlocked_count() -> int:
    """Verified users currently unlocked (DB)."""
    return await repo.verified_count()


# ---------------------------------------------------------------------------
# VPLINK shortening (vplink.in style:  ?api=TOKEN&url=<dest> )
# ---------------------------------------------------------------------------
async def make_short_url(destination: str) -> str:
    """Wrap `destination` in a VPLINK short link via the configured API base.
    Auto-detects JSON {"status":"success","shortenedUrl":...} vs plain text."""
    base = await get_api_base()
    if not base:
        raise RuntimeError("shortener API not configured (/shortenerapi)")
    import aiohttp
    url = base + urllib.parse.quote(destination, safe="")
    timeout = aiohttp.ClientTimeout(total=MAX_SECONDS_PER_VERIFY)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as resp:
            body = (await resp.text()).strip()
    if body.startswith("{"):
        import json
        data = json.loads(body)
        if data.get("status") == "success" and data.get("shortenedUrl"):
            return data["shortenedUrl"]
        raise RuntimeError(f"vplink error: {data.get('message', body[:120])}")
    if body.lower().startswith("http"):
        return body
    raise RuntimeError(f"unexpected vplink response: {body[:120]}")


# ---------------------------------------------------------------------------
# Gate message (hardcoded primary button + optional secondary buttons)
# ---------------------------------------------------------------------------
async def send_gate(bot, user_id: int, code: str) -> bool:
    """DM the verification gate. True = gate sent (delivery must STOP),
    False = shortener disabled/misconfigured/verified (delivery proceeds)."""
    if not await is_enabled():
        return False
    if await is_verified(user_id):
        return False
    if not await get_api_base():
        log.warning("shortener ON but /shortenerapi not set — failing OPEN")
        return False

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from ..config import settings

    tok = await new_token(user_id, code)
    base_url = (settings.base_webhook_url or "").rstrip("/")
    destination = f"{base_url}/verify?t={tok}"
    try:
        short = await make_short_url(destination)
    except Exception as e:
        log.exception("vplink shorten failed: %s — failing OPEN", e)
        return False  # never lock users out because the shortener API hiccuped

    rows = [[InlineKeyboardButton(text="🔓 Verify & Unlock", url=short)]]
    for label, url in await get_secondary_buttons():
        rows.append([InlineKeyboardButton(text=label, url=url)])
    # NOTE: there is deliberately NO "I've Verified" button — it would leak
    # the verify_TOKEN deep-link and bypass the shortener entirely (v4.3.3).

    await bot.send_message(chat_id=user_id, text=await get_gate_text(),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           parse_mode="HTML")
    return True


# ---------------------------------------------------------------------------
# /verify landing page (aiohttp) — auto-redirect + fallback button
# ---------------------------------------------------------------------------
def landing_html(deep_link: str, heading: str) -> str:
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
    heading = re.sub(r"<[^>]+>", "", heading)  # strip HTML for the page title
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1;url={esc(deep_link)}">
<title>Verified ✅</title>
<style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;
       justify-content:center;background:#0f172a;color:#e2e8f0;
       font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
  .card{{text-align:center;padding:40px 28px;max-width:420px}}
  .tick{{font-size:64px;line-height:1}}
  h1{{font-size:22px;margin:16px 0 8px}}
  p{{color:#94a3b8;font-size:14px;margin:0 0 24px}}
  a.btn{{display:inline-block;background:#22c55e;color:#052e16;
        font-weight:700;text-decoration:none;padding:14px 28px;
        border-radius:12px;font-size:16px}}
  a.btn:active{{transform:scale(.97)}}
  .hint{{margin-top:18px;font-size:12px;color:#64748b}}
</style></head>
<body><div class="card">
  <div class="tick">✅</div>
  <h1>{esc(heading)}</h1>
  <p>Verification complete — taking you back to Telegram…</p>
  <a class="btn" href="{esc(deep_link)}">✅ Continue in Telegram</a>
  <div class="hint">If nothing happens, tap the button above.</div>
</div>
<script>setTimeout(function(){{window.location.href={deep_link!r}}},900);</script>
</body></html>"""
