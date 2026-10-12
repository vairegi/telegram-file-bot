"""v5.1.0: bot-side client helpers for /finish2 referer observe-mode."""
import asyncio

from app.services import linkguard


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Rec:
    def __init__(self, ret):
        self.calls = []
        self.ret = ret

    async def __call__(self, method, path, payload=None, params=None, timeout=12.0):
        self.calls.append((method, path, payload))
        return self.ret


def test_set_observe_on_payload(monkeypatch):
    rec = Rec({"ok": True, "observe_until": 123})
    monkeypatch.setattr(linkguard, "_request", rec)
    r = run(linkguard.set_observe(True, 15))
    assert r["ok"] is True
    assert rec.calls == [("POST", "/api/admin/observe", {"on": True, "minutes": 15})]


def test_set_observe_off_payload(monkeypatch):
    rec = Rec({"ok": True, "observe_until": 0})
    monkeypatch.setattr(linkguard, "_request", rec)
    run(linkguard.set_observe(False, 0))
    assert rec.calls == [("POST", "/api/admin/observe", {"on": False, "minutes": 0})]


def test_observed_hosts_passthrough(monkeypatch):
    rec = Rec({"ok": True, "observe_until": 999, "hosts": [{"host": "x.com", "hits": 2}]})
    monkeypatch.setattr(linkguard, "_request", rec)
    r = run(linkguard.observed_hosts())
    assert r["observe_until"] == 999 and r["hosts"][0]["host"] == "x.com"
    assert rec.calls == [("POST", "/api/admin/observed_hosts", {})]


def test_add_ref_host_payload(monkeypatch):
    rec = Rec({"ok": True, "host": "links.arolinks.com"})
    monkeypatch.setattr(linkguard, "_request", rec)
    r = run(linkguard.add_ref_host("https://links.arolinks.com/abc"))
    assert r["ok"] is True
    assert rec.calls == [("POST", "/api/admin/ref_hosts_add",
                          {"host": "https://links.arolinks.com/abc"})]


def test_clear_observed_bool(monkeypatch):
    monkeypatch.setattr(linkguard, "_request", Rec({"ok": True}))
    assert run(linkguard.clear_observed()) is True
    monkeypatch.setattr(linkguard, "_request", Rec({"ok": False}))
    assert run(linkguard.clear_observed()) is False


def test_all_fail_open_on_none(monkeypatch):
    monkeypatch.setattr(linkguard, "_request", Rec(None))
    assert run(linkguard.set_observe(True, 5)) is None
    assert run(linkguard.observed_hosts()) is None
    assert run(linkguard.add_ref_host("x.com")) is None
    assert run(linkguard.clear_observed()) is False
