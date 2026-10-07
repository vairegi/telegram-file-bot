"""v5.0: offline tests for the LinkGuard Three-Door security system.
1. Python mirror of the worker's token/grant crypto + denial matrices.
2. Bot-side client + send_gate integration — mint payloads + FAIL-OPEN.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import sys
import time
import types

import pytest

sys.path.insert(0, "/home/user/telegram-file-bot")

from app.services import linkguard as lg          # noqa: E402
from app.services import repo                     # noqa: E402
from app.services import shortener as sh          # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


SECRET = b"test-signing-secret-0123456789abcdef"
OWN_HOST = "linkguard.test.workers.dev"


def _sign(payload: str) -> str:
    return _b64url(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())


def make_token(slug, session_id, exp, rnd="r1"):
    payload = _b64url(f"{slug}.{session_id}.{exp}.{rnd}".encode())
    return payload + "." + _sign(payload)


def make_grant(slug, exp, rnd="g1"):
    payload = _b64url(f"{slug}.{exp}.{rnd}".encode())
    return payload + "." + _sign(payload)


def _parse_signed(tok):
    if not tok or tok.count(".") != 1:
        return None
    payload, sig = tok.split(".", 1)
    if not payload or not sig:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        return base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)).decode().split(".")
    except Exception:
        return None


def door3a_check(tok, *, referer, claims, now_):
    if not (referer or "").lower().startswith(f"https://{OWN_HOST}/"):
        return "bad_referer"
    fields = _parse_signed(tok)
    if not fields or len(fields) != 4:
        return "bad_signature"
    slug = fields[0]
    try:
        if now_ > int(fields[2]):
            return "expired"
    except ValueError:
        return "bad_signature"
    row = claims.get(tok)
    if row is None:
        return "unknown_token"
    if row["used"]:
        return "token_used"
    row["used"] = True                      # burn BEFORE redirecting
    return f"ok:{slug}"


def door3b_check(slug, grant, *, referer, ref_hosts, grants, session, ua,
                 landing_host, now_):
    if not slug or len(slug) > 32:
        return "bad_slug"
    g = grants.get(slug)
    if g is None:
        return "unknown_grant"
    if g["used"]:
        return "grant_used"
    ref = (referer or "").lower()
    if not (ref and any(ref == "https://" + h or
                        ref.startswith("https://" + h + "/") or
                        ref.startswith("https://" + h + "?") for h in ref_hosts)):
        return "bad_referer"
    fields = _parse_signed(grant)
    if not fields or len(fields) != 3 or fields[0] != slug:
        return "bad_signature"
    try:
        if now_ > int(fields[1]):
            return "expired"
    except ValueError:
        return "bad_signature"
    if session is not None:
        if landing_host == OWN_HOST:
            if session.get("ua") and session["ua"] != ua:
                return "ua_mismatch"
        else:
            return "wrong_host"
    g["used"] = True
    return "ok"


class TestCryptoMirror:
    def test_valid_token_parses(self):
        tok = make_token("slug1", "sess1", int(time.time()) + 90)
        assert _parse_signed(tok)[:2] == ["slug1", "sess1"]

    def test_forged_signature_rejected(self):
        payload = make_token("slug1", "sess1", int(time.time()) + 90).split(".", 1)[0]
        assert _parse_signed(payload + "." + _b64url(b"x" * 32)) is None

    def test_tampered_payload_rejected(self):
        tok = make_token("slug1", "sess1", int(time.time()) + 90)
        payload, sig = tok.split(".", 1)
        fields = base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)).decode().split(".")
        fields[0] = "slug2"
        assert _parse_signed(_b64url(".".join(fields).encode()) + "." + sig) is None

    def test_wrong_secret_rejected(self):
        global SECRET
        tok = make_token("slug1", "sess1", int(time.time()) + 90)
        old, SECRET = SECRET, b"another-secret-key-0987654321"
        try:
            assert _parse_signed(tok) is None
        finally:
            SECRET = old

    def test_malformed_tokens_rejected(self):
        for bad in ("", "abc", "a.b.c", ".sig", "pay.", "!!!.!!!", "a..b"):
            assert _parse_signed(bad) is None

    def test_expired_token_denied(self):
        now_ = int(time.time())
        tok = make_token("slug1", "s", now_ - 1)
        assert door3a_check(tok, referer=f"https://{OWN_HOST}/slug1",
                            claims={tok: {"used": False}}, now_=now_) == "expired"

    def test_replay_denied_and_burned_once(self):
        now_ = int(time.time())
        tok = make_token("slug1", "s", now_ + 90)
        claims = {tok: {"used": False}}
        ref = f"https://{OWN_HOST}/slug1"
        assert door3a_check(tok, referer=ref, claims=claims, now_=now_) == "ok:slug1"
        assert door3a_check(tok, referer=ref, claims=claims, now_=now_) == "token_used"

    def test_direct_hit_no_referer_denied_not_burned(self):
        now_ = int(time.time())
        tok = make_token("slug1", "s", now_ + 90)
        claims = {tok: {"used": False}}
        assert door3a_check(tok, referer="", claims=claims, now_=now_) == "bad_referer"
        assert claims[tok]["used"] is False

    def test_foreign_referer_denied(self):
        now_ = int(time.time())
        tok = make_token("slug1", "s", now_ + 90)
        claims = {tok: {"used": False}}
        for ref in ("https://evil.example/", "http://" + OWN_HOST + "/x",
                    "https://" + OWN_HOST + ".evil.com/"):
            assert door3a_check(tok, referer=ref, claims=claims,
                                now_=now_) == "bad_referer"

    def test_unknown_token_denied(self):
        tok = make_token("slug1", "s", int(time.time()) + 90)
        assert door3a_check(tok, referer=f"https://{OWN_HOST}/x", claims={},
                            now_=int(time.time())) == "unknown_token"

    def test_check_order_referer_first(self):
        assert door3a_check("garbage", referer="", claims={},
                            now_=int(time.time())) == "bad_referer"


class TestGrantMirror:
    def _setup(self):
        now_ = int(time.time())
        slug = "grant1"
        return slug, make_grant(slug, now_ + 900), {slug: {"used": False}}, \
            ["vplink.in", "links.vplink.in"], "https://vplink.in/abc", now_

    def test_happy_path_burns_grant(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        assert door3b_check(slug, grant, referer=ref, ref_hosts=hosts,
                            grants=grants, session=None, ua="UA",
                            landing_host=OWN_HOST, now_=now_) == "ok"
        assert grants[slug]["used"] is True

    def test_grant_replay_denied(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        kw = dict(referer=ref, ref_hosts=hosts, grants=grants, session=None,
                  ua="UA", landing_host=OWN_HOST, now_=now_)
        assert door3b_check(slug, grant, **kw) == "ok"
        assert door3b_check(slug, grant, **kw) == "grant_used"

    def test_grant_check_order_slug_before_referer(self):
        assert door3b_check("x" * 33, "g", referer="", ref_hosts=[], grants={},
                            session=None, ua="", landing_host="", now_=0) == "bad_slug"
        assert door3b_check("nope", "g", referer="", ref_hosts=[], grants={},
                            session=None, ua="", landing_host="", now_=0) == "unknown_grant"
        assert door3b_check("s", "g", referer="", ref_hosts=[],
                            grants={"s": {"used": True}}, session=None, ua="",
                            landing_host="", now_=0) == "grant_used"

    def test_grant_wrong_referer_denied_not_burned(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        for bad_ref in ("", "https://evil.example/", "http://vplink.in/x",
                        "https://vplink.in.evil.com/"):
            assert door3b_check(slug, grant, referer=bad_ref, ref_hosts=hosts,
                                grants=grants, session=None, ua="UA",
                                landing_host=OWN_HOST, now_=now_) == "bad_referer"
        assert grants[slug]["used"] is False

    def test_grant_expired_denied(self):
        now_ = int(time.time())
        slug = "grant1"
        assert door3b_check(slug, make_grant(slug, now_ - 1),
                            referer="https://vplink.in/", ref_hosts=["vplink.in"],
                            grants={slug: {"used": False}}, session=None, ua="UA",
                            landing_host=OWN_HOST, now_=now_) == "expired"

    def test_ua_mismatch_flagged_when_session_present(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        assert door3b_check(slug, grant, referer=ref, ref_hosts=hosts,
                            grants=grants, session={"ua": "OriginalUA"},
                            ua="DifferentUA", landing_host=OWN_HOST,
                            now_=now_) == "ua_mismatch"

    def test_telegram_inapp_browser_variant(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        assert door3b_check(slug, grant, referer=ref, ref_hosts=hosts,
                            grants=grants, session={"ua": "UA"}, ua="UA",
                            landing_host="t.me", now_=now_) == "wrong_host"

    def test_cross_browser_flow_session_absent_still_ok(self):
        slug, grant, grants, hosts, ref, now_ = self._setup()
        assert door3b_check(slug, grant, referer=ref, ref_hosts=hosts,
                            grants=grants, session=None, ua="AnyUA",
                            landing_host=OWN_HOST, now_=now_) == "ok"


@pytest.fixture()
def fake_settings(monkeypatch):
    store: dict = {}

    async def fake_get_setting(key):
        return store.get(key)

    async def fake_set_setting(key, value):
        if value is None:
            store.pop(key, None)
        else:
            store[key] = value

    async def fake_get_json(key, default=None):
        return store.get(key, default)

    async def fake_set_json(key, value):
        store[key] = value

    async def fake_bool(key, default=False):
        v = store.get(key)
        return (str(v) == "1") if v is not None else default

    monkeypatch.setattr(repo, "get_setting", fake_get_setting)
    monkeypatch.setattr(repo, "set_setting", fake_set_setting)
    monkeypatch.setattr(repo, "get_setting_json", fake_get_json)
    monkeypatch.setattr(repo, "set_setting_json", fake_set_json)
    monkeypatch.setattr(repo, "get_setting_bool", fake_bool)
    return store


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    calls: list = []
    queue: list = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def request(self, method, url, json=None, params=None, headers=None):
        type(self).calls.append({"method": method, "url": url, "json": json,
                                 "params": params, "headers": headers})
        payload, status = type(self).queue.pop(0) if type(self).queue else ({}, 500)
        return _FakeResp(payload, status)

    def get(self, url, headers=None):
        type(self).calls.append({"method": "GET", "url": url})
        payload, status = type(self).queue.pop(0) if type(self).queue else ({}, 500)
        return _FakeResp(payload, status)


def _wire_worker(monkeypatch, responses):
    _FakeSession.calls, _FakeSession.queue = [], list(responses)
    monkeypatch.setattr(lg.aiohttp, "ClientSession", _FakeSession)


class TestClient:
    def test_mint_payload_and_headers(self, fake_settings, monkeypatch):
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16
        _wire_worker(monkeypatch, [({"ok": True, "slug": "abc",
                                    "url": "https://w.example.workers.dev/abc"}, 200)])
        r = run(lg.mint("https://vplink.in/xyz", grant_slug="g1"))
        assert r["slug"] == "abc"
        call = _FakeSession.calls[0]
        assert call["method"] == "POST" and call["url"].endswith("/api/admin/mint")
        assert call["headers"]["x-admin-key"] == "k" * 16
        assert call["json"]["grant_slug"] == "g1"
        assert call["json"]["ttl_hours"] == 168

    def test_mint2_payload(self, fake_settings, monkeypatch):
        fake_settings["linkguard_base"] = "https://w.example.workers.dev/"
        fake_settings["linkguard_key"] = "k" * 16
        _wire_worker(monkeypatch, [({"ok": True, "slug": "g1",
                                    "finish2_url": "https://w/finish2?s=g1"}, 200)])
        r = run(lg.mint_return_grant("https://t.me/bot?start=verify_T",
                                     ref_hosts=["vplink.in"]))
        assert r["finish2_url"].endswith("/finish2?s=g1")
        assert _FakeSession.calls[0]["url"].endswith("/api/admin/mint2")

    def test_fail_open_on_500(self, fake_settings, monkeypatch):
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16
        _wire_worker(monkeypatch, [({"error": "boom"}, 500)])
        assert run(lg.mint("https://vplink.in/xyz")) is None

    def test_fail_open_on_exception(self, fake_settings, monkeypatch):
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16

        class _Boom:
            def __init__(self, *a, **k):
                raise OSError("dns exploded")

        monkeypatch.setattr(lg.aiohttp, "ClientSession", _Boom)
        assert run(lg.mint("https://vplink.in/xyz")) is None
        assert run(lg.mint_return_grant("https://t.me/bot?start=verify_T")) is None

    def test_fail_open_when_unconfigured(self, fake_settings):
        assert run(lg.mint("https://vplink.in/xyz")) is None
        assert run(lg.health()) is None

    def test_worker_host_extraction(self):
        assert lg.worker_host_from_url("https://vplink.in/api?api=1&url=x") == "vplink.in"
        assert lg.worker_host_from_url("https://links.gplinks.co/x") == "links.gplinks.co"
        assert lg.worker_host_from_url("not a url") == ""


class _FakeBot:
    def __init__(self):
        self._me = types.SimpleNamespace(username="TestBot")
        self.sent = []

    async def get_me(self):
        return self._me

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return types.SimpleNamespace(message_id=1)


async def _tok(u, c):
    return "TOK123"


async def _gate_text():
    return "gate"


async def _btns():
    return []


async def _six():
    return 6.0


async def _noop(*a, **k):
    return None


async def _false():
    return False


class TestSendGateIntegration:
    def _prime(self, fake_settings, monkeypatch):
        fake_settings["shortener_enabled"] = "1"
        fake_settings["shortener_api"] = "https://vplink.in/api?api=KEY&url="
        monkeypatch.setattr(sh, "new_token", _tok)
        monkeypatch.setattr(sh, "has_tokens", lambda u: _false())
        monkeypatch.setattr(sh, "get_gate_text", _gate_text)
        monkeypatch.setattr(sh, "get_secondary_buttons", _btns)
        monkeypatch.setattr(sh, "get_ttl_hours", _six)
        monkeypatch.setattr(sh, "delete_gate_later", _noop)

    def test_full_chain_button_is_public_slug(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch)
        fake_settings["linkguard_enabled"] = "1"
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16
        shortened = {}

        async def fake_make_short(api_base, url):  # v5.1: provider-aware
            shortened["url"] = url
            return "https://vplink.in/SHORT"

        monkeypatch.setattr(sh, "make_short_url_for", fake_make_short)
        _wire_worker(monkeypatch, [
            ({"ok": True, "slug": "grantXYZ",
              "finish2_url": "https://w.example.workers.dev/finish2?s=grantXYZ"}, 200),
            ({"ok": True, "slug": "pub123",
              "url": "https://w.example.workers.dev/pub123"}, 200)])
        bot = _FakeBot()
        assert run(sh.send_gate(bot, 42, "abc")) is True
        assert shortened["url"].startswith(
            "https://w.example.workers.dev/finish2?s=grantXYZ")
        btn = bot.sent[0]["markup"].inline_keyboard[0][0]
        assert btn.text == "🔓 Verify & Unlock"
        assert btn.url == "https://w.example.workers.dev/pub123"
        assert "vplink.in" in _FakeSession.calls[0]["json"]["ref_hosts"]
        assert "verify_" in _FakeSession.calls[0]["json"]["url"]

    def test_fallback_when_linkguard_off(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch)

        async def fake_make_short(api_base, url):  # v5.1: provider-aware
            assert url.startswith("https://t.me/TestBot?start=verify_")
            return "https://vplink.in/SHORT"

        monkeypatch.setattr(sh, "make_short_url_for", fake_make_short)
        _wire_worker(monkeypatch, [])
        bot = _FakeBot()
        assert run(sh.send_gate(bot, 42, "abc")) is True
        assert bot.sent[0]["markup"].inline_keyboard[0][0].url == "https://vplink.in/SHORT"

    def test_fallback_when_worker_down(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch)
        fake_settings["linkguard_enabled"] = "1"
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16

        async def fake_make_short(api_base, url):  # v5.1: provider-aware
            assert "?start=verify_" in url
            return "https://vplink.in/SHORT"

        monkeypatch.setattr(sh, "make_short_url_for", fake_make_short)
        _wire_worker(monkeypatch, [({}, 500), ({}, 500), ({}, 500)])
        bot = _FakeBot()
        assert run(sh.send_gate(bot, 42, "abc")) is True
        assert bot.sent[0]["markup"].inline_keyboard[0][0].url == "https://vplink.in/SHORT"

    def test_gate_disabled_still_fail_open(self, fake_settings, monkeypatch):
        fake_settings["shortener_enabled"] = None
        assert run(sh.send_gate(_FakeBot(), 42, "abc")) is False


def test_slug_regex_bounds():
    pat = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
    assert pat.match("abcDEF123_-")
    assert not pat.match("x" * 33)
    assert not pat.match("bad slug!")
    assert not pat.match("")


def test_deep_link_not_leaked_in_public_slug_url():
    public = f"https://w.example.workers.dev/{_b64url(b'randomslug')[:12]}"
    assert "verify_" not in public and "t.me" not in public
