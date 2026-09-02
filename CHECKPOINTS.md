# CHECKPOINTS.md — build ledger (v3.0 dual-backend Turso→MongoDB)

## v3.0 — Turso → MongoDB Atlas dual backend + migration engine

| %   | Milestone (what was added)                                          | File-wrapper URL                                  | AI Drive mirror |
|-----|---------------------------------------------------------------------|---------------------------------------------------|-----------------|
| 45  | mongo_db.py + async repo.py (82 fns) + migrate.py + main wiring     | https://www.genspark.ai/api/files/s/6YbwM8Ep      | /telegram-file-bot-v3.0-checkpoints/work_ckpt45.tar.gz |
| 55  | All 7 services converted to async (posting/sync/userbot/backup/fsub/scheduler/autodelete) + set_counter_floor race fix | https://www.genspark.ai/api/files/s/2v3N5roA | /telegram-file-bot-v3.0-checkpoints/work_ckpt55.tar.gz |
| 65  | All 10 handler files converted + requirements.txt + render.yaml     | https://www.genspark.ai/api/files/s/D9WF1ouV      | /telegram-file-bot-v3.0-checkpoints/work_ckpt65.tar.gz |
| 75  | Dual-backend test suite + row-shape contract fix (_row fills NULLs) | https://www.genspark.ai/api/files/s/EAhOnOTG      | /telegram-file-bot-v3.0-checkpoints/work_ckpt75.tar.gz |
| 85  | Live rehearsal on real Turso (read-only): 39.5k posts byte-identical | (folded into 95%)                                | — |
| 95  | channels INTEGER-PK scan fix + delta convergence; rehearsal GREEN   | (folded into 100%)                               | — |
| 100 | Final package + GUIDE.md + this ledger                              | (delivered in chat; AI Drive mirror path beside)  | /telegram-file-bot-v3.0-checkpoints/telegram-file-bot_v3.0.zip |

## Test results (sandbox-verified, this build)

**Dual-backend unit suite** (`tests/test_repo.py`, same 5 scenarios on both):
```
tests/test_repo.py::test_all[turso] PASSED
tests/test_repo.py::test_all[mongo] PASSED
2 passed
```

**Live rehearsal** (`rehearsal.py` — real production Turso READ-ONLY → local
MongoDB single-node replica set, mirroring Atlas topology with retryWrites):
```
FULL PASS 1 copied 80166 rows
FULL PASS 2 (resume) copied 0 rows          # idempotency proven
DELTA PASS copied 40631 rows                # catches live writes mid-migration
VERIFY: PASS
posts: 39535/39535        channels: 4/4      settings: 30/38
admins: 2/2               favorites: 563/563 user_directory: 497/497
backup_progress: 39535/39535   backup_history: 0/0
counter.post_number=1991 (turso max #N=1991)   # #N parity — no gaps
mongo write round-trip (favorite add/remove): OK
post_id counter above migrated max: OK (new id=39604)
```

## What changed vs v2.9

- **New**: `app/mongo_db.py` (async PyMongo layer, self-healing, auto
  collections+indexes, `counters` collection for numeric ids).
- **New**: `app/services/migrate.py` + `app/handlers/migrate_cmds.py`
  (`/migrate_mongo`, `/migrate_mongo delta`, `/migrate_mongo_status`).
- **Rewritten**: `app/services/repo.py` — all 66 functions now `async`,
  dual-backend dispatch on `DB_BACKEND` (`turso` default = no-op deploy).
- **Converted**: every handler + service to `await` the async repo layer.
- **Config**: `config.py` reads `MONGODB_URI`, `MONGODB_DB_NAME`,
  `DB_BACKEND`; `render.yaml` declares them; `requirements.txt` adds
  `pymongo==4.17.0`.
- **Kept intact**: `app/db.py` (Turso) untouched as the frozen fallback.

## Migration runbook (verified)

1. Deploy this code with no env change → bot still on Turso (no-op).
2. Add `MONGODB_URI` + `MONGODB_DB_NAME` on Render → still Turso.
3. `/migrate_mongo` → full copy + delta top-up + field-by-field verify.
4. `/pauseposting` + `/pausebackup` → set `DB_BACKEND=mongo` → redeploy.
5. `/migrate_mongo delta` (catches the redeploy window) → resume posting.
6. Rollback any time = set `DB_BACKEND=turso` and redeploy.
