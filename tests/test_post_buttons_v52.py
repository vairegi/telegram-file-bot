"""v5.2: post-button customization tests.

  * The main-channel "Get File" button becomes green "⬇️ DOWNLOAD #N".
  * /addbuttontopost extras: first extra sits BESIDE Download (two half-width
    buttons in row 1); every further extra is full-width on its own row.
  * Button colors map to Bot API styles: red->danger, green->success,
    blue->primary; invalid colors rejected; Telegram ignores unknown styles
    gracefully (old aiogram has no `style` — kb builder tolerates that).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/home/user/btn/telegram-file-bot")

from app.services import repo                     # noqa: E402
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

    monkeypatch.setattr(repo, "get_setting", fake_get_setting)
    monkeypatch.setattr(repo, "set_setting", fake_set_setting)
    monkeypatch.setattr(repo, "get_setting_json", fake_get_json)
    monkeypatch.setattr(repo, "set_setting_json", fake_set_json)
    return store


class TestMainButton:
    def test_download_label_and_green(self, fake_settings):
        kb = posting.kb_main_get_file("mybot", "abc", 42)
        btn = kb.inline_keyboard[0][0]
        assert btn.text == "⬇️ DOWNLOAD #42"
        assert btn.url == "https://t.me/mybot?start=get_abc"
        assert getattr(btn, "style", None) == "success"     # green

    def test_no_extras_single_row(self, fake_settings):
        kb = posting.kb_main_get_file("mybot", "abc", 7)
        assert len(kb.inline_keyboard) == 1
        assert len(kb.inline_keyboard[0]) == 1


class TestExtraButtonsLayout:
    def test_first_extra_beside_download(self, fake_settings):
        run(repo.set_setting_json("post_extra_buttons",
                                  [{"label": "BACKUP", "url": "https://t.me/x",
                                    "style": "danger"}]))
        extras = run(posting._extra_post_buttons())   # store -> validated list
        kb = posting.kb_main_get_file("mybot", "abc", 9, extras=extras)
        row0 = kb.inline_keyboard[0]
        assert len(row0) == 2                              # half + half
        assert row0[0].text == "⬇️ DOWNLOAD #9"
        assert row0[1].text == "BACKUP"
        assert row0[1].url == "https://t.me/x"
        assert getattr(row0[1], "style", None) == "danger"  # red

    def test_second_extra_full_width_below(self, fake_settings):
        run(repo.set_setting_json("post_extra_buttons", [
            {"label": "BACKUP", "url": "https://t.me/x", "style": "danger"},
            {"label": "MORE", "url": "https://t.me/y", "style": "primary"},
        ]))
        extras = run(posting._extra_post_buttons())
        kb = posting.kb_main_get_file("mybot", "abc", 9, extras=extras)
        assert len(kb.inline_keyboard) == 2
        assert len(kb.inline_keyboard[0]) == 2             # download + backup
        assert len(kb.inline_keyboard[1]) == 1             # MORE full width
        assert kb.inline_keyboard[1][0].text == "MORE"
        assert getattr(kb.inline_keyboard[1][0], "style", None) == "primary"

    def test_malformed_extra_skipped(self, fake_settings):
        run(repo.set_setting_json("post_extra_buttons", [
            {"label": "", "url": "https://t.me/x"},        # empty label
            {"label": "NOLINK"},                           # missing url
            {"label": "OK", "url": "https://t.me/ok"},     # valid
        ]))
        extras = run(posting._extra_post_buttons())   # malformed dropped HERE
        kb = posting.kb_main_get_file("mybot", "abc", 1, extras=extras)
        flat = [b for row in kb.inline_keyboard for b in row]
        assert [b.text for b in flat] == ["⬇️ DOWNLOAD #1", "OK"]


class TestColorValidation:
    def test_color_map(self):
        assert posting._style_of("red") == "danger"
        assert posting._style_of("GREEN") == "success"
        assert posting._style_of("blue") == "primary"
        assert posting._style_of("") == ""
        assert posting._style_of("purple") == ""
        assert posting._style_of(None) == ""


class TestCommandHelpers:
    def test_add_and_remove(self, fake_settings, monkeypatch):
        from app.handlers import content_cmds as cc

        class _Msg:
            def __init__(self, text):
                self.text = text
                self.replies = []

            async def reply(self, text, parse_mode=None):
                self.replies.append(text)

        m = _Msg("/addbuttontopost BACKUP - https://t.me/backup - red")
        monkeypatch.setattr(cc, "_reject_non_admin", _no_admin)
        run(cc.cmd_addbuttontopost(m))
        extras = run(repo.get_setting_json("post_extra_buttons", []))
        assert extras == [{"label": "BACKUP", "url": "https://t.me/backup",
                           "style": "danger"}]
        assert "✅" in m.replies[-1]

        m2 = _Msg("/removebuttonfrompost 1")
        run(cc.cmd_removebuttonfrompost(m2))
        assert run(repo.get_setting_json("post_extra_buttons", [])) == []
        assert "🗑" in m2.replies[-1]

    def test_bad_color_rejected(self, fake_settings, monkeypatch):
        from app.handlers import content_cmds as cc

        class _Msg:
            text = "/addbuttontopost X - https://t.me/x - purple"

            async def reply(self, text, parse_mode=None):
                self.last = text

        m = _Msg()
        monkeypatch.setattr(cc, "_reject_non_admin", _no_admin)
        run(cc.cmd_addbuttontopost(m))
        assert "❌" in m.last
        assert run(repo.get_setting_json("post_extra_buttons", [])) == []


async def _no_admin(msg):
    return False
