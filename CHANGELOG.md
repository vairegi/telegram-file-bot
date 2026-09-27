# v4.6 — /browse improvements (on top of v4.5)

All four requested improvements are implemented:

## 1. /help now lists every browse command
`app/handlers/setup_cmds.py` /help text gained:
    /browse — browse the library by tags
    /browse_pin #tag [section] — pin a tag in /browse
    /browse_unpin #tag [section] — unpin · /browse_pins — list pins
    /numberoftags N — tags per page in /browse (5-50)
The commands were also registered in the Telegram command menu
(USER_MENU / ADMIN_MENU in app/main.py).

## 2. /numberoftags N — admin controls tags per page
    /numberoftags        → shows the current value
    /numberoftags 30     → Level-1 tag pages now show 30 tags
Stored as the `browse_page_size` setting (works on Mongo AND the dormant
Turso path via repo.get_setting_int/set_setting). Clamped to 5–50,
default 20. Item pages stay at 10 (they are much taller: title + button).

## 3. Green navigation + 🏠 Home button
Every bottom nav row is now green (style="success") and includes Home:
  * Tag pages:    « Back · 🏠 Home · ‹ Prev · Next ›
  * Item pages:   « Tags · 🏠 Home · ‹ Prev · Next ›
Home always returns to the section list (brw:home).

## 4. Menu auto-delete (3 min idle) + old-menu cleanup
  * One menu per user: sending /browse again DELETES that user's previous
    menu first (DM: deleteMessage; group: deleteEphemeralMessage).
  * After 180s without interaction the menu deletes itself. Every tap
    (Back/Next/tag/item navigation) resets the 3-minute timer.
  * Tuning: change MENU_TTL in app/services/browse.py (seconds).

## Files in this zip
    app/services/browse.py        (REWRITE of v4.5 file)
    app/handlers/browse_cmds.py   (REWRITE of v4.5 file, +/numberoftags)
    app/main.py                   (EDIT: router + menus + /numberoftags)
    app/handlers/setup_cmds.py    (EDIT: /help lines)
    tests/test_browse_v45.py      (REWRITE: 20 tests)
    tests/test_similar_v41.py     (EDIT: +browse in USER_MENU whitelist)
    TEST_OUTPUT.txt               (full suite run after the change)

## Deploy
Drag-and-drop these files over the repo (same paths), redeploy on Render.
No new dependencies, no DB migration.

## Test status
Full suite after change: all browse v4.6 tests green, plus the rest of the
suite — the only failures are the 2 PRE-EXISTING test_v42 parallel-delivery
tests that fail identically on a fresh clone before any change (they need a
live TURSO_DATABASE_URL; same class as the two tests the handoff says to
ignore).

## Notes
  * Live client rendering of the new green nav row / Home button and the
    3-minute auto-delete could not be verified from here (no bot token) —
    please confirm once in DM and once in the group.
  * Auto-delete of a GROUP menu uses deleteEphemeralMessage; if a user's
    client was offline, Telegram may already have dropped the ephemeral
    message — that case is logged and ignored by design.
