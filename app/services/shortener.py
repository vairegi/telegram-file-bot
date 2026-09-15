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
    token must exist and belong to THIS user. The doc is DELETED in the same
    operation -> cannot be reused, and a different user can never redeem it.
    v4.3.5: no 'completed' flag — reaching this point means the user came
    through the shortener's GET LINK (the deep-link IS the proof)."""
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
    # v4.3.5: wrap the TELEGRAM deep-link itself in the shortener (the proven
    # flow) — "GET LINK" on the last shortener page opens the bot directly.
    # No Render landing page, no webview redirect to fail.
    from .posting import get_bot_username
    username = await get_bot_username(bot)
    if not username:
        log.warning("shortener: bot username unavailable — failing OPEN")
        return False
    destination = f"https://t.me/{username}?start=verify_{tok}"
    try:
        short = await make_short_url(destination)
    except Exception as e:
        log.exception("vplink shorten failed: %s — failing OPEN", e)
        return False  # never lock users out because the shortener API hiccuped

    rows = [[InlineKeyboardButton(text="🔓 Verify & Unlock", url=short)]]
    for label, url in await get_secondary_buttons():
        rows.append([InlineKeyboardButton(text=label, url=url)])
    # v4.3.4: "I've Verified" returns as a CALLBACK button — callback_data is
    # NOT a link, so no token can leak or be forged. The handler looks up the
    # user's completed token in MongoDB and only then redeems it. This also
    # rescues users whose landing-page redirect back to Telegram never fired.
    rows.append([InlineKeyboardButton(text="✅ I've Verified — Continue",
                                      callback_data="vrfchk:" + (code or ""))])

    await bot.send_message(chat_id=user_id, text=await get_gate_text(),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           parse_mode="HTML")
    return True


