# Checkpoint ledger — v2.9 (backup channel mirroring)

| %   | Milestone                                                                 | Link |
|-----|---------------------------------------------------------------------------|------|
| 20  | v2.8 baseline recovered (33 py files verified)                            | https://www.genspark.ai/api/files/s/sC3NShlu |
| 45  | backup_progress + backup_history tables, repo queries, services/backup.py | (bundled in 75%) |
| 75  | 11 backup commands + main.py wiring + menu + help (35 py files compile)   | https://www.genspark.ai/api/files/s/SDowYUJB |
| 95  | Full backup test suite: 24/24 (schema, full pass, idempotent, resume,     | https://www.genspark.ai/api/files/s/lKXGCkM8 |
|     | pause gate, reset/undo/wipe, FloodWait retry, concurrency guard)          |      |
| 100 | Final package telegram-file-bot_v2.9.zip                                  | (this file) |

## v2.9 — Backup channel mirroring

### Commands
| Command | What it does |
|---|---|
| `/addbackup <chat_id>` | Register a backup channel (bot must be admin; title auto-fetched) |
| `/removebackup <chat_id>` | Unregister (progress rows kept) |
| `/listbackup` | Title-as-invite-link · mirrored count · remaining count |
| `/backup <chat_id>` | Full catch-up pass (progress DMs every 50 messages) |
| `/backup10 <chat_id>` | Mirror next 10 pending messages (smoke test) |
| `/resetbackup [chat_id]` | Archive progress to history, then clear (all channels if no arg) |
| `/undoresetbackup <chat_id>` | Restore the most recently reset batch |
| `/dltbackup <chat_id>` | Permanently wipe progress + history (no undo) |
| `/pausebackup` | Stop the auto-loop and block manual runs |
| `/resumebackup` | Resume; immediate catch-up kick for every backup channel |
| `/backupstatus` | Per-channel: mirrored/total (%) — pending, plus running flag |

### Engine
- Mirrors every `cover`/`file` post from DB channel(s) via `copy_message`
  (skipped posts are never mirrored — by design they carry no content)
- Progress table: `backup_progress(backup_chat_id, db_chat_id, source_message_id)`
  — per-channel cursors, so a new backup starts from #1 while old ones keep
  their place
- Idempotent: re-running mirrors nothing if already up to date
- Per-message failure → recorded as error, run continues; the next pass
  retries the failed messages automatically
- FloodWait-aware: parses the wait seconds from the error, sleeps, retries
  the same message
- Pace: ~3 messages/sec (0.35s between copies), safe for Bot API limits
- Concurrency guard: only one run per backup channel at a time
- Auto-loop: every 60s sweeps pending posts to all backup channels while
  unpaused; `/pausebackup` freezes it, `/resumebackup` catches everything up

### Deploy
1. Push to GitHub → Render auto-deploys (both new tables auto-create on boot)
2. `/addbackup -1004383445396` (bot admin in that channel)
3. `/backup10 -1004383445396` → verify 10 posts land correctly
4. `/backup -1004383445396` → full catch-up with progress DMs
5. `/backupstatus` → per-channel % dashboard
