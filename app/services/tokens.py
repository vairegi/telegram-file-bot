"""v4.9 — daily token wallet (replaces the 6-hour verification stamp).

Model (owner spec):
  * Solving the shortener grants `tokens_per_solve` tokens (default 11).
  * EVERY post delivered costs 1 token (a post with 3 PDFs still costs 1).
  * Tokens expire at the next 04:00 IST — i.e. "whatever you don't use is
    gone by 4 in the morning". A solve within 60 min of 4 AM rolls the
    expiry to the FOLLOWING 4 AM so nobody gets a 2-minute window.
  * 0 tokens (or expired) -> the shortener gate appears again.

Storage:
  Mongo:  verified_users { _id, tokens, token_expiry }  (same collection the
          old verification stamp used — no new collection, no migration).
  Turso:  settings JSON "token_wallets" = {uid: {tokens, expiry}} (dormant
          fallback path, mirroring repo.py's dual-backend approach).

Every function is failure-safe: a store hiccup must never break delivery.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Iterable, Optional

from zoneinfo import ZoneInfo

from . import repo

log = logging.getLogger("tokens")

IST = ZoneInfo("Asia/Kolkata")
RESET_HOUR = 4                 # 04:00 IST daily expiry
DEFAULT_PER_SOLVE = 11
MIN_WINDOW_SEC = 3600          # solves inside 60 min of 4 AM roll to next day
_SETTINGS_KEY = "token_wallets"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def next_reset(now: Optional[float] = None) -> float:
    """Epoch of the next 04:00 IST that is at least MIN_WINDOW_SEC away."""
    now = float(now if now is not None else time.time())
    t = _dt.datetime.fromtimestamp(now, tz=IST)
    four = t.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if t >= four or (four.timestamp() - now) < MIN_WINDOW_SEC:
        four = four + _dt.timedelta(days=1)
    return four.timestamp()


def expiry_label(exp: Optional[float]) -> str:
    if not exp:
        return "—"
    return _dt.datetime.fromtimestamp(float(exp), tz=IST).strftime(
        "%d %b, %I:%M %p IST")


async def per_solve() -> int:
    try:
        n = await repo.get_setting_int("tokens_per_solve", DEFAULT_PER_SOLVE)
    except Exception:
        return DEFAULT_PER_SOLVE
    return n if 1 <= int(n) <= 1000 else DEFAULT_PER_SOLVE


# ---------------------------------------------------------------------------
# Wallet read / write
# ---------------------------------------------------------------------------
async def get_wallet(user_id: int) -> dict:
    uid = int(user_id)
    if repo._mongo():
        try:
            from .. import mongo_db

            async def _op(db):
                d = await db.verified_users.find_one(
                    {"_id": uid}, {"tokens": 1, "token_expiry": 1})
                return d or {}
            d = await mongo_db.with_retry(_op)
        except Exception as e:
            log.info("wallet read failed for %s: %s", uid, e)
            d = {}
        return {"tokens": int(d.get("tokens") or 0),
                "expiry": float(d.get("token_expiry") or 0.0)}
    try:
        data = await repo.get_setting_json(_SETTINGS_KEY, {}) or {}
    except Exception:
        return {"tokens": 0, "expiry": 0.0}
    w = data.get(str(uid)) or {}
    return {"tokens": int(w.get("tokens") or 0),
            "expiry": float(w.get("expiry") or 0.0)}


async def get_many(user_ids: Iterable[int]) -> dict:
    """{uid: wallet} for the given uids (used by /verified_users table)."""
    uids = [int(u) for u in user_ids]
    out: dict = {}
    if repo._mongo():
        try:
            from .. import mongo_db

            async def _op(db):
                cur = db.verified_users.find(
                    {"_id": {"$in": uids}}, {"tokens": 1, "token_expiry": 1})
                return await cur.to_list(length=None)
            for d in await mongo_db.with_retry(_op):
                out[int(d["_id"])] = {"tokens": int(d.get("tokens") or 0),
                                      "expiry": float(d.get("token_expiry") or 0)}
        except Exception as e:
            log.info("wallet bulk read failed: %s", e)
        return out
    try:
        data = await repo.get_setting_json(_SETTINGS_KEY, {}) or {}
    except Exception:
        return out
    for u in uids:
        w = data.get(str(u)) or {}
        out[u] = {"tokens": int(w.get("tokens") or 0),
                  "expiry": float(w.get("expiry") or 0.0)}
    return out


async def _write(uid: int, tokens: int, expiry: float) -> None:
    if repo._mongo():
        from .. import mongo_db

        async def _op(db):
            await db.verified_users.update_one(
                {"_id": uid},
                {"$set": {"tokens": int(tokens),
                          "token_expiry": float(expiry),
                          "updated_at": _dt.datetime.now(_dt.timezone.utc)}},
                upsert=True)
            return True
        await mongo_db.with_retry(_op)
        return
    data = await repo.get_setting_json(_SETTINGS_KEY, {}) or {}
    data[str(uid)] = {"tokens": int(tokens), "expiry": float(expiry)}
    await repo.set_setting_json(_SETTINGS_KEY, data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def has_tokens(user_id: int) -> bool:
    """True when the user can fetch a post right now (unused tokens remain)."""
    try:
        w = await get_wallet(user_id)
        return int(w["tokens"]) > 0 and float(w["expiry"]) > time.time()
    except Exception:
        return False


async def grant(user_id: int, n: Optional[int] = None,
                now: Optional[float] = None) -> dict:
    """Give (SET, not add) `n` tokens expiring at the next 04:00 IST.
    Called once per successful shortener solve."""
    uid = int(user_id)
    amount = int(n if n is not None else await per_solve())
    exp = next_reset(now)
    try:
        await _write(uid, amount, exp)
    except Exception as e:
        log.warning("token grant failed for %s: %s", uid, e)
    return {"tokens": amount, "expiry": exp}


async def consume(user_id: int) -> int:
    """Atomically spend 1 token. Returns the REMAINING balance (0 = none)."""
    uid = int(user_id)
    now = time.time()
    if repo._mongo():
        try:
            from .. import mongo_db
            from pymongo import ReturnDocument

            async def _op(db):
                d = await db.verified_users.find_one_and_update(
                    {"_id": uid, "tokens": {"$gt": 0},
                     "token_expiry": {"$gt": now}},
                    {"$inc": {"tokens": -1}},
                    return_document=ReturnDocument.AFTER)
                return (d or {}).get("tokens", 0)
            return int(await mongo_db.with_retry(_op) or 0)
        except Exception as e:
            log.info("token consume failed for %s: %s", uid, e)
            return 0
    w = await get_wallet(uid)
    if w["tokens"] > 0 and w["expiry"] > now:
        try:
            await _write(uid, w["tokens"] - 1, w["expiry"])
        except Exception:
            pass
        return max(0, w["tokens"] - 1)
    return 0


async def refund(user_id: int) -> int:
    """Give back 1 token (used when a delivery failed after consuming)."""
    w = await get_wallet(user_id)
    if w["expiry"] > time.time():
        try:
            await _write(int(user_id), w["tokens"] + 1, w["expiry"])
        except Exception:
            pass
        return w["tokens"] + 1
    return w["tokens"]


async def active_count() -> int:
    """Users holding unexpired tokens (feeds /stats)."""
    now = time.time()
    if repo._mongo():
        try:
            from .. import mongo_db

            async def _op(db):
                return await db.verified_users.count_documents(
                    {"tokens": {"$gt": 0}, "token_expiry": {"$gt": now}})
            return int(await mongo_db.with_retry(_op) or 0)
        except Exception:
            return 0
    try:
        data = await repo.get_setting_json(_SETTINGS_KEY, {}) or {}
    except Exception:
        return 0
    return sum(1 for w in data.values()
               if int((w or {}).get("tokens") or 0) > 0
               and float((w or {}).get("expiry") or 0) > now)


async def wallet_line(user_id: int) -> str:
    """One-line HTML summary of the balance (for /mystats and DMs)."""
    w = await get_wallet(user_id)
    now = time.time()
    if w["tokens"] <= 0 or w["expiry"] <= now:
        return ("🔑 Tokens: <b>0</b> — solve the shortener to get "
                f"{await per_solve()} more.")
    return (f"🔑 Tokens: <b>{w['tokens']}</b> · expire "
            f"{expiry_label(w['expiry'])}")
