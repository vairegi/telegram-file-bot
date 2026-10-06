"""v5.0: LinkGuard client — talks to the self-hosted Cloudflare Workers gate.

    bot DM -> /<slug> (DOOR 1: Turnstile) -> /finish (DOOR 3a) -> shortener
           -> /finish2 (DOOR 3b) -> t.me/<bot>?start=verify_<token>

Every failure path returns None so callers can fall back to today's behaviour
(fail-open, non-negotiable). Admin commands live in handlers/linkguard_cmds.py.
"""
from __future__ import annotations

import logging
import urllib.parse

import aiohttp

from . import repo

log = logging.getLogger("linkguard")

REQ_TIMEOUT = aiohttp.ClientTimeout(total=12)
MAX_SLUG_LEN = 32


async def enabled() -> bool:
    return await repo.get_setting_bool("linkguard_enabled", False)


async def base_url() -> str:
    return ((await repo.get_setting("linkguard_base")) or "").strip().rstrip("/")


async def admin_key() -> str:
    return ((await repo.get_setting("linkguard_key")) or "").strip()


async def is_configured() -> bool:
    return bool(await base_url()) and bool(await admin_key())


async def _request(method, path, payload=None, params=None, timeout=12.0):
    """One authed admin call. Returns parsed JSON or None on ANY failure."""
    base = await base_url()
    key = await admin_key()
    if not base or not key:
        return None
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.request(method, base + path, json=payload,
                                    params=params,
                                    headers={"x-admin-key": key}) as resp:
                if resp.status >= 400:
                    log.warning("linkguard %s %s -> HTTP %s", method, path, resp.status)
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        log.warning("linkguard %s %s failed: %s", method, path, e)
        return None


async def mint(short_url, ttl_hours=168, grant_slug=None):
    return await _request("POST", "/api/admin/mint",
                          {"url": short_url, "ttl_hours": ttl_hours,
                           "grant_slug": grant_slug})


async def mint_return_grant(deep_link, ref_hosts=None, ttl_hours=168):
    return await _request("POST", "/api/admin/mint2",
                          {"url": deep_link, "ttl_hours": ttl_hours,
                           "ref_hosts": ref_hosts or []})


async def revoke(slug):
    r = await _request("POST", "/api/admin/revoke", {"slug": slug})
    return bool(r and r.get("ok"))


async def list_active():
    r = await _request("GET", "/api/admin/list")
    return (r or {}).get("slugs", []) or []


async def push_ref_hosts(hosts):
    r = await _request("POST", "/api/admin/ref_hosts", {"hosts": hosts})
    return bool(r and r.get("ok"))


async def get_ref_hosts():
    r = await _request("GET", "/api/admin/ref_hosts")
    return (r or {}).get("hosts", []) or []


async def add_decoys(n, ttl_hours=720):
    r = await _request("POST", "/api/admin/decoys", {"n": n, "ttl_hours": ttl_hours})
    return int((r or {}).get("created", 0))


async def get_logs(limit=25, reason=None, slug=None):
    params = {"limit": str(limit)}
    if reason:
        params["reason"] = reason
    if slug:
        params["slug"] = slug
    r = await _request("GET", "/api/admin/logs", params=params)
    return (r or {}).get("logs", []) or []


async def health():
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


def worker_host_from_url(url):
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""
