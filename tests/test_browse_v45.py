"""v4.5 /browse tests — assert the EXACT JSON shape sent to Telegram.

Per the strict prompt, these tests lock in:
  * every RichText leaf node is a bare str (never {"type":"plain", ...});
  * every button has text + EXACTLY ONE action field (url | callback_data |
    web_app | login_url);
  * every rich block carries the correct "type" string;
  * every callback_data is <= 64 BYTES;
  * DM path uses sendRichMessage/editMessageText, group path uses
    sendRichMessage(+ephemeral_message_parameters)/editEphemeralMessageText;
  * a simulated Telegram 400 surfaces the REAL error text;
  * every callback_data the menu emits resolves back through window_for.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import browse

# ---------------------------------------------------------------------------
# Fixtures: two published covers with realistic caption sections.
# ---------------------------------------------------------------------------
CAP1 = ("Title One\n"
        "➤ Parodies: #nijisanji\n"
        "➤ Artists: #machi\n"
        "➤ Tags: #sole_female #glasses")
CAP2 = ("Title Two — A Very Long Story\n"
        "➤ Parodies: #nijisanji #vspo\n"
        "➤ Characters: #lize_helesta\n"
        "➤ Tags: #glasses")
CAP3 = ("Solo Work\n"
        "➤ Artists: #machi\n"
        "➤ Groups: #some_circle\n"
        "➤ Tags: #sole_male")

COVERS = [
    {"id": 1, "_id": 1, "code": "AAA111", "caption": CAP1, "post_number": 1,
     "kind": "cover", "published_at": "2026-01-01"},
    {"id": 2, "_id": 2, "code": "BBB222", "caption": CAP2, "post_number": 2,
     "kind": "cover", "published_at": "2026-01-02"},
    {"id": 3, "_id": 3, "code": "CCC333", "caption": CAP3, "post_number": 3,
     "kind": "cover", "published_at": "2026-01-03"},
]

_ACTION_FIELDS = ("url", "callback_data", "web_app", "login_url")
_BLOCK_TYPES = {"heading", "paragraph", "buttons", "footer", "divider",
                "table", "pre"}


def _patch_data(monkeypatch, pins=None):
    async def fake_covers(limit=10000, exclude_id=0):
        return COVERS

    async def fake_get_setting_json(key, default=None):
        if key == "browse_pins":
            return pins or {}
        return default

    monkeypatch.setattr(browse.repo, "recent_published_covers", fake_covers)
    monkeypatch.setattr(browse.repo, "get_setting_json",
                        fake_get_setting_json)
    browse._index_cache.update(ts=0.0, by_section={}, covers_by_tag={})


def _blocks(rm):
    assert set(rm.keys()) <= {"blocks", "skip_entity_detection", "is_rtl",
                              "html", "markdown", "media"}
    assert "blocks" in rm
    return rm["blocks"]


def _check_richtext(node, path=""):
    """A leaf is a bare str; typed nodes have a valid type + RichText text."""
    if isinstance(node, str):
        return
    if isinstance(node, list):
        for i, n in enumerate(node):
            _check_richtext(n, f"{path}[{i}]")
        return
    assert isinstance(node, dict), f"{path}: RichText must be str/list/dict"
    assert node.get("type") != "plain", f"{path}: plain-type leaf is WRONG"
    assert "type" in node, f"{path}: typed RichText needs a type"
    if "text" in node:
        _check_richtext(node["text"], f"{path}.text")


def _check_shape(rm):
    """Whole-message shape assertion — run on EVERY window we render."""
    for bi, block in enumerate(_blocks(rm)):
        btype = block.get("type")
        assert btype in _BLOCK_TYPES, f"block[{bi}] bad type {btype!r}"
        if btype == "buttons":
            buttons = block["buttons"]
            assert 1 <= len(buttons) <= 8, f"block[{bi}] row size"
            for j, btn in enumerate(buttons):
                _check_richtext(btn["text"], f"block[{bi}].buttons[{j}].text")
                actions = [f for f in _ACTION_FIELDS if btn.get(f)]
                assert len(actions) == 1, \
                    f"block[{bi}].buttons[{j}] must have EXACTLY ONE action"
                if "callback_data" in btn:
                    assert len(btn["callback_data"].encode()) <= 64, \
                        f"callback_data too long: {btn['callback_data']!r}"
                if "style" in btn:
                    assert btn["style"] in ("danger", "success", "primary",
                                            "link")
        elif "text" in block:
            _check_richtext(block["text"], f"block[{bi}].text")
    # The whole payload must round-trip as JSON (what we actually POST).
    json.dumps(rm)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------
def test_sections_window_shape(monkeypatch):
    _patch_data(monkeypatch)
    by_section, _ = run(browse.get_index())
    rm = browse.render_sections(by_section, {})
    _check_shape(rm)
    # one button per non-empty section
    btns = [b for bl in _blocks(rm) if bl["type"] == "buttons"
            for b in bl["buttons"]]
    labels = [b["text"] for b in btns]
    assert any("Parodies" in l for l in labels)
    assert any("Artists" in l for l in labels)
    assert any("Tags" in l for l in labels)


def test_tags_window_shape_and_counts(monkeypatch):
    _patch_data(monkeypatch)
    by_section, _ = run(browse.get_index())
    rm = browse.render_tags(by_section, {}, "parodies", page=0)
    _check_shape(rm)
    texts = [b["text"] for bl in _blocks(rm) if bl["type"] == "buttons"
             for b in bl["buttons"]]
    assert any("#nijisanji (2)" in t for t in texts)
    assert any("#vspo (1)" in t for t in texts)


def test_items_window_deep_links(monkeypatch):
    _patch_data(monkeypatch)
    by_section, covers_by_tag = run(browse.get_index())
    covers = covers_by_tag[("parodies", "nijisanji")]
    rm = browse.render_items(covers, "parodies", "nijisanji",
                             page=0, bot_name="testbot")
    _check_shape(rm)
    urls = []
    for block in _blocks(rm):
        if block["type"] == "buttons":
            urls += [b.get("url") for b in block["buttons"] if b.get("url")]
        if block["type"] == "paragraph" and isinstance(block["text"], list):
            urls += [n.get("url") for n in block["text"]
                     if isinstance(n, dict) and n.get("type") == "url"]
    assert "https://t.me/testbot?start=get_AAA111" in urls
    assert "https://t.me/testbot?start=get_BBB222" in urls
    # item paragraphs use RichTextUrl nodes (clickable titles)
    link_nodes = [n for block in _blocks(rm)
                  if block["type"] == "paragraph"
                  and isinstance(block["text"], list)
                  for n in block["text"]
                  if isinstance(n, dict) and n.get("type") == "url"]
    assert link_nodes, "item titles must be RichTextUrl nodes"


def test_pinned_tags_float_to_top(monkeypatch):
    _patch_data(monkeypatch, pins={"tags": ["sole_male"]})
    by_section, _ = run(browse.get_index(force=True))
    pins = {"tags": ["sole_male"]}
    rm = browse.render_tags(by_section, pins, "tags", page=0)
    _check_shape(rm)
    first_row = [b for bl in _blocks(rm) if bl["type"] == "buttons"
                 for b in bl["buttons"]]
    assert first_row[0]["text"].startswith("⭐ #sole_male")


def test_tag_id_stability_and_callback_roundtrip(monkeypatch):
    _patch_data(monkeypatch)
    tid = browse.tag_id("parodies", "nijisanji")
    assert tid == browse.tag_id("parodies", "nijisanji")
    assert tid != browse.tag_id("tags", "nijisanji")

    async def go():
        rm = await browse.window_for("brw:home", bot_name="testbot")
        _check_shape(rm)
        rm = await browse.window_for("brw:s:parodies", bot_name="testbot")
        _check_shape(rm)
        rm = await browse.window_for(f"brw:t:parodies:{tid}",
                                     bot_name="testbot")
        _check_shape(rm)
        # unknown tag id -> None (stale menu)
        assert await browse.window_for("brw:t:parodies:deadbeef00") is None
        assert await browse.window_for("brw:garbage") is None

    run(go())


def test_every_emitted_callback_resolves(monkeypatch):
    """Collect every callback_data the menus emit and replay each one."""
    _patch_data(monkeypatch)

    async def go():
        seen = set()
        frontier = ["brw:home"]
        while frontier:
            data = frontier.pop()
            if data in seen:
                continue
            seen.add(data)
            rm = await browse.window_for(data, bot_name="testbot")
            assert rm is not None, f"{data} did not resolve"
            _check_shape(rm)
            for block in _blocks(rm):
                if block["type"] == "buttons":
                    for b in block["buttons"]:
                        cd = b.get("callback_data")
                        if cd and cd.startswith("brw:") and cd not in seen:
                            frontier.append(cd)
        assert "brw:s:parodies" in seen
        assert any(d.startswith("brw:t:parodies:") for d in seen)

    run(go())


def test_callback_length_guard():
    with pytest.raises(browse.BrowseError):
        browse.nav_button("x", "brw:" + "y" * 100)


def test_button_row_size_guard():
    with pytest.raises(browse.BrowseError):
        browse.block_buttons_row(
            [browse.nav_button(str(i), f"brw:{i}") for i in range(9)])


# ---------------------------------------------------------------------------
# Transport tests — DM rich path vs group ephemeral path (mocked api_post)
# ---------------------------------------------------------------------------
def _fake_dm_msg():
    return SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                           from_user=SimpleNamespace(id=7))


def _fake_group_msg():
    return SimpleNamespace(chat=SimpleNamespace(id=-1001, type="supergroup"),
                           from_user=SimpleNamespace(id=7))


def test_dm_send_and_edit(monkeypatch):
    calls = []

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 555}

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_dm_msg(), rm)
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(chat=SimpleNamespace(id=42),
                                    message_id=555,
                                    ephemeral_message_id=None))
        await browse.edit_browse(bot, cb, rm)

    run(go())
    assert calls[0][0] == "sendRichMessage"
    assert "ephemeral_message_parameters" not in calls[0][1]
    assert calls[1][0] == "editMessageText"
    assert calls[1][1]["message_id"] == 555
    assert calls[1][1]["rich_message"] == rm


def test_group_ephemeral_send_and_edit(monkeypatch):
    calls = []

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 1, "ephemeral_message_id": 900}

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_group_msg(), rm)
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(chat=SimpleNamespace(id=-1001),
                                    message_id=1,
                                    ephemeral_message_id=900))
        await browse.edit_browse(bot, cb, rm)

    run(go())
    assert calls[0][0] == "sendRichMessage"
    eph = calls[0][1]["ephemeral_message_parameters"]
    assert eph == {"receiver_user_id": 7}
    assert calls[1][0] == "editEphemeralMessageText"
    assert calls[1][1]["receiver_user_id"] == 7
    assert calls[1][1]["ephemeral_message_id"] == 900


def test_real_telegram_error_is_surfaced(monkeypatch):
    """Simulated 400 -> BrowseError carries Telegram's verbatim description."""

    class FakeResp:
        status = 400

        async def json(self, content_type=None):
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: rich message block #2 "
                                   "has invalid type 'button'"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, url, json=None):
            return FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(browse.aiohttp, "ClientSession", FakeSession)

    async def go():
        bot = SimpleNamespace(token="T")
        with pytest.raises(browse.BrowseError) as ei:
            await browse.api_post(bot, "sendRichMessage", {"chat_id": 1})
        assert "invalid type 'button'" in str(ei.value)
        assert "400" in str(ei.value)

    run(go())
