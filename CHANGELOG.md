# v4.8 — /banlist fix, /verified_users fix, instant ban, /banmessage

## 1. /verified_users now actually records (root cause fixed)
The v4.7 hook logged inside repo.verified_set(), which has NO elapsed/code
context — and in the deployed file it was only wired into the Mongo branch.
v4.8 logs at the REDEMPTION sites instead:
  * setup_cmds.py — deep-link /start verify_<TOKEN> path: elapsed is computed
    from token.issued_at, Category comes from the delivered post's caption
    tags (➤ Parodies/Tags/Artists), Link Type from your configured shortener
    API base (vplink/arolink/gplinks/...).
  * callbacks.py — the ✅ I've Verified rescue path (no token left there, so
    Elapsed shows "—" for those rows; Count/Category/Link Type still log).
Count = number of solves today per user; Elapsed = how long the LAST solve
took. Daily window stays IST 00:00→23:59 (storage key verified_log:YYYY-MM-DD).

## 2. /banlist (single word) + INSTANT ban
  * The ban table now answers /banlist directly (the richlist router is
    registered before shortener_cmds, whose old placeholder is stubbed out).
  * The 3-strike warning system is GONE: the bypass branch in setup_cmds.py
    now bans on the FIRST detected bypass (elapsed < 120s), notifies the
    super-admin as before, sends NO fresh gate, and the ban reason records
    the elapsed time. The strike counter/decay plumbing in
    shortener.py/repo.py is left in place but no longer gates anything.

## 3. /banmessage — custom ban DM with HTML
  /banmessage <html>     set the message (validated against Telegram first:
                         a preview is sent to YOUR DM; broken HTML is
                         rejected and NOT saved)
  /banmessage            show current
  /banmessage preview    DM yourself a rendered preview
  /banmessage clear      reset to default
Supports <b> <i> <u> <s> <code> <pre> <a href> <blockquote>. Placeholders:
{elapsed} (solve seconds, e.g. 16.4s) and {user_id}. A bad placeholder never
breaks the ban flow — the raw template is sent instead.

## Files
  app/services/richlists.py      (REWRITE — log-at-redemption + ban msg tpl)
  app/handlers/richlist_cmds.py  (REWRITE — /banlist /verified_users /banmessage)
  app/handlers/setup_cmds.py     (EDIT — instant ban + redemption logging)
  app/handlers/callbacks.py      (EDIT — ✅ button redemption logging)
  app/handlers/shortener_cmds.py (EDIT — old /banlist placeholder stubbed)
  app/services/repo.py           (EDIT — v4.7 verified_set hook REMOVED, would
                                  otherwise double-log with wrong context)
  app/main.py                    (EDIT — banlist/banmessage menu entries)
  tests/test_richlists_v48.py    (NEW — 12 tests)
  tests/test_v43_shortener.py    (EDIT — bypass test updated to instant-ban
                                  expectation)
  TEST_OUTPUT.txt                (full suite run after the change)

## Test status
141 passed; only the 2 pre-existing test_v42 parallel-delivery failures
(identical on a fresh clone, need live TURSO_DATABASE_URL — no regressions).

## Deploy
Drag-and-drop over the repo (same paths), redeploy on Render. No new deps,
no DB migration. Existing banned users are untouched. Then: solve a
shortener with a test account and /verified_users will show the row with
Elapsed filled; type /banlist for the table.
