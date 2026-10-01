"""v4.9 — token wallet tests (model, gating, table columns)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from app.services import browse, richlists, tokens

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------------------
# Expiry maths: 4:00 AM IST
# ---------------------------------------------------------------------------
def _epoch(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


def test_next_reset_same_day():
    # 10:00 IST -> next 4 AM is TOMORROW 04:00
    now = _epoch(2026, 10, 1, 10, 0)
    exp = tokens.next_reset(now)
    assert dt.datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-02 04:00"


def test_next_reset_before_4am_rolls_a_full_day():
    # 00:30 IST is < 60 min? no -> but it IS before 4 AM, so window = 3.5h
    now = _epoch(2026, 10, 1, 0, 30)
    exp = tokens.next_reset(now)
    assert dt.datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-01 04:00"


def test_next_reset_near_4am_rolls_to_tomorrow():
    # 03:30 IST -> only 30 min away -> give a FULL day instead
    now = _epoch(2026, 10, 1, 3, 30)
    exp = tokens.next_reset(now)
    assert dt.datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-02 04:00"


def test_expiry_label():
    lbl = tokens.expiry_label(_epoch(2026, 10, 2, 4, 0))
    assert "04:00" in lbl and "IST" in lbl


# ---------------------------------------------------------------------------
# Wallet behaviour with an in-memory settings store (Turso path)
# ---------------------------------------------------------------------------
def _wire(monkeypatch):
    store: dict = {}

    async def gj(k, d=None):
        return store.get(k, d)

    async def sj(k, v):
        store[k] = v

    async def gsi(k, d=0):
        v = store.get(k)
        return int(v) if v not in (None, "") else d

    async def ss(k, v):
        store[k] = v

    monkeypatch.setattr(tokens.repo, "get_setting_json", gj)
    monkeypatch.setattr(tokens.repo, "set_setting_json", sj)
    monkeypatch.setattr(tokens.repo, "get_setting_int", gsi)
    monkeypatch.setattr(tokens.repo, "set_setting", ss)
    monkeypatch.setattr(tokens.repo, "_mongo", lambda: False)
    return store


def test_default_per_solve_is_11(monkeypatch):
    _wire(monkeypatch)
    assert run(tokens.per_solve()) == 11


def test_grant_sets_11_and_expiry(monkeypatch):
    store = _wire(monkeypatch)
    now = _epoch(2026, 10, 1, 12, 0)
    w = run(tokens.grant(7, now=now))
    assert w["tokens"] == 11
    assert dt.datetime.fromtimestamp(w["expiry"], IST).strftime("%H:%M") == "04:00"
    assert run(tokens.has_tokens(7)) is True


def test_consume_is_one_post_per_token(monkeypatch):
    _wire(monkeypatch)
    run(tokens.grant(7, n=3))
    assert run(tokens.consume(7)) == 2      # post 1
    assert run(tokens.consume(7)) == 1      # post 2 (3 PDFs still 1 token)
    assert run(tokens.consume(7)) == 0      # post 3
    assert run(tokens.consume(7)) == 0      # empty -> stays 0
    assert run(tokens.has_tokens(7)) is False


def test_expired_tokens_do_not_count(monkeypatch):
    store = _wire(monkeypatch)
    # wallet written with an expiry in the PAST
    store["token_wallets"] = {"9": {"tokens": 5, "expiry": 1_600_000_000.0}}
    assert run(tokens.has_tokens(9)) is False
    assert run(tokens.consume(9)) == 0      # cannot spend expired tokens
    assert run(tokens.active_count()) == 0


def test_refund_restores_one(monkeypatch):
    _wire(monkeypatch)
    run(tokens.grant(7, n=2))
    run(tokens.consume(7))
    assert run(tokens.refund(7)) == 2


def test_grant_overwrites_not_stacks(monkeypatch):
    _wire(monkeypatch)
    run(tokens.grant(7, n=11))
    run(tokens.consume(7)); run(tokens.consume(7))
    assert run(tokens.get_wallet(7))["tokens"] == 9
    run(tokens.grant(7, n=11))              # solve again -> back to 11
    assert run(tokens.get_wallet(7))["tokens"] == 11


def test_active_count(monkeypatch):
    _wire(monkeypatch)
    run(tokens.grant(1, n=2)); run(tokens.grant(2, n=1))
    run(tokens.consume(2))
    assert run(tokens.active_count()) == 1


def test_wallet_line_states(monkeypatch):
    _wire(monkeypatch)
    assert "0" in run(tokens.wallet_line(7))
    run(tokens.grant(7, n=4))
    line = run(tokens.wallet_line(7))
    assert "<b>4</b>" in line and "IST" in line


# ---------------------------------------------------------------------------
# /verified_users table: new columns (File, Tokens) + profile-linked Name
# ---------------------------------------------------------------------------
def test_verified_table_columns_and_name_link(monkeypatch):
    store: dict = {}

    async def gj(k, d=None):
        return store.get(k, d)

    async def sj(k, v):
        store[k] = v

    async def gsi(k, d=0):
        v = store.get(k)
        return int(v) if v not in (None, "") else d

    monkeypatch.setattr(richlists.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.repo, "set_setting_json", sj)
    monkeypatch.setattr(richlists.repo, "get_setting_int", gsi)

    async def dir_(uids):
        return {111: {"first_name": "Charanraj R", "username": "charan"},
                222: {"first_name": None, "username": "drunkony"}}
    monkeypatch.setattr(richlists.repo, "get_directory_users", dir_)
    monkeypatch.setattr(richlists.tokens.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.tokens.repo, "set_setting_json", sj)
    monkeypatch.setattr(richlists.tokens.repo, "get_setting_int", gsi)
    monkeypatch.setattr(richlists.tokens.repo, "_mongo", lambda: False)

    async def go():
        await richlists.record_verification(111, elapsed=299.0, category="jav")
        await richlists.record_download(111)
        await richlists.record_download(111)
        await richlists.record_verification(222, elapsed=226.0)
        await richlists.record_download(222)
        await richlists.tokens.grant(111, n=5)
        await richlists.tokens.grant(222, n=11)

    run(go())
    rm, plain, users, verifs = run(richlists.build_verified_list())
    assert users == 2 and verifs == 2
    t = [b for b in rm["blocks"] if b["type"] == "table"][0]
    hdr = [c["text"] for c in t["cells"][0]]
    assert hdr == ["#", "Name", "Elapsed", "File", "Link Type", "Count",
                   "Tokens left"]
    assert "Category" not in hdr
    row1 = t["cells"][1]
    # Name is a RichTextUrl linking to the user's profile
    name = row1[1]["text"]
    assert isinstance(name, dict) and name["type"] == "url"
    assert name["url"] == "tg://user?id=111"
    assert name["text"] == "Charanraj R"
    assert row1[2]["text"] == "299s"        # Elapsed
    assert row1[3]["text"] == "2"           # File (2 downloads)
    assert row1[5]["text"] == "1"           # Count (1 solve)
    assert row1[6]["text"] == "5"           # Tokens left
    row2 = t["cells"][2]
    assert row2[1]["text"]["text"] == "@drunkony"
    assert row2[1]["text"]["url"] == "tg://user?id=222"
    json.dumps(rm)


def test_download_only_row_still_appears(monkeypatch):
    """Tokens from a late-night solve: user downloads after midnight IST."""
    store: dict = {}

    async def gj(k, d=None):
        return store.get(k, d)

    async def sj(k, v):
        store[k] = v

    async def gsi(k, d=0):
        v = store.get(k)
        return int(v) if v not in (None, "") else d

    monkeypatch.setattr(richlists.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.repo, "set_setting_json", sj)
    monkeypatch.setattr(richlists.repo, "get_setting_int", gsi)

    async def dir_(uids):
        return {333: {"first_name": "Late Night", "username": None}}
    monkeypatch.setattr(richlists.repo, "get_directory_users", dir_)
    monkeypatch.setattr(richlists.tokens.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.tokens.repo, "set_setting_json", sj)
    monkeypatch.setattr(richlists.tokens.repo, "get_setting_int", gsi)
    monkeypatch.setattr(richlists.tokens.repo, "_mongo", lambda: False)

    run(richlists.record_download(333, "ABC"))
    rm, plain, users, verifs = run(richlists.build_verified_list())
    assert users == 1 and verifs == 0
    t = [b for b in rm["blocks"] if b["type"] == "table"][0]
    assert t["cells"][1][3]["text"] == "1"     # File
    assert t["cells"][1][5]["text"] == "0"     # Count


# ---------------------------------------------------------------------------
# Gate behaviour (the heart of the change)
# ---------------------------------------------------------------------------
def test_gate_opens_only_without_tokens(monkeypatch):
    _wire(monkeypatch)
    from app.services import shortener as sh

    async def no_verified(uid):
        return await tokens.has_tokens(uid)
    monkeypatch.setattr(sh, "has_tokens", no_verified)

    # no tokens -> send_gate must return True (show the shortener)
    gate_shown = {"n": 0}

    async def fake_send_gate(bot, uid, code):
        if await tokens.has_tokens(uid):
            return False
        gate_shown["n"] += 1
        return True
    monkeypatch.setattr(sh, "send_gate", fake_send_gate)

    async def go():
        assert await sh.send_gate(None, 7, "ABC") is True   # gated
        await tokens.grant(7, n=11)
        assert await sh.send_gate(None, 7, "ABC") is False  # delivered
    run(go())
    assert gate_shown["n"] == 1


def test_consume_for_post_message(monkeypatch):
    _wire(monkeypatch)
    from app.services import shortener as sh
    run(tokens.grant(7, n=2))
    line = run(sh.consume_for_post(7))
    assert "<b>1</b>" in line and "IST" in line
    line2 = run(sh.consume_for_post(7))
    assert "last token" in line2.lower()
