"""v3.8: counters must keep working across day/week/month rollovers (Mongo).

Reproduces the production bug with a fake Mongo collection that enforces _id
uniqueness exactly like MongoDB: old fixed-_id + period-in-filter upserts
raised DuplicateKeyError on day 2 (silently swallowed) -> frozen counters.
"""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo


class DuplicateKeyError(Exception):
    pass


class FakeCol:
    """Minimal Mongo collection with _id uniqueness + upsert semantics."""
    def __init__(self):
        self.docs = {}

    async def update_one(self, flt, upd, upsert=False):
        key = flt.get("_id")
        match = self.docs.get(key)
        if match is not None and any(match.get(k) != v for k, v in flt.items() if k != "_id"):
            match = None
        inserting = False
        if match is None and upsert:
            if key in self.docs:
                raise DuplicateKeyError(f"dup _id: {key}")   # Mongo behaviour
            match = dict(flt)
            self.docs[key] = match
            inserting = True
        if match is None:
            return
        for op, fields in upd.items():
            if op == "$set":
                match.update(fields)
            elif op == "$setOnInsert":
                if inserting:
                    match.update(fields)
            elif op == "$addToSet":
                for k, v in fields.items():
                    match.setdefault(k, [])
                    if v not in match[k]:
                        match[k].append(v)
            elif op == "$inc":
                for k, v in fields.items():
                    parts = k.split(".")
                    d = match
                    for pt in parts[:-1]:
                        d = d.setdefault(pt, {})
                    d[parts[-1]] = int(d.get(parts[-1], 0)) + int(v)

    async def find_one(self, flt, proj=None):
        key = flt.get("_id")
        d = self.docs.get(key)
        if d is None:
            return None
        if any(d.get(k) != v for k, v in flt.items() if k != "_id"):
            return None
        return d


@pytest.fixture
def mongo_env(monkeypatch):
    fake_db = SimpleNamespace(usage_counters=FakeCol(),
                              user_directory_stats=FakeCol())
    fake_mod = SimpleNamespace(
        with_retry=lambda op: op(fake_db))
    monkeypatch.setattr(repo, "_mongo", lambda: True)
    monkeypatch.setitem(sys.modules, "app.mongo_db", fake_mod)
    async def _noop_upsert(uid, username=None, first_name=None):
        return None
    monkeypatch.setattr(repo, "upsert_directory_user", _noop_upsert)
    return fake_db


def test_fake_reproduces_the_original_bug(mongo_env):
    """Sanity: the OLD upsert pattern really does blow up on day 2."""
    col = FakeCol()
    asyncio.run(col.update_one({"_id": "active_today", "day": "2026-09-04"},
                               {"$addToSet": {"uids": 1}}, upsert=True))
    with pytest.raises(DuplicateKeyError):
        asyncio.run(col.update_one({"_id": "active_today", "day": "2026-09-05"},
                                   {"$addToSet": {"uids": 2}}, upsert=True))


def test_active_counters_survive_day_rollover(mongo_env, monkeypatch):
    days = iter(["2026-09-04", "2026-09-04", "2026-09-05"])
    monkeypatch.setattr("app.utils.today_ist", lambda: next(days))
    monkeypatch.setattr("app.utils.week_start_ist", lambda: "2026-08-31")
    monkeypatch.setattr("app.utils.month_start_ist", lambda: "2026-09-01")
    asyncio.run(repo.track_user_seen(1))   # day 1
    asyncio.run(repo.track_user_seen(2))   # day 1
    asyncio.run(repo.track_user_seen(3))   # day 2 — old code crashed here
    monkeypatch.setattr("app.utils.today_ist", lambda: "2026-09-04")
    assert asyncio.run(repo.users_active_today()) == 2
    monkeypatch.setattr("app.utils.today_ist", lambda: "2026-09-05")
    assert asyncio.run(repo.users_active_today()) == 1
    assert asyncio.run(repo.users_active_week()) == 3
    assert asyncio.run(repo.users_active_month()) == 3


def test_fetches_today_and_total_rollover(mongo_env, monkeypatch):
    days = iter(["2026-09-04", "2026-09-05"])
    monkeypatch.setattr("app.utils.today_ist", lambda: next(days))
    asyncio.run(repo.record_file_fetch(1, 5))
    asyncio.run(repo.record_file_fetch(1, 3))   # next day — old code crashed here
    monkeypatch.setattr("app.utils.today_ist", lambda: "2026-09-04")
    assert asyncio.run(repo.fetches_today()) == 5
    monkeypatch.setattr("app.utils.today_ist", lambda: "2026-09-05")
    assert asyncio.run(repo.fetches_today()) == 3
    assert asyncio.run(repo.fetches_total()) == 8


def test_leaderboard_week_rollover(mongo_env, monkeypatch):
    weeks = iter(["2026-08-31", "2026-08-31", "2026-09-07"])
    monkeypatch.setattr("app.utils.week_start_ist", lambda: next(weeks))
    asyncio.run(repo.record_fetch_weekly(1, 10))
    asyncio.run(repo.record_fetch_weekly(1, 5))
    asyncio.run(repo.record_fetch_weekly(9, 4))   # new week — old code crashed
    monkeypatch.setattr("app.utils.week_start_ist", lambda: "2026-08-31")
    top_old = asyncio.run(repo.top_fetchers_week(10))
    assert top_old == [{"user_id": 1, "fetches": 15}]
    monkeypatch.setattr("app.utils.week_start_ist", lambda: "2026-09-07")
    top_new = asyncio.run(repo.top_fetchers_week(10))
    assert top_new == [{"user_id": 9, "fetches": 4}]


def test_month_rollover(mongo_env, monkeypatch):
    months = iter(["2026-09-01", "2026-10-01"])
    monkeypatch.setattr("app.utils.today_ist", lambda: "2026-09-30")
    monkeypatch.setattr("app.utils.week_start_ist", lambda: "2026-09-28")
    monkeypatch.setattr("app.utils.month_start_ist", lambda: next(months))
    asyncio.run(repo.track_user_seen(7))
    asyncio.run(repo.track_user_seen(8))          # October — must not crash
    monkeypatch.setattr("app.utils.month_start_ist", lambda: "2026-09-01")
    assert asyncio.run(repo.users_active_month()) == 1
    monkeypatch.setattr("app.utils.month_start_ist", lambda: "2026-10-01")
    assert asyncio.run(repo.users_active_month()) == 1
