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

import asyncio
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
    for provider in await get_providers():
        if provider["name"].casefold() == "vplink":
            return provider["api"]
    return ""


# ---------------------------------------------------------------------------
# v5.4: uniform provider registry. The old settings are read-only migration
# inputs; an explicitly empty v2 registry must NEVER resurrect legacy vplink.
EXTRA_PROVIDERS_KEY = "shortener_providers"
PROVIDERS_KEY = "shortener_registry_v2"
ROTATION_KEY = "shortener_rotation"
_provider_lock = asyncio.Lock()  # serialize admin edits in this bot process


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""


def _providers_validated(rows):
    out, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:40]
        api = str(row.get("api") or "").strip()
        if not name or not api or name.casefold() in seen:
            continue
        hosts = row.get("hosts") or []
        if not isinstance(hosts, list):
            hosts = []
        out.append({"name": name, "api": api,
                    "hosts": [str(h).strip().lower() for h in hosts if str(h).strip()],
                    "enabled": row.get("enabled", True) is not False})
        seen.add(name.casefold())
    return out


async def get_extra_providers() -> list:
    # Kept for external callers; all commands now use the uniform registry.
    return [p for p in await get_providers() if p["name"].casefold() != "vplink"]


async def get_providers() -> list:
    rows = await repo.get_setting_json(PROVIDERS_KEY, None)
    if rows is not None:
        return _providers_validated(rows)
    # Backwards compatible: no deployment-time write or live DB migration.
    api = ((await repo.get_setting("shortener_api")) or "").strip()
    rows = [{"name": "vplink", "api": api, "hosts": []}] if api else []
    extras = await repo.get_setting_json(EXTRA_PROVIDERS_KEY, [])
    if isinstance(extras, list):
        rows.extend(extras)
    return _providers_validated(rows)


async def active_providers() -> list:
    return [p for p in await get_providers() if p["enabled"]]


async def all_provider_hosts() -> list:
    # Retain paused hosts for outstanding links; removed hosts in already
    # minted worker grants are also unaffected by this bot-only change.
    seen, out = set(), []
    for p in await get_providers():
        for h in [_host_of(p["api"])] + p["hosts"]:
            if h and h not in seen:
                seen.add(h)
                out.append(h)
    return out


def _cursor_index(providers, cursor):
    if isinstance(cursor, str) and not cursor.isdigit():
        return next((i for i, p in enumerate(providers)
                     if p["name"].casefold() == cursor.casefold()), 0)
    try:
        return int(cursor) % max(1, len(providers))
    except (TypeError, ValueError):
        return 0


def _next_active(providers, start):
    for offset in range(len(providers)):
        i = (start + offset) % len(providers)
        if providers[i]["enabled"]:
            return i
    return None


async def current_provider_index(user_id: int) -> int:
    """Registry index (including paused rows); -1 means none active."""
    providers = await get_providers()
    rot = (await repo.get_setting_json(ROTATION_KEY, {})) or {}
    start = _cursor_index(providers, rot.get(str(int(user_id)), 0))
    idx = _next_active(providers, start)
    return -1 if idx is None else idx


async def advance_provider(user_id: int) -> None:
    try:
        async with _provider_lock:
            providers = await get_providers()
            rot = (await repo.get_setting_json(ROTATION_KEY, {})) or {}
            start = _cursor_index(providers, rot.get(str(int(user_id)), 0))
            current = _next_active(providers, start)
            if current is None:
                return
            nxt = _next_active(providers, current + 1)
            rot[str(int(user_id))] = providers[nxt]["name"]
            await repo.set_setting_json(ROTATION_KEY, rot)
    except Exception as e:
        log.warning("provider rotation advance failed: %s", type(e).__name__)


def _find_provider(providers, target):
    target = str(target).strip()
    if target.isdigit():
        idx = int(target)
        if 0 <= idx < len(providers):
            return idx
    else:
        for i, p in enumerate(providers):
            if p["name"].casefold() == target.casefold():
                return i
    raise ValueError("No such shortener. See /shorteners for its name or current index.")


async def _save_providers(old, new):
    # Convert legacy integer cursors BEFORE changing the list. Name cursors
    # survive index shifts. A removed target advances to the next remaining
    # provider in the OLD circular order, not a hard-coded vplink fallback.
    rot = (await repo.get_setting_json(ROTATION_KEY, {})) or {}
    names = {p["name"].casefold() for p in new}
    for uid, cursor in list(rot.items()):
        if not old:
            continue
        start = _cursor_index(old, cursor)
        candidates = old[start:] + old[:start]
        dest = next((p["name"] for p in candidates if p["name"].casefold() in names), None)
        if dest is None:
            rot.pop(uid, None)
        else:
            rot[uid] = dest
    await repo.set_setting_json(ROTATION_KEY, rot)
    await repo.set_setting_json(PROVIDERS_KEY, new)


