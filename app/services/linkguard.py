"""v5.0: LinkGuard client — talks to the self-hosted Cloudflare Workers gate.

The Three-Door system puts a Turnstile-protected landing page IN FRONT of the
paid shortener and a guarded EXIT after it:

    bot DM -> /<slug> (DOOR 1: Turnstile) -> /finish (DOOR 3a) -> shortener
           -> /finish2 (DOOR 3b) -> t.me/<bot>?start=verify_<token>

This module is ONLY a thin async client. Every failure path returns None so
callers (shortener.send_gate) can FALL BACK to today's behaviour — LinkGuard
going down must never lock a paying user out (fail-open, non-negotiable).

Admin commands live in handlers/linkguard_cmds.py.
"""
from __future__ import annotations

import logging
import urllib.parse

import aiohttp

from . import repo

log = logging.getLogger("linkguard")

# Shared settings keys (work on Mongo and Turso via repo settings store):
#   linkguard_enabled : "1"/None
#   linkguard_base    : https://<worker>.workers.dev
#   linkguard_key     : ADMIN_KEY shared with the worker
#   linkguard_ref_hosts_extra : JSON list of manually-added referer hosts

REQ_TIMEOUT = aiohttp.ClientTimeout(total=12)   # worker must stay snappy
MAX_SLUG_LEN = 32                               # worker-side cap


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
async def enabled() -> bool:
    return await repo.get_setting_bool("linkguard_enabled", False)


async def base_url() -> str:
    return ((await repo.get_setting("linkguard_base")) or "").strip().rstrip("/")


async def admin_key() -> str:
    return ((await repo.get_setting("linkguard_key")) or "").strip()


async def is_configured() -> bool:
    return bool(await base_url()) and bool(await admin_key())


async def _request(method: str, path: str, payload: dict | None = None,
                   params: dict | None = None, timeout: float = 12.0) -> dict | None:
    """One authed admin call. Returns parsed JSON dict or None on ANY failure
    (network, timeout, non-2xx, bad JSON) — fail-open by contract."""
    base = await base_url()
    key = await admin_key()
    if not base or not key:
        return None
    url = base + path
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.request(method, url, json=payload, params=params,
                                    headers={"x-admin-key": key}) as resp:
                if resp.status >= 400:
                    log.warning("linkguard %s %s -> HTTP %s", method, path, resp.status)
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        log.warning("linkguard %s %s failed: %s", method, path, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def mint(short_url: str, ttl_hours: int = 168,
               grant_slug: str | None = None) -> dict | None:
    """DOOR-1/3a mint: bind a public slug to the REAL shortener URL.
    Returns {slug, url} or None (fail-open)."""
    return await _request("POST", "/api/admin/mint",
                          {"url": short_url, "ttl_hours": ttl_hours,
                           "grant_slug": grant_slug})


async def mint_return_grant(deep_link: str, ref_hosts: list[str] | None = None,
                            ttl_hours: int = 168) -> dict | None:
    """DOOR-3b mint: bind a grant slug to the raw Telegram deep link.
    Returns {slug, finish2_url} or None (fail-open)."""
    return await _request("POST", "/api/admin/mint2",
                          {"url": deep_link, "ttl_hours": ttl_hours,
                           "ref_hosts": ref_hosts or []})


async def revoke(slug: str) -> bool:
    r = await _request("POST", "/api/admin/revoke", {"slug": slug})
    return bool(r and r.get("ok"))


async def list_active() -> list[str]:
    r = await _request("GET", "/api/admin/list")
    return (r or {}).get("slugs", []) or []


async def push_ref_hosts(hosts: list[str]) -> bool:
    r = await _request("POST", "/api/admin/ref_hosts", {"hosts": hosts})
    return bool(r and r.get("ok"))


async def get_ref_hosts() -> list[str]:
    r = await _request("GET", "/api/admin/ref_hosts")
    return (r or {}).get("hosts", []) or []


async def add_decoys(n: int, ttl_hours: int = 720) -> int:
    r = await _request("POST", "/api/admin/decoys", {"n": n, "ttl_hours": ttl_hours})
    return int((r or {}).get("created", 0))


async def get_logs(limit: int = 25, reason: str | None = None,
                   slug: str | None = None) -> list[dict]:
    params: dict = {"limit": str(limit)}
    if reason:
        params["reason"] = reason
    if slug:
        params["slug"] = slug
    r = await _request("GET", "/api/admin/logs", params=params)
    return (r or {}).get("logs", []) or []


async def health() -> dict | None:
    """Unauthenticated /api/health — no key needed, still fail-open."""
    base = await base_url()
    if not base:
        return None
    try:
        async with aiohttp.ClientSession(timeout=REQ_TIMEOUT) as sess:
            async with sess.get(base + "/api/health") as resp:
                if resp.status >= 400:
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        log.warning("linkguard health failed: %s", e)
        return None


def worker_host_from_url(url: str) -> str:
    """Extract hostname from any URL; '' when unparseable."""
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""
