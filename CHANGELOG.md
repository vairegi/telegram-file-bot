# v4.7 — /ban list & /verified_users as Rich Message tables

## Commands
  /ban list         Rich table of banned users (# · User · Detail · Tap-to-copy
                    /unban command). Reads Mongo user_directory banned=true
                    rows (Turso banned_users settings JSON as fallback).
                    Replaces the old "open MongoDB" placeholder of /banlist.
                    The existing `/ban <user_id>` behaviour is untouched —
                    only the exact argument "list" is intercepted.
  /verified_users   Rich table of TODAY's verifications, IST day
                    (first capture 00:00, resets 23:59 — storage key is the
                    IST date, verified_log:YYYY-MM-DD). Columns:
                    # · Name · Elapsed · Category · Link Type · Count.

## Rich tables
InputRichBlockTable with RichBlockTableCell rows, is_header on the header
row, is_striped on, bare-string RichText leaves (never {"type":"plain"}).
Sent via the raw Bot API transport from browse.py (aiogram 3.13 has no rich
helpers). If Telegram rejects the rich table (400), the command
AUTOMATICALLY falls back to a monospace HTML <pre> table — it never
hard-fails.

## Data capture (repo.py hook)
verified_set() now calls richlists.record_verification() (failure-safe), so
every verification path (timed + legacy) is logged with count, link type
(derived from the configured shortener API base: vplink/arolink/...), and
category/enriched elapsed when available. No DB migration — the daily log
lives in the settings store.

## Files
  app/services/richlists.py        (NEW)
  app/handlers/richlist_cmds.py    (NEW) — router is included BEFORE
                                   shortener_cmds so `/ban list` wins
  app/services/repo.py             (EDIT: verified_set log hook)
  app/main.py                      (EDIT: router + verified_users menu entry)
  tests/test_richlists_v47.py      (NEW: 11 tests)
  TEST_OUTPUT.txt                  (full suite after change)

## Deploy
Drag-and-drop over the repo (same paths), redeploy on Render. No new
dependencies. Note: Elapsed/Category columns fill in for verifications that
happen after this deploy; pre-existing verified stamps have no per-day data.
Live table rendering unverified from here (no bot token) — please run
/ban list and /verified_users once in DM; plain fallback covers any client
that can't render tables.
