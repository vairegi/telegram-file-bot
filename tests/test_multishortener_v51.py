"""v5.1: multi-shortener rotation tests.

  * Provider registry: vplink (index 0) + extras from settings JSON.
  * Per-user rotation pointer advances AFTER each successful solve
    (_mark_verified) and wraps around; 4 AM wallet expiry does NOT reset it.
  * send_gate uses the provider AT the user's pointer (fail-open everywhere).
  * all_provider_hosts() feeds the LinkGuard finish2 referer allowlist.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

sys.path.insert(0, "/home/user/ms/telegram-file-bot")

from app.services import linkguard as lg          # noqa: E402
from app.services import repo                     # noqa: E402
from app.services import shortener as sh          # noqa: E402
from app.services import tokens as _tokens        # noqa: E402
from app.services import posting                  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


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


VPLINK_API = "https://vplink.in/api?api=KEY&url="
AROLINKS_API = "https://arolinks.com/api?api=K2&url="
GPLINKS_API = "https://gplinks.co/api?api=K3&url="


def _two_providers(store):
    store["shortener_api"] = VPLINK_API
    store["shortener_providers"] = [
        {"name": "arolinks", "api": AROLINKS_API,
         "hosts": ["links.arolinks.com"]},
    ]


class TestProviderRegistry:
    def test_default_single_provider(self, fake_settings):
        fake_settings["shortener_api"] = VPLINK_API
        providers = run(sh.get_providers())
        assert len(providers) == 1
        assert providers[0]["name"] == "vplink"
        assert providers[0]["api"] == VPLINK_API

    def test_extra_providers_parsed(self, fake_settings):
        _two_providers(fake_settings)
        fake_settings["shortener_providers"].append(
            {"name": "gplinks", "api": GPLINKS_API, "hosts": []})
        providers = run(sh.get_providers())
        assert [p["name"] for p in providers] == ["vplink", "arolinks", "gplinks"]
        assert providers[1]["hosts"] == ["links.arolinks.com"]

    def test_malformed_extra_skipped(self, fake_settings):
        _two_providers(fake_settings)
        fake_settings["shortener_providers"].append({"name": "broken"})
        fake_settings["shortener_providers"].append({"api": GPLINKS_API})
        names = [p["name"] for p in run(sh.get_providers())]
        assert "broken" not in names and len(names) == 2

    def test_all_provider_hosts_merges_everything(self, fake_settings):
        _two_providers(fake_settings)
        hosts = run(sh.all_provider_hosts())
        assert hosts == ["vplink.in", "arolinks.com", "links.arolinks.com"]


async def _noop_grant(*a, **k):
    return {"tokens": 11, "expiry": 0.0}


class TestRotation:
    def test_pointer_defaults_to_zero(self, fake_settings):
        assert run(sh.current_provider_index(42)) == 0

    def test_mark_verified_advances_pointer(self, fake_settings, monkeypatch):
        monkeypatch.setattr(_tokens, "grant", _noop_grant)
        _two_providers(fake_settings)
        assert run(sh.current_provider_index(42)) == 0
        run(sh._mark_verified(42))
        assert run(sh.current_provider_index(42)) == 1
        run(sh._mark_verified(42))
        assert run(sh.current_provider_index(42)) == 0   # wrapped

    def test_pointer_beyond_list_wraps(self, fake_settings):
        _two_providers(fake_settings)
        run(repo.set_setting_json("shortener_rotation", {"42": 7}))
        assert run(sh.current_provider_index(42)) == 1    # 7 % 2

    def test_advance_failure_is_fail_open(self, fake_settings, monkeypatch):
        monkeypatch.setattr(_tokens, "grant", _noop_grant)

        async def boom(key, value):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(repo, "set_setting_json", boom)
        run(sh._mark_verified(42))                    # must NOT raise

    def test_wallet_expiry_does_not_reset_pointer(self, fake_settings, monkeypatch):
        monkeypatch.setattr(_tokens, "grant", _noop_grant)
        _two_providers(fake_settings)
        run(sh._mark_verified(42))
        # (tokens quietly expire at 4 AM here — no code runs)
        assert run(sh.current_provider_index(42)) == 1


class _FakeBot:
    def __init__(self):
        self._me = types.SimpleNamespace(username="TestBot")
        self.sent = []

    async def get_me(self):
        return self._me

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return types.SimpleNamespace(message_id=1)


async def _false(*a, **k):
    return False


async def _tok(u, c):
    return "TOK123"


async def _gate_text():
    return "gate"


async def _btns():
    return []


async def _uname(bot):
    return "TestBot"


async def _noop(*a, **k):
    return None


class TestGateProviderSelection:
    def _prime(self, fake_settings, monkeypatch, pointer):
        _two_providers(fake_settings)
        fake_settings["shortener_enabled"] = "1"
        run(repo.set_setting_json("shortener_rotation", {"42": pointer}))
        monkeypatch.setattr(sh, "is_verified", _false)
        monkeypatch.setattr(sh, "new_token", _tok)
        monkeypatch.setattr(sh, "get_gate_text", _gate_text)
        monkeypatch.setattr(sh, "get_secondary_buttons", _btns)
        monkeypatch.setattr(sh, "delete_gate_later", _noop)
        monkeypatch.setattr(posting, "get_bot_username", _uname)

    def test_first_solve_uses_vplink(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch, pointer=0)
        seen = {}

        async def fake_short(api_base, url):
            seen["base"] = api_base
            return "https://short.test/x"

        monkeypatch.setattr(sh, "make_short_url_for", fake_short)
        assert run(sh.send_gate(_FakeBot(), 42, "abc")) is True
        assert seen["base"] == VPLINK_API

    def test_second_solve_uses_arolinks(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch, pointer=1)
        seen = {}

        async def fake_short(api_base, url):
            seen["base"] = api_base
            return "https://short.test/x"

        monkeypatch.setattr(sh, "make_short_url_for", fake_short)
        bot = _FakeBot()
        assert run(sh.send_gate(bot, 42, "abc")) is True
        assert seen["base"] == AROLINKS_API
        assert bot.sent[0]["markup"].inline_keyboard[0][0].url == \
            "https://short.test/x"

    def test_no_provider_api_fails_open(self, fake_settings, monkeypatch):
        fake_settings["shortener_enabled"] = "1"
        monkeypatch.setattr(sh, "is_verified", _false)
        assert run(sh.send_gate(_FakeBot(), 42, "abc")) is False

    def test_linkguard_refhosts_cover_all_providers(self, fake_settings, monkeypatch):
        self._prime(fake_settings, monkeypatch, pointer=1)
        fake_settings["linkguard_enabled"] = "1"
        fake_settings["linkguard_base"] = "https://w.example.workers.dev"
        fake_settings["linkguard_key"] = "k" * 16
        calls = []

        async def fake_mint2(url, ref_hosts=None, ttl_hours=168):
            calls.append({"url": url, "ref_hosts": ref_hosts})
            return None                              # abort chain -> fail-open

        monkeypatch.setattr(lg, "mint_return_grant", fake_mint2)

        async def fake_short(api_base, url):
            return "https://short.test/x"

        monkeypatch.setattr(sh, "make_short_url_for", fake_short)
        assert run(sh.send_gate(_FakeBot(), 42, "abc")) is True
        hosts = calls[0]["ref_hosts"]
        assert "vplink.in" in hosts
        assert "arolinks.com" in hosts
        assert "links.arolinks.com" in hosts
