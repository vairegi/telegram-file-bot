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
from . import tokens as _tokens

log = logging.getLogger("shortener")

DEFAULT_TTL_HOURS = 6            # /setverifytime can override (1..168)
GATE_CLEANUP_SEC = 20 * 60       # v4.3.7: unsolved gate messages self-delete
_gate_msgs: dict = {}            # user_id -> gate message_id (tiny, in-process)
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
            "then come back — your file will be sent automatically.\n"
            "You will receive tokens for this solve — <b>1 token = 1 post</b>, "
            "valid until 4:00 AM IST.</blockquote>")


def _default_verify_text() -> str:
    return "✅ <b>Verified!</b> You're unlocked — your file is on its way."


# ---------------------------------------------------------------------------
# Tokens (Mongo-backed via repo.token_*) + verification stamps (repo.verified_*)
# ---------------------------------------------------------------------------
async def has_tokens(user_id: int) -> bool:
    """v4.9: access = unspent tokens (replaces the 6-hour stamp)."""
    return await _tokens.has_tokens(int(user_id))


async def is_verified(user_id: int) -> bool:
    """Kept as an alias — “verified” now means “has tokens left”."""
    return await _tokens.has_tokens(int(user_id))


async def _mark_verified(user_id: int) -> None:
    """v4.9: a successful solve grants TODAY'S token budget."""
    await _tokens.grant(int(user_id))


