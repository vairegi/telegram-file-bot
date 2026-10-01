"""v4.7 richlists tests — SUPERSEDED by v4.9 (columns changed).
Kept as a thin smoke test so the deployed file name stays valid; the full
coverage lives in tests/test_tokens_v49.py and tests/test_richlists_v48.py.
"""
from __future__ import annotations

import asyncio

from app.services import richlists


def test_vcolumn_layout_is_v49():
    assert richlists.VCOLUMNS == ["#", "Name", "Elapsed", "File", "Link Type",
                                  "Count", "Tokens left"]
    assert "Category" not in richlists.VCOLUMNS


def test_daily_key_shape():
    assert richlists._log_key("2026-09-29") == "verified_log:2026-09-29"


def test_record_download_bumps(monkeypatch):
    store: dict = {}

    async def gj(k, d=None):
        return store.get(k, d)

    async def sj(k, v):
        store[k] = v

    monkeypatch.setattr(richlists.repo, "get_setting_json", gj)
    monkeypatch.setattr(richlists.repo, "set_setting_json", sj)
    asyncio.run(richlists.record_download(5, "ABC"))
    rec = store[richlists._log_key()]["5"]
    assert rec["downloads"] == 1
