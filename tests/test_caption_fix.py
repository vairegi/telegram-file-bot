"""Regression test for the 'message caption is too long' queue jam (v3.1)."""
import sys, asyncio
sys.path.insert(0, "/home/user/pkg")

import pytest
from app.services import posting


@pytest.fixture
def no_extra(monkeypatch):
    async def _blank(): return ""
    monkeypatch.setattr(posting, "_postcaption_extra", _blank)
    monkeypatch.setattr(posting, "_filecaption_extra", _blank)


def u16(s): return len((s or "").encode("utf-16-le", errors="ignore")) // 2


def test_short_caption_unchanged(no_extra):
    cap = asyncio.run(posting.build_cover_caption("Short Title\nSome body text", 1992))
    assert "#1992" in cap and "Short Title" in cap and "Some body text" in cap
    assert u16(cap) <= 1024


def test_exactly_1024_caption_fits(no_extra):
    # The real jammer: a stored caption already at Telegram's limit.
    cap = asyncio.run(posting.build_cover_caption("X" * 1024, 1992))
    assert u16(cap) <= 1024, f"len={u16(cap)}"
    assert "#1992" in cap and cap.rstrip().endswith("<b>#1992</b>")  # number line survives
    assert cap.startswith("X")     # title head preserved
    assert "…" in cap              # truncation marked


def test_emoji_heavy_caption_fits(no_extra):
    # Emoji are 2 UTF-16 units each — the naive len() check would pass at
    # ~1000 chars but Telegram would still reject. This must fit.
    cap = asyncio.run(posting.build_cover_caption("🔥" * 900, 42))
    assert u16(cap) <= 1024, f"len={u16(cap)}"
    assert "#42" in cap


def test_html_specials_never_split(no_extra):
    # '<' escapes to '&lt;' — truncation must not leave a dangling '&lt'.
    body = "<tag> " * 300
    cap = asyncio.run(posting.build_cover_caption("Title\n" + body, 7))
    assert u16(cap) <= 1024
    assert not cap.rstrip("…").endswith("&l") and not cap.rstrip("…").endswith("&lt")
    assert "&lt;tag&gt;" in cap  # escaping intact where kept


def test_postcaption_extra_still_appended(monkeypatch):
    async def _extra(): return "For Anime Visit Our Site - http://animealpha.cc"
    async def _blank(): return ""
    monkeypatch.setattr(posting, "_postcaption_extra", _extra)
    monkeypatch.setattr(posting, "_filecaption_extra", _blank)
    cap = asyncio.run(posting.build_cover_caption("Y" * 1024, 5))
    assert u16(cap) <= 1024
    assert "animealpha.cc" in cap   # extra survives, body shrank instead
    assert "#5" in cap


def test_file_caption_fits(no_extra):
    cap = asyncio.run(posting.build_file_caption("Z" * 1024, 12, 2, 5))
    assert u16(cap) <= 1024
    assert "File #12" in cap and "2/5" in cap


def test_file_caption_short(no_extra):
    cap = asyncio.run(posting.build_file_caption(None, 3, 1, 1))
    assert cap == "<b>File #3</b>"


def test_real_jammer_caption():
    """Feed the ACTUAL caption of posts.id=4487 from live Turso."""
    import libsql
    TURSO_URL = "libsql://doujinshi-fileshare-fileshare.aws-us-west-2.turso.io"
    TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODc1NTMyMzgsImlkIjoiMDFhMDMyNzktM2YxMC03Yjk5LThjZjUtZDQyODUxMmJlNDFlIiwia2lkIjoiR0owdXZQNXhzZEdpR283eWRpbWpXZ2l6aTNYN2FKR21sWjlrS0N1WnJIdyIsInJpZCI6ImM3YjRlZjkzLTBkNDAtNDcwYi05NWFmLTU4ZTU5M2U5NDgwZiJ9.YW88v1XpiV--5aWfXCnrSij2pl8SkN75sB_STs9geBM38GR4eMSSUMSZwc7BTXGwaaPCz7j_5G1_5rDjHRmWAA"
    tc = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    rows = tc.execute("SELECT id, caption FROM posts WHERE kind='cover' AND published_at IS NULL AND length(COALESCE(caption,''))>=1000 ORDER BY id LIMIT 11").fetchall()
    assert rows, "expected long captions in DB"
    async def _blank(): return ""
    posting._postcaption_extra = _blank
    for pid, caption in rows:
        cap = asyncio.run(posting.build_cover_caption(caption, 1992 + pid))
        assert u16(cap) <= 1024, f"post {pid} composed len={u16(cap)}"