async def consume_for_post(user_id: int):
    """Spend 1 token for this post; return a ready-to-send HTML line
    (or None when the wallet is unreadable). 1 post = 1 token, no matter
    how many files are attached."""
    try:
        left = await _tokens.consume(int(user_id))
        w = await _tokens.get_wallet(int(user_id))
        if not w.get("expiry"):
            return None
        lbl = _tokens.expiry_label(w.get("expiry"))
        if left <= 0:
            return ("🔑 That was your last token — you have <b>0</b> left.\n"
                    f"Solve the shortener again for "
                    f"<b>{await _tokens.per_solve()}</b> more.")
        return (f"🔑 <b>{left}</b> token(s) left · expire {lbl}.\n"
                f"Check anytime with /mystats.")
    except Exception:
        return None


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
    through the shortener's GET LINK (the deep-link IS the proof).
    NOTE: consumed WITHOUT the timing check — use consume_token_timed() for
    the /start verify_ path (v4.4 anti-bypass)."""
    code = await repo.token_consume(token, int(user_id))
    if code is None:
        return None
    await _mark_verified(user_id)
    return code


# ---- v4.4: anti-bypass timing ----
MIN_SOLVE_SEC = 120        # faster than this = bypass tool
LEGIT_SOLVE_SEC = 150      # at/above this = legitimately solved (decay strikes)


async def consume_token_timed(token: str, user_id: int):
    """Timing-aware redemption for /start verify_TOKEN.
    Returns (status, data):
      ("ok", code)             — legit solve (elapsed >= 120s); strikes decay
                                 to 0 when elapsed >= 150s
      ("bypass", elapsed)      — too fast; token NOT consumed; +1 strike
      ("invalid", None)        — unknown / wrong-user / expired token
    """
    rec = await repo.token_get(token)
    if not rec or int(rec.get("user_id", -1)) != int(user_id):
        return ("invalid", None)
    issued = rec.get("issued_at")
    elapsed = (time.time() - float(issued)) if issued else 10**9  # legacy: pass
    if elapsed < MIN_SOLVE_SEC:
        strikes = await repo.strikes_inc(int(user_id))
        return ("bypass", {"elapsed": elapsed, "strikes": strikes})
    if elapsed >= LEGIT_SOLVE_SEC:
        await repo.strikes_reset(int(user_id))   # legit solve decays strikes
    code = await repo.token_consume(token, int(user_id))
    if code is None:
        return ("invalid", None)               # raced/expired between reads
    await _mark_verified(user_id)
    return ("ok", code)


async def token_count() -> int:
    return await repo.token_count()


async def unlocked_count() -> int:
    """v4.9: users holding unexpired tokens."""
    return await _tokens.active_count()


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
async def delete_gate_now(bot, user_id: int) -> None:
    """v4.3.7: delete the user's pending gate message immediately (called on
    successful verification). No-ops when there is none."""
    mid = _gate_msgs.pop(int(user_id), None)
    if not mid:
        return
    from . import tg as _tg
    try:
        await _tg.delete_message(bot, chat_id=int(user_id), message_id=mid)
    except Exception:
        pass  # already gone — harmless


def clear_gate(user_id: int) -> None:
    """Drop the gate record WITHOUT deleting (used when the gate message was
    edited into the success text instead of deleted)."""
    _gate_msgs.pop(int(user_id), None)


async def delete_gate_later(bot, user_id: int, message_id: int,
                            delay: int = GATE_CLEANUP_SEC) -> None:
    """v4.3.7: delete an unsolved gate message after 20 min so chats stay
    clean. Skips if the record now points at a NEWER gate (user re-tapped
    Get File) or the gate was already cleared by a successful verify."""
    import asyncio
    from . import tg as _tg
    try:
        await asyncio.sleep(delay)
        if _gate_msgs.get(int(user_id)) != message_id:
            return  # verified already, or replaced by a newer gate
        await _tg.delete_message(bot, chat_id=int(user_id), message_id=message_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if _gate_msgs.get(int(user_id)) == message_id:
            _gate_msgs.pop(int(user_id), None)


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

    # v5.0: Three-Door chain (fail-open on every hop). When LinkGuard is
    # enabled AND configured AND the worker answers, the gate button leads to
    # the PUBLIC worker slug: /<slug> (Turnstile) -> /finish (entry token)
    # -> shortener -> /finish2 (grant) -> the deep link below. The raw
    # verify_<tok> deep link never leaves the server.
    short: str | None = None
    linkguard_url: str | None = None
    try:
        from . import linkguard as _lg
        if await _lg.enabled() and await _lg.is_configured():
            api_host = _lg.worker_host_from_url(await get_api_base())
            extras = (await repo.get_setting_json(
                "linkguard_ref_hosts_extra", [])) or []
            ref_hosts = []
            for h in ([api_host] + [str(x).strip().lower() for x in extras]):
                if h and h not in ref_hosts:
                    ref_hosts.append(h)
            g = await _lg.mint_return_grant(destination, ref_hosts=ref_hosts)
            if g and g.get("finish2_url"):
                try:
                    finish2_short = await make_short_url(g["finish2_url"])
                except Exception as e:
                    log.warning("linkguard: shorten(finish2) failed: %s — "
                                "falling back to direct deep-link flow", e)
                    finish2_short = None
                if finish2_short:
                    p = await _lg.mint(finish2_short, grant_slug=g.get("slug"))
                    if p and p.get("url"):
                        linkguard_url = p["url"]
            if linkguard_url is None:
                log.warning("linkguard: mint chain incomplete — fail-open")
    except Exception as e:
        log.exception("linkguard chain crashed — fail-open: %s", e)

    if linkguard_url is None:
        # Original v4.x behaviour: VPLINK wraps the deep link directly.
        try:
            short = await make_short_url(destination)
        except Exception as e:
            log.exception("vplink shorten failed: %s — failing OPEN", e)
            return False  # never lock users out because the shortener API hiccuped

    button_url = linkguard_url or short
    rows = [[InlineKeyboardButton(text="🔓 Verify & Unlock", url=button_url)]]
    for label, url in await get_secondary_buttons():
        rows.append([InlineKeyboardButton(text=label, url=url)])
    # v4.3.4: "I've Verified" returns as a CALLBACK button — callback_data is
    # NOT a link, so no token can leak or be forged. The handler looks up the
    # user's completed token in MongoDB and only then redeems it. This also
    # rescues users whose landing-page redirect back to Telegram never fired.
    rows.append([InlineKeyboardButton(text="✅ I've Verified — Continue",
                                      callback_data="vrfchk:" + (code or ""))])

    r = await bot.send_message(chat_id=user_id, text=await get_gate_text(),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               parse_mode="HTML")
    mid = getattr(r, "message_id", None)
    if mid:
        _gate_msgs[int(user_id)] = mid
        import asyncio as _aio
        _aio.create_task(delete_gate_later(bot, user_id, mid))
    return True


