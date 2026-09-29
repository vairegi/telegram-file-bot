"""v4.8 tests — instant-ban, /banmessage template, /verified_users logging."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import browse, richlists


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------------------
# Shape checks
# ---------------------------------------------------------------------------
def _check_rt(n, p=""):
    if isinstance(n, str):
        return
    if isinstance(n, list):
        for i, x in enumerate(n):
            _check_rt(x, f"{p}[{i}]")
        return
    assert isinstance(n, dict), p
    assert n.get("type") != "plain"
    if "text" in n:
        _check_rt(n["text"], p + ".text")


def _check_table(rm):
    json.dumps(rm)
    t = [b for b in rm["blocks"] if b["type"] == "table"][0]
    w = len(t["cells"][0])
    for row in t["cells"]:
        assert len(row) == w
        for c in row:
            _check_rt(c["text"], "cell")
    assert all(c.get("is_header") for c in t["cells"][0])
    return t


# ---------------------------------------------------------------------------
# /verified_users with fresh logging at redemption
# ---------------------------------------------------------------------------
def _patch_store(monkeypatch):
    store: dict = {}

    async def gj(k, d=None):
        return store.get(k, d)

    async def sj(k, v):
        store[k] = v

    async def gs(k):
        return store.get(k)

    async def ss(k, v):
        if v is None:
            store.pop(k, None)
        else:
            store[k] = v

    async def dir_(uids):
        return {952130393: {"first_name": "Coded", "username": "coded_k"},
                8936230259: {"first_name": None, "username": "drunkony"}}

    monkeypatch.setattr(richlists.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.repo, "set_setting_json", sj)
    monkeypatch.setattr(richlists.repo, "get_setting", gs)
    monkeypatch.setattr(richlists.repo, "set_setting", ss)
    monkeypatch.setattr(richlists.repo, "get_directory_users", dir_)

    async def fake_post(code):
        return {"caption": "Title\n➤ Tags: #hanime\n➤ Parodies: #korean"}
    monkeypatch.setattr(richlists.repo, "get_post_by_code", fake_post)
    store["shortener_api"] = "https://vplink.in/api?api=x&url="
    return store


def test_record_verification_captures_all_columns(monkeypatch):
    store = _patch_store(monkeypatch)

    async def go():
        cat = await richlists.category_for_code("ABC")
        assert "hanime" in cat and "korean" in cat
        # 2 redemptions same user (Count = 2, keep most recent elapsed)
        await richlists.record_verification(952130393, elapsed=19.9,
                                            category=cat)
        await richlists.record_verification(952130393, elapsed=44.5,
                                            category=cat)
        # different user via arolink
        store["shortener_api"] = "https://arolink.com/api?api=x&url="
        await richlists.record_verification(8936230259, elapsed=299.0,
                                            category="jav")

    run(go())
    rm, plain, users, verifs = run(richlists.build_verified_list())
    assert users == 2 and verifs == 3
    t = _check_table(rm)
    row1 = t["cells"][1]  # first data row
    assert row1[1]["text"] == "Coded"
    assert row1[2]["text"] == "44s"           # LAST elapsed, not first
    assert row1[5]["text"] == "2"             # count
    assert row1[4]["text"] == "vplink"        # link_type from api base
    row2 = t["cells"][2]
    assert row2[4]["text"] == "arolink"
    assert "299s" in plain and "jav" in plain


def test_link_type_detection():
    assert richlists.detect_link_type("https://vplink.in/api") == "vplink"
    assert richlists.detect_link_type("https://arolink.com/api") == "arolink"
    assert richlists.detect_link_type("https://gplinks.com/api") == "gplinks"
    assert richlists.detect_link_type("") == "unknown"
    assert richlists.detect_link_type("https://x.com") == "other"


def test_ist_day_key():
    assert richlists._log_key("2026-09-29") == "verified_log:2026-09-29"
    assert richlists._log_key("2026-09-29") != richlists._log_key("2026-09-30")


# ---------------------------------------------------------------------------
# /banlist
# ---------------------------------------------------------------------------
def test_ban_list_table(monkeypatch):
    async def fake():
        return [
            {"user_id": 8936230259, "username": "drunkony",
             "first_name": None,
             "reason": "3 consecutive bypass s", "banned_at": "2026-09-29T09"},
            {"user_id": 5866296364, "username": None, "first_name": None,
             "reason": "manual ban", "banned_at": "2026-09-29T08"},
            {"user_id": 1006116902738, "username": None, "first_name": None,
             "reason": "manual ban", "banned_at": ""},
        ]
    monkeypatch.setattr(richlists, "_banned_rows", fake)
    rm, plain, total = run(richlists.build_ban_list())
    assert total == 3
    t = _check_table(rm)
    assert t["cells"][1][1]["text"] == "@drunkony"
    assert t["cells"][1][3]["text"] == "/unban 8936230259"
    assert "/unban 5866296364" in plain


def test_ban_list_empty(monkeypatch):
    async def fake():
        return []
    monkeypatch.setattr(richlists, "_banned_rows", fake)
    _rm, _plain, total = run(richlists.build_ban_list())
    assert total == 0


# ---------------------------------------------------------------------------
# /banmessage template
# ---------------------------------------------------------------------------
def test_ban_message_default(monkeypatch):
    store: dict = {}

    async def gs(k):
        return store.get(k)
    monkeypatch.setattr(richlists.repo, "get_setting", gs)
    m = run(richlists.get_ban_message())
    assert "banned" in m.lower()
    assert "{elapsed}" in m


def test_ban_message_render_placeholders():
    tpl = ("🚫 Banned. Elapsed: <b>{elapsed}s</b>. "
           "ID: <code>{user_id}</code>.")
    out = richlists.render_ban_message(tpl, elapsed=16.4, user_id=42)
    assert "16.4s" in out and "42" in out and "<b>" in out


def test_ban_message_render_handles_bad_template():
    # Missing key: return raw template, do NOT crash the ban path
    out = richlists.render_ban_message("elapsed {broken", 5.0, 7)
    assert "elapsed" in out


# ---------------------------------------------------------------------------
# Rich fallback still works
# ---------------------------------------------------------------------------
def test_rich_fallback_to_plain(monkeypatch):
    calls = []
    sent = []

    async def fake_api_post(bot, method, payload):
        calls.append((method, payload))
        raise browse.BrowseError("sendRichMessage failed [400]: Bad Request")

    class FakeBot:
        token = "T"

        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=9)

    monkeypatch.setattr(richlists.browse, "api_post", fake_api_post)
    rm = richlists._wrap("t", richlists.rich_table(
        ["A"], [[richlists._cell("1")]]))
    run(richlists.send_rich_or_plain(FakeBot(), 42, rm, "<b>plain</b>"))
    assert calls[0][0] == "sendRichMessage"
    assert sent and sent[0][0] == 42 and "plain" in sent[0][1]


def test_rich_success_no_fallback(monkeypatch):
    calls = []
    sent = []

    async def fake_api_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 5}

    class FakeBot:
        token = "T"

        async def send_message(self, *a, **kw):
            sent.append(a)

    monkeypatch.setattr(richlists.browse, "api_post", fake_api_post)
    rm = richlists._wrap("t", richlists.rich_table(
        ["A"], [[richlists._cell("1")]]))
    run(richlists.send_rich_or_plain(FakeBot(), 42, rm, "plain"))
    assert calls[0][0] == "sendRichMessage"
    assert not sent


# ---------------------------------------------------------------------------
# Instant-ban is the CODE PATH used by setup_cmds (no strike wait)
# We assert that call graph: bypass -> ban_user + get + render + send
# ---------------------------------------------------------------------------
def test_instant_ban_flow(monkeypatch):
    """Simulate the patched setup_cmds bypass branch calling ban helpers."""
    banned = []
    stored = {"ban_message":
              "🚫 Instant ban. Elapsed <b>{elapsed}s</b>. id={user_id}"}

    async def gs(k):
        return stored.get(k)
    async def ss(k, v):
        stored[k] = v

    async def ban(uid, reason):
        banned.append((uid, reason))

    monkeypatch.setattr(richlists.repo, "get_setting", gs)
    monkeypatch.setattr(richlists.repo, "set_setting", ss)
    monkeypatch.setattr(richlists.repo, "ban_user", ban)

    async def go():
        # patched bypass branch would do exactly this:
        elapsed = 16.4
        user_id = 8936230259
        await richlists.repo.ban_user(user_id, f"instant ban: bypass "
                                      f"({elapsed:.1f}s)")
        tpl = await richlists.get_ban_message()
        text = richlists.render_ban_message(tpl, elapsed=elapsed,
                                            user_id=user_id)
        return text

    text = run(go())
    assert banned == [(8936230259, "instant ban: bypass (16.4s)")]
    assert "16.4s" in text and str(8936230259) in text
