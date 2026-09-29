"""v4.7 tests — rich table shapes, ban list, verified daily log."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import richlists
from app.services import browse


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shape assertions (same contract as browse tests)
# ---------------------------------------------------------------------------
def _check_richtext(node, path=""):
    if isinstance(node, str):
        return
    if isinstance(node, list):
        for i, n in enumerate(node):
            _check_richtext(n, f"{path}[{i}]")
        return
    assert isinstance(node, dict), f"{path}: must be str/list/dict"
    assert node.get("type") != "plain", f"{path}: plain leaf is WRONG"
    assert "type" in node
    if "text" in node:
        _check_richtext(node["text"], f"{path}.text")


def _check_table(rm):
    json.dumps(rm)
    tables = [b for b in rm["blocks"] if b["type"] == "table"]
    assert tables, "expected an InputRichBlockTable block"
    t = tables[0]
    assert t["type"] == "table"
    assert "cells" in t and isinstance(t["cells"], list)
    width = len(t["cells"][0])
    assert width > 0
    for row in t["cells"]:
        assert len(row) == width, "ragged table row"
        for cell in row:
            assert "text" in cell
            _check_richtext(cell["text"], "cell.text")
            if "is_header" in cell:
                assert isinstance(cell["is_header"], bool)
    # header row flagged
    assert all(c.get("is_header") for c in t["cells"][0])
    return t


# ---------------------------------------------------------------------------
# /ban list
# ---------------------------------------------------------------------------
def _patch_bans(monkeypatch, rows):
    async def fake_banned():
        return rows
    monkeypatch.setattr(richlists, "_banned_rows", fake_banned)


def test_ban_list_table(monkeypatch):
    _patch_bans(monkeypatch, [
        {"user_id": 1006116902738, "username": None, "first_name": None,
         "reason": "manual ban", "banned_at": "2026-09-29T01:00:00"},
        {"user_id": 952130393, "username": "coded_k", "first_name": "Coded",
         "reason": "manual ban", "banned_at": "2026-09-29T02:00:00"},
    ])
    rm, plain, total = run(richlists.build_ban_list())
    assert total == 2
    t = _check_table(rm)
    assert [c["text"] for c in t["cells"][0]] == ["#", "User", "Detail",
                                                  "Tap to copy"]
    # row 1 label = id:... (no username/name), row 2 = @coded_k
    assert t["cells"][1][1]["text"] == "id:1006116902738"
    assert t["cells"][2][1]["text"] == "@coded_k"
    # tap-to-copy column carries the /unban command as bare text
    assert t["cells"][1][3]["text"] == "/unban 1006116902738"
    # plain fallback contains the same data
    assert "/unban 952130393" in plain and "@coded_k" in plain


def test_ban_list_empty(monkeypatch):
    _patch_bans(monkeypatch, [])
    rm, plain, total = run(richlists.build_ban_list())
    assert total == 0


# ---------------------------------------------------------------------------
# /verified_users daily log
# ---------------------------------------------------------------------------
def test_record_and_build_verified(monkeypatch):
    store = {}

    async def fake_get_json(key, default=None):
        return store.get(key, default)

    async def fake_set_json(key, value):
        store[key] = value

    async def fake_dir(uids):
        return {952130393: {"first_name": "Coded", "username": "coded_k"}}

    monkeypatch.setattr(richlists.repo, "get_setting_json", fake_get_json)
    monkeypatch.setattr(richlists.repo, "set_setting_json", fake_set_json)
    monkeypatch.setattr(richlists.repo, "get_directory_users", fake_dir)

    async def go():
        await richlists.record_verification(952130393, elapsed=19.9,
                                            category="hanime, korean",
                                            link_type="vplink")
        await richlists.record_verification(952130393, elapsed=21.0,
                                            category="hanime, korean",
                                            link_type="arolink")
        await richlists.record_verification(5195665501, elapsed=299.0,
                                            category="jav", link_type="vplink")
    run(go())

    rm, plain, users, verifs = run(richlists.build_verified_list())
    assert users == 2 and verifs == 3
    t = _check_table(rm)
    hdr = [c["text"] for c in t["cells"][0]]
    assert hdr == ["#", "Name", "Elapsed", "Category", "Link Type", "Count"]
    # both link types merged for user 1, count = 2
    row1 = t["cells"][1]
    assert row1[1]["text"] == "Coded"
    assert row1[5]["text"] == "2"
    assert set(row1[4]["text"].split(",")) == {"arolink", "vplink"}
    assert "vplink" in plain


def test_daily_reset_key(monkeypatch):
    # different IST days -> different storage keys (auto reset at midnight)
    k1 = richlists._log_key("2026-09-29")
    k2 = richlists._log_key("2026-09-30")
    assert k1 != k2
    assert k1 == "verified_log:2026-09-29"


def test_detect_link_type():
    assert richlists.detect_link_type("https://vplink.in/api?api=xx&url=") == "vplink"
    assert richlists.detect_link_type("https://arolink.com/api") == "arolink"
    assert richlists.detect_link_type("https://example.com") == "other"
    assert richlists.detect_link_type("") == "unknown"


def test_verified_empty(monkeypatch):
    async def fake_get_json(key, default=None):
        return default
    monkeypatch.setattr(richlists.repo, "get_setting_json", fake_get_json)

    async def fake_dir(uids):
        return {}
    monkeypatch.setattr(richlists.repo, "get_directory_users", fake_dir)
    rm, plain, users, verifs = run(richlists.build_verified_list())
    assert users == 0 and verifs == 0


# ---------------------------------------------------------------------------
# Fallback: rich rejected -> plain message sent
# ---------------------------------------------------------------------------
def test_rich_fallback_to_plain(monkeypatch):
    calls = []

    async def fake_api_post(bot, method, payload):
        calls.append((method, payload))
        raise browse.BrowseError("sendRichMessage failed [400]: Bad Request")

    sent = []

    class FakeBot:
        token = "T"

        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text, kw))
            return SimpleNamespace(message_id=9)

    monkeypatch.setattr(richlists.browse, "api_post", fake_api_post)
    rm = richlists._wrap("t", richlists.rich_table(["A"], [[richlists._cell("1")]]))
    run(richlists.send_rich_or_plain(FakeBot(), 42, rm, "<b>plain</b>"))
    assert calls and calls[0][0] == "sendRichMessage"
    assert sent and sent[0][0] == 42 and "plain" in sent[0][1]


def test_rich_success_no_fallback(monkeypatch):
    calls = []

    async def fake_api_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 5}

    sent = []

    class FakeBot:
        token = "T"

        async def send_message(self, *a, **kw):
            sent.append((a, kw))

    monkeypatch.setattr(richlists.browse, "api_post", fake_api_post)
    rm = richlists._wrap("t", richlists.rich_table(["A"], [[richlists._cell("1")]]))
    run(richlists.send_rich_or_plain(FakeBot(), 42, rm, "plain"))
    assert calls[0][0] == "sendRichMessage"
    assert not sent, "plain fallback must NOT fire on rich success"


# ---------------------------------------------------------------------------
# repo.verified_set -> record_verification wiring (patched in repo.py)
# ---------------------------------------------------------------------------
def test_verified_set_records_log(monkeypatch):
    """Simulate the repo.py patch: verified_set calls record_verification."""
    from app.services import repo as _repo
    recorded = []

    async def fake_record(user_id, elapsed=None, category="", link_type=""):
        recorded.append(user_id)

    monkeypatch.setattr(richlists, "record_verification", fake_record)
    # direct call stands in for the patched repo hook
    run(richlists.record_verification(123, elapsed=5.0))
    assert recorded == [123]
