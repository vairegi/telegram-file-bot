"""Functional test suite — runs the SAME scenarios against BOTH backends.

turso backend: libsql on a local file DB (per-module tmp dir).
mongo backend: local mongod (single-node replica set — retryWrites capable,
               mirroring Atlas) at 127.0.0.1:27017, throwaway database.

Known intentional difference (documented in GUIDE.md, cosmetic only):
  insert_batch() returns len(rows) on Turso (v2.9 executemany contract —
  duplicates never subtracted) but the TRUE inserted count on Mongo
  (BulkWriteResult). Only the backfill "dupes" stat reads it.

A single event loop is shared process-wide: AsyncMongoClient's connection
pool is bound to the loop that created it (a fresh loop per call was the
bug in an earlier run).
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(params=["turso", "mongo"], scope="module")
def backend(request, tmp_path_factory):
    from app.config import settings
    from app.services import repo

    mode = request.param
    mp = pytest.MonkeyPatch()   # module-scope fixture → plain MonkeyPatch
    mp.setattr(settings, "db_backend", mode)

    if mode == "turso":
        db_file = str(tmp_path_factory.mktemp("db") / "t.db")
        mp.setattr(settings, "turso_database_url", db_file)
        mp.setattr(settings, "turso_auth_token", "")
        from app import db as dbmod
        dbmod.reset_conn()
        dbmod.init_schema()
    else:
        from pymongo import MongoClient
        name = f"pytest_repo_{os.getpid()}"
        mc = MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=4000)
        mc.drop_database(name)
        mc.close()
        mp.setattr(settings, "mongodb_uri",
                   "mongodb://127.0.0.1:27017/?retryWrites=true")
        mp.setattr(settings, "mongodb_db_name", name)
        from app import mongo_db
        _run(mongo_db.reset_conn())
        _run(mongo_db.init_schema())

    repo.cache_flush()
    yield repo, settings
    mp.undo()


def _fresh(repo, settings):
    """Wipe every table/collection between scenarios."""
    if settings.db_backend == "mongo":
        from pymongo import MongoClient
        mc = MongoClient("mongodb://127.0.0.1:27017")
        mc.drop_database(settings.mongodb_db_name)
        mc.close()
        from app import mongo_db
        _run(mongo_db.reset_conn())
        _run(mongo_db.init_schema())
    else:
        from app import db as dbmod
        dbmod.reset_conn()
        for t in ["posts", "channels", "settings", "admins", "favorites",
                  "user_directory", "backup_progress", "backup_history"]:
            dbmod.execute(f"DELETE FROM {t}")
        dbmod.execute("DELETE FROM sqlite_sequence")
    repo.cache_flush()


# ---------------------------------------------------------------------------
async def _settings(repo, settings):
    assert await repo.get_setting("nope") is None
    assert await repo.get_setting("nope", "dflt") == "dflt"
    await repo.set_setting("k1", "v1")
    assert await repo.get_setting("k1") == "v1"
    await repo.set_setting("k1", "v2")
    assert await repo.get_setting("k1") == "v2"
    await repo.set_setting("k1", None)
    assert await repo.get_setting("k1") is None
    await repo.set_setting("b1", "1")
    assert await repo.get_setting_bool("b1") is True
    assert await repo.get_setting_bool("missing", True) is True
    await repo.set_setting("i1", "42")
    assert await repo.get_setting_int("i1") == 42
    await repo.set_setting_json("j1", {"a": [1, 2]})
    assert await repo.get_setting_json("j1") == {"a": [1, 2]}
    await repo.set_cursor(-100111, 555)
    assert await repo.get_cursor(-100111) == 555


async def _channels_admins(repo, settings):
    await repo.add_channel(-1001, "database", "DB Chan")
    await repo.add_channel(-1002, "main", "Main Chan")
    await repo.add_channel(-1003, "log")
    await repo.add_channel(-1004, "backup", "BK")
    await repo.add_channel(-1002, "main", None)      # re-add: title kept
    chans = await repo.list_all_channels()
    assert {c["chat_id"] for c in chans} == {-1001, -1002, -1003, -1004}
    assert (await repo.get_channel(-1002))["title"] == "Main Chan"
    assert [c["chat_id"] for c in await repo.get_database_channels()] == [-1001]
    assert (await repo.get_log_channel())["chat_id"] == -1003
    assert (await repo.database_chat_ids()) == {-1001}
    await repo.update_channel_title(-1004, "BK2")
    assert (await repo.get_channel(-1004))["title"] == "BK2"
    await repo.remove_channel(-1004)
    assert await repo.get_channel(-1004) is None

    await repo.add_admin(10, is_super=True)
    await repo.add_admin(20)
    assert await repo.is_admin(10) and await repo.is_super_admin(10)
    assert await repo.is_admin(20) and not await repo.is_super_admin(20)
    assert not await repo.is_admin(99)
    await repo.add_admin(20, is_super=True)
    assert await repo.is_super_admin(20)
    await repo.remove_admin(20)
    assert not await repo.is_admin(20)
    admins = await repo.list_admins()
    assert [a["user_id"] for a in admins] == [10]


async def _posts(repo, settings):
    DB = -1001
    c1 = await repo.insert_cover(DB, 100, "Title One\nrest", "photo", None, None)
    c2 = await repo.insert_cover(DB, 200, "Title Two", "photo", "fid2", "n.jpg")
    assert c1 and c2 and c1 != c2
    assert await repo.insert_cover(DB, 100, "dupe", "photo", None, None) is None
    assert await repo.post_exists(DB, 100) is True
    assert await repo.post_exists(DB, 999) is False
    f1 = await repo.insert_file(DB, 101, 100, "f1 cap", "document", None, "a.pdf")
    f2 = await repo.insert_file(DB, 102, 100, None, "sticker", None, None)
    assert f1 and f2
    assert await repo.insert_file(DB, 101, 100, None, "document", None, "a.pdf") is None
    n = await repo.insert_batch([
        ("cover", "photo", DB, 300, None, "T3", None, None, None),
        ("file", "document", DB, 301, 300, None, None, "b.cbz", None),
        ("cover", "photo", DB, 100, None, "dupe", None, None, None),  # dupe
    ])
    # Turso executemany returns len(rows)=3 (v2.9 contract); Mongo returns
    # the true inserted count = 2. See module docstring.
    assert n == (2 if settings.db_backend == "mongo" else 3)

    assert (await repo.get_post_by_id(c1))["caption"].startswith("Title One")
    bycode = await repo.get_post_by_code((await repo.get_post_by_id(c2))["code"])
    assert bycode["id"] == c2
    assert (await repo.find_cover_before(DB, 150))["id"] == c1
    assert [f["id"] for f in await repo.files_of_cover(DB, 100)] == [f1, f2]

    assert await repo.queued_cover_count() == 3
    assert await repo.total_cover_count() == 3
    assert await repo.total_file_count() == 3
    assert await repo.published_cover_count() == 0
    assert (await repo.next_queued_cover())["id"] == c1
    pred = await repo.predicted_number_of_next(2)
    assert [p["predicted_number"] for p in pred] == [1, 2]

    n1 = await repo.mark_published(c1, -1002, 9001, file_id="cached1")
    assert n1 == 1
    n2 = await repo.mark_published(c2, -1002, 9002)
    assert n2 == 2
    assert (await repo.mark_published(c2, -1002, 9002)) == 2   # repost: no new #
    assert await repo.highest_post_number() == 2
    assert (await repo.get_post_by_number(1))["id"] == c1
    assert (await repo.get_post_by_id(c1))["file_id"] == "cached1"
    await repo.update_file_id(c2, "fid2b")
    assert (await repo.get_post_by_id(c2))["file_id"] == "fid2b"

    skipped = await repo.skip_first_n(1, -1002)
    assert skipped == 1
    assert await repo.queued_cover_count() == 0
    assert await repo.highest_post_number() == 3

    n_reset = await repo.jumpto_number(2)
    assert n_reset == 2
    assert await repo.highest_post_number() == 1
    assert await repo.queued_cover_count() == 2
    n2b = await repo.mark_published(c2, -1002, 9002)
    assert n2b == 2                                            # no #N gap

    await repo.unskip_by_number(1)
    assert (await repo.get_post_by_id(c1))["published_at"] is None
    assert await repo.delete_post_by_number(2) is True        # c2, published as #2
    assert (await repo.get_post_by_id(c2))["kind"] == "skip"  # soft-deleted
    assert (await repo.get_post_by_id(c1))["kind"] == "cover"  # c1 unaffected
    t3 = await repo.insert_cover(DB, 400, "zz unique needle", "photo", None, None)
    assert t3
    hits = await repo.find_by_caption("unique needle")
    assert len(hits) == 1 and hits[0]["id"] == t3
    assert await repo.find_by_caption("no such text") == []
    await repo.mark_published(c1, -1002, 9001)
    await repo.queue_reset()
    assert await repo.highest_post_number() == 0
    assert await repo.published_cover_count() == 0
    n_again = await repo.mark_published(c1, -1002, 9001)
    assert n_again == 1                                        # restarts at #1


async def _favorites(repo, settings):
    DB = -1001
    c1 = await repo.insert_cover(DB, 100, "Fav Cover One", "photo", None, None)
    c2 = await repo.insert_cover(DB, 200, "Fav Cover Two", "photo", None, None)
    f1 = await repo.insert_file(DB, 101, 100, None, "document", None, "x.pdf")
    f2 = await repo.insert_file(DB, 102, 100, None, "document", None, "y.pdf")
    f3 = await repo.insert_file(DB, 201, 200, None, "document", None, "z.pdf")
    await repo.add_favorite(42, f1)
    await repo.add_favorite(42, f2)
    await repo.add_favorite(42, f3)
    await repo.add_favorite(7, c1)
    assert await repo.is_favorite(42, f1) is True
    assert await repo.is_favorite(42, 99999) is False
    favs = await repo.list_favorites(42)
    assert {f["id"] for f in favs} == {c1, c2}      # files resolve to covers
    assert {f.get("fav_post_id") for f in favs} <= {f1, f2, f3}
    assert await repo.favorites_count_of_user(42) == 3
    assert await repo.savers_total() == 2
    assert await repo.saves_total() == 4
    tops = await repo.top_savers(limit=10, offset=0)
    assert tops[0]["user_id"] == 42 and tops[0]["saves"] == 3
    covs = await repo.favorite_covers_of_user(42, limit=3)
    assert {c["id"] for c in covs} == {c1, c2}
    removed = await repo.remove_favorites_for_cover(42, DB, 100)
    assert removed == 2
    assert await repo.favorites_count_of_user(42) == 1
    await repo.remove_favorite(42, f3)
    assert await repo.favorites_count_of_user(42) == 0


async def _directory_and_backup(repo, settings):
    await repo.upsert_directory_user(42, "alice", "Alice")
    await repo.upsert_directory_user(7, None, "Bob")
    await repo.upsert_directory_user(42, "alice2", "Alice2")
    users = await repo.get_directory_users([42, 7, 999])
    assert users[42]["username"] == "alice2" and users[7]["first_name"] == "Bob"
    assert 999 not in users
    assert set(await repo.all_user_ids()) == {42, 7}

    DB = -1001
    await repo.insert_cover(DB, 100, "b1", "photo", None, None)
    await repo.insert_cover(DB, 200, "b2", "photo", None, None)
    await repo.insert_file(DB, 101, 100, None, "document", None, "f.pdf")
    msgs = await repo.all_db_source_messages()
    assert [m["source_message_id"] for m in msgs] == [100, 101, 200]

    BK = -1004
    assert await repo.backup_mirrored_count(BK) == 0
    await repo.backup_record(BK, DB, 100, 5001)
    await repo.backup_record(BK, DB, 101, 5002)
    assert await repo.backup_mirrored_count(BK) == 2
    assert await repo.backup_mirrored_set(BK) == {(DB, 100), (DB, 101)}
    await repo.backup_record(BK, DB, 100, 5001)     # replace → still 2
    assert await repo.backup_mirrored_count(BK) == 2
    moved = await repo.backup_reset(BK)
    assert moved == 2
    assert await repo.backup_mirrored_count(BK) == 0
    restored = await repo.backup_undo_reset(BK)
    assert restored == 2
    assert await repo.backup_mirrored_count(BK) == 2
    await repo.backup_reset(BK)
    await repo.backup_delete_all_progress(BK)
    assert await repo.backup_mirrored_count(BK) == 0
    assert await repo.backup_undo_reset(BK) == 0
    assert await repo.backup_is_paused() is False
    await repo.set_backup_paused(True)
    assert await repo.backup_is_paused() is True
    await repo.set_backup_paused(False)
    assert await repo.backup_is_paused() is False


SCENARIOS = [_settings, _channels_admins, _posts, _favorites,
             _directory_and_backup]


def test_all(backend):
    repo, settings = backend
    for scenario in SCENARIOS:
        _fresh(repo, settings)
        _run(scenario(repo, settings))