def validate_provider(name, api, hosts):
    name = str(name).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", name):
        raise ValueError("Name must start with a letter; use up to 40 letters, digits, _ or -.")
    parsed = urllib.parse.urlsplit(api)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or not api.endswith("url=") or ("url", "") not in query):
        raise ValueError("API must be an HTTP(S) template ending in the empty url= parameter.")
    clean = []
    for host in hosts:
        host = str(host).strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
            raise ValueError("Redirect hosts must be hostnames, not full URLs.")
        if host not in clean:
            clean.append(host)
    return {"name": name, "api": api, "hosts": clean, "enabled": True}


async def add_provider(name, api, hosts=(), replace_legacy=False):
    provider = validate_provider(name, api, hosts)
    async with _provider_lock:
        old = await get_providers()
        new = [dict(p) for p in old]
        matches = [i for i, p in enumerate(new) if p["name"].casefold() == name.casefold()]
        if matches:
            if not replace_legacy or name.casefold() != "vplink":
                raise ValueError("A shortener with this name already exists. Remove it first to replace it.")
            provider["hosts"] = new[matches[0]]["hosts"]
            provider["enabled"] = new[matches[0]]["enabled"]
            new[matches[0]] = provider
        else:
            new.append(provider)
        await _save_providers(old, new)
        return provider


async def remove_provider(target):
    async with _provider_lock:
        old = await get_providers()
        idx = _find_provider(old, target)
        removed = old[idx]
        await _save_providers(old, old[:idx] + old[idx + 1:])
        # The authoritative v2 list is already saved; clearing this legacy
        # secret also prevents accidentally exposing a deleted vplink API.
        if removed["name"].casefold() == "vplink":
            await repo.set_setting("shortener_api", None)
        return removed


async def set_provider_enabled(target, enabled):
    async with _provider_lock:
        old = await get_providers()
        idx = _find_provider(old, target)
        new = [dict(p) for p in old]
        new[idx]["enabled"] = bool(enabled)
        await _save_providers(old, new)
        return new[idx]


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
    """v4.9: a successful solve grants TODAY'S token budget.
    v5.1: ...and moves the user to the NEXT shortener for their next solve."""
    await _tokens.grant(int(user_id))
    await advance_provider(user_id)


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
    # The token record is already consumed; use our pre-consume snapshot.
    # Log only on successful atomic redemption, never on a rescue callback.
    try:
        from . import richlists
        await richlists.record_verification(
            user_id, elapsed=(time.time() - float(rec["sent_at"]))
            if rec.get("sent_at") else None,
            link_type=rec.get("provider") or "unknown",
            sent_at=rec.get("sent_at"), event_id=token)
    except Exception:
        log.exception("verification audit failed (delivery continues)")
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
    """Compatibility wrapper: use the first ACTIVE provider, never a paused
    legacy fallback. User-specific gates use make_short_url_for instead."""
    providers = await active_providers()
    if not providers:
        raise RuntimeError("No active shortener configured")
    return await make_short_url_for(providers[0]["api"], destination)


async def make_short_url_for(api_base: str, destination: str) -> str:
    """Wrap `destination` in a short link via the GIVEN API base (v5.1).
    Auto-detects JSON {"status":"success","shortenedUrl":...} vs plain text."""
    base = (api_base or "").strip()
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
    # Select from one configuration snapshot. Paused/deleted providers are
    # never used as error fallbacks, including the LinkGuard fallback path.
    providers = await get_providers()
    rot = (await repo.get_setting_json(ROTATION_KEY, {})) or {}
    start = _cursor_index(providers, rot.get(str(int(user_id)), 0))
    p_idx = _next_active(providers, start)
    if p_idx is None:
        log.warning("shortener ON but no active providers — failing OPEN")
        return False
    provider = providers[p_idx]
    api_base = provider["api"]
    log.info("gate for %s via provider %d (%s)", user_id, p_idx,
             provider["name"])

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
            # v5.1: the allowlist covers EVERY provider (API hosts + declared
            # redirect hosts) + manual extras — the rotation may route this
            # user through any configured shortener.
            extras = (await repo.get_setting_json(
                "linkguard_ref_hosts_extra", [])) or []
            ref_hosts = []
            for h in (await all_provider_hosts()) + [str(x).strip().lower()
                                                     for x in extras]:
                if h and h not in ref_hosts:
                    ref_hosts.append(h)
            g = await _lg.mint_return_grant(destination, ref_hosts=ref_hosts)
            if g and g.get("finish2_url"):
                try:
                    finish2_short = await make_short_url_for(
                        api_base, g["finish2_url"])
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
        # Original v4.x behaviour: shortener wraps the deep link directly
        # (v5.1: via whichever provider the rotation selected for this user).
        try:
            short = await make_short_url_for(api_base, destination)
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
    try:
        sent_date = getattr(r, "date", None)
        sent_at = sent_date.timestamp() if hasattr(sent_date, "timestamp") else time.time()
        await repo.token_set_metadata(tok, provider["name"], sent_at)
    except Exception:
        log.exception("gate metadata unavailable (do not infer provider/timing)")
    mid = getattr(r, "message_id", None)
    if mid:
        _gate_msgs[int(user_id)] = mid
        import asyncio as _aio
        _aio.create_task(delete_gate_later(bot, user_id, mid))
    return True


