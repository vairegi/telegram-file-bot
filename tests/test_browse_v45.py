"""v4.6 /browse tests — exact JSON shapes + the v4.6 improvements.

Locks in:
  * every RichText leaf node is a bare str (never {"type":"plain", ...});
  * every button has text + EXACTLY ONE action field;
  * every rich block carries a valid "type" string;
  * every callback_data <= 64 BYTES;
  * DM path: sendRichMessage/editMessageText; group path: sendRichMessage
    (+ephemeral_message_parameters)/editEphemeralMessageText;
  * a simulated Telegram 400 surfaces the REAL error text;
  * every emitted callback_data resolves through window_for;
  * v4.6: green Back/Home/Prev/Next nav row incl. 🏠 Home;
  * v4.6: /numberoftags setting controls Level-1 page size (clamped 5..50);
  * v4.6: a new /browse deletes the user's previous menu;
  * v4.6: menus auto-delete after MENU_TTL idle (deleteMessage in DM,
    deleteEphemeralMessage in groups); navigation resets the timer.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import browse

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


def _patch_data(monkeypatch, pins=None, page_size=None):
    async def fake_covers(limit=10000, exclude_id=0):
        return COVERS

    async def fake_get_setting_json(key, default=None):
        if key == "browse_pins":
            return pins or {}
        return default

    async def fake_get_setting_int(key, default=0):
        if key == "browse_page_size":
            return page_size if page_size is not None else default
        return default

    monkeypatch.setattr(browse.repo, "recent_published_covers", fake_covers)
    monkeypatch.setattr(browse.repo, "get_setting_json", fake_get_setting_json)
    monkeypatch.setattr(browse.repo, "get_setting_int", fake_get_setting_int)
    browse._index_cache.update(ts=0.0, by_section={}, covers_by_tag={})


def _blocks(rm):
    assert set(rm.keys()) <= {"blocks", "skip_entity_detection", "is_rtl",
                              "html", "markdown", "media"}
    assert "blocks" in rm
    return rm["blocks"]


def _check_richtext(node, path=""):
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
                    assert len(btn["callback_data"].encode()) <= 64
                if "style" in btn:
                    assert btn["style"] in ("danger", "success", "primary",
                                            "link")
        elif "text" in block:
            _check_richtext(block["text"], f"block[{bi}].text")
    json.dumps(rm)


def _nav_buttons(rm):
    """Buttons of the LAST buttons block (the nav row)."""
    rows = [b for b in _blocks(rm) if b["type"] == "buttons"]
    return rows[-1]["buttons"]


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
        assert await browse.window_for("brw:t:parodies:deadbeef00") is None
        assert await browse.window_for("brw:garbage") is None

    run(go())


def test_every_emitted_callback_resolves(monkeypatch):
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
# v4.6 — nav row: green Back/Home/Prev/Next + 🏠 Home button
# ---------------------------------------------------------------------------
def test_tags_nav_row_green_with_home(monkeypatch):
    _patch_data(monkeypatch, page_size=1)  # 2 tags -> 2 pages -> Prev/Next shown
    by_section, _ = run(browse.get_index(force=True))
    rm = browse.render_tags(by_section, {}, "parodies", page=0, page_size=1)
    _check_shape(rm)
    nav = _nav_buttons(rm)
    texts = [b["text"] for b in nav]
    assert "« Back" in texts and "🏠 Home" in texts and "Next ›" in texts
    for b in nav:
        assert b.get("style") == "success", "nav buttons must be green"
    assert all(b["callback_data"] == "brw:home"
               for b in nav if b["text"] in ("« Back", "🏠 Home"))


def test_items_nav_row_green_with_home(monkeypatch):
    _patch_data(monkeypatch)
    by_section, covers_by_tag = run(browse.get_index())
    covers = covers_by_tag[("parodies", "nijisanji")]
    rm = browse.render_items(covers, "parodies", "nijisanji", bot_name="b")
    nav = _nav_buttons(rm)
    texts = [b["text"] for b in nav]
    assert "« Tags" in texts and "🏠 Home" in texts
    for b in nav:
        assert b.get("style") == "success"


# ---------------------------------------------------------------------------
# v4.6 — /numberoftags page-size setting
# ---------------------------------------------------------------------------
def test_page_size_setting(monkeypatch):
    _patch_data(monkeypatch, page_size=30)
    assert run(browse.get_page_size()) == 30
    _patch_data(monkeypatch, page_size=500)
    assert run(browse.get_page_size()) == browse.MAX_PAGE_SIZE
    _patch_data(monkeypatch, page_size=1)
    assert run(browse.get_page_size()) == browse.MIN_PAGE_SIZE
    _patch_data(monkeypatch, page_size=None)
    assert run(browse.get_page_size()) == browse.DEFAULT_PAGE_SIZE


def test_page_size_controls_pagination(monkeypatch):
    # 'tags' has 3 tags: sole_female, glasses, sole_male.
    # 20/page -> single page; 2/page -> 2 pages with green Next/Prev nav.
    _patch_data(monkeypatch)
    by_section, _ = run(browse.get_index(force=True))

    rm = browse.render_tags(by_section, {}, "tags", page=0, page_size=20)
    para = [b for b in _blocks(rm) if b["type"] == "paragraph"][0]
    assert "Page 1/1" in para["text"]

    rm = browse.render_tags(by_section, {}, "tags", page=0, page_size=2)
    para = [b for b in _blocks(rm) if b["type"] == "paragraph"][0]
    assert "Page 1/2" in para["text"]
    nav = _nav_buttons(rm)
    assert any(b["text"] == "Next ›" and
               b["callback_data"] == "brw:sp:tags:1" and
               b.get("style") == "success" for b in nav)

    rm2 = browse.render_tags(by_section, {}, "tags", page=1, page_size=2)
    para2 = [b for b in _blocks(rm2) if b["type"] == "paragraph"][0]
    assert "Page 2/2" in para2["text"]
    assert any(b["text"] == "‹ Prev" and
               b["callback_data"] == "brw:sp:tags:0" for b in _nav_buttons(rm2))


# ---------------------------------------------------------------------------
# Transport tests — DM rich path vs group ephemeral path (mocked api_post)
# ---------------------------------------------------------------------------
def _fake_dm_msg(uid=7):
    return SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                           from_user=SimpleNamespace(id=uid))


def _fake_group_msg(uid=7):
    return SimpleNamespace(chat=SimpleNamespace(id=-1001, type="supergroup"),
                           from_user=SimpleNamespace(id=uid))


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
        browse._open_menus.clear()

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
        browse._open_menus.clear()

    run(go())
    assert calls[0][0] == "sendRichMessage"
    eph = calls[0][1]["ephemeral_message_parameters"]
    assert eph == {"receiver_user_id": 7}
    assert calls[1][0] == "editEphemeralMessageText"
    assert calls[1][1]["receiver_user_id"] == 7
    assert calls[1][1]["ephemeral_message_id"] == 900


def test_real_telegram_error_is_surfaced(monkeypatch):
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


# ---------------------------------------------------------------------------
# v4.6 — menu lifecycle
# ---------------------------------------------------------------------------
def test_new_browse_deletes_previous_menu(monkeypatch):
    calls = []
    mid = {"n": 100}

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        if method == "sendRichMessage":
            mid["n"] += 1
            return {"message_id": mid["n"]}
        return True

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_dm_msg(), rm)   # msg 101
        await browse.send_browse(bot, _fake_dm_msg(), rm)   # msg 102
        browse._open_menus.clear()

    run(go())
    deletes = [p for m, p in calls if m == "deleteMessage"]
    assert deletes and deletes[0]["message_id"] == 101, \
        "second /browse must delete the first menu (message 101)"


def test_menu_auto_deletes_after_idle(monkeypatch):
    calls = []
    monkeypatch.setattr(browse, "MENU_TTL", 0.02)

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 42}

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_dm_msg(), rm)
        await asyncio.sleep(0.15)  # let the TTL fire

    run(go())
    deletes = [p for m, p in calls if m == "deleteMessage"]
    assert deletes and deletes[0]["message_id"] == 42
    assert 7 not in browse._open_menus


def test_group_menu_auto_delete_uses_ephemeral(monkeypatch):
    calls = []
    monkeypatch.setattr(browse, "MENU_TTL", 0.02)

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 1, "ephemeral_message_id": 900}

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_group_msg(), rm)
        await asyncio.sleep(0.15)

    run(go())
    dele = [p for m, p in calls if m == "deleteEphemeralMessage"]
    assert dele and dele[0]["ephemeral_message_id"] == 900
    assert dele[0]["receiver_user_id"] == 7


def test_navigation_resets_idle_timer(monkeypatch):
    calls = []
    monkeypatch.setattr(browse, "MENU_TTL", 0.08)

    async def fake_post(bot, method, payload):
        calls.append((method, payload))
        return {"message_id": 42}

    monkeypatch.setattr(browse, "api_post", fake_post)
    rm = browse.rich_message([browse.block_paragraph("hi")])

    async def go():
        bot = SimpleNamespace(token="T")
        await browse.send_browse(bot, _fake_dm_msg(), rm)
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(chat=SimpleNamespace(id=42),
                                    message_id=42,
                                    ephemeral_message_id=None))
        # navigate at ~60ms — inside the 80ms TTL — which must RESET the timer
        await asyncio.sleep(0.06)
        await browse.edit_browse(bot, cb, rm)
        await asyncio.sleep(0.06)  # t=120ms: original TTL would have fired
        assert not [m for m, _ in calls if m == "deleteMessage"]
        await asyncio.sleep(0.08)  # t=200ms: new TTL (80ms from nav) fires
        browse._open_menus.clear()

    run(go())
    deletes = [p for m, p in calls if m == "deleteMessage"]
    assert deletes and deletes[0]["message_id"] == 42
