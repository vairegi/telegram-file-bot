# v4.5 — /browse : rich-message multi-level tag browser

## What this adds
A `/browse` command that opens a **Telegram Rich Message** menu driven
entirely by the `#tags` already inside your cover captions:

    Level 0  Sections      📚 Parodies · 🧑 Characters · 🎨 Artists ·
                          👥 Groups · 🏷 Tags  (each shows its tag count)
    Level 1  Tags          top-20 per page, sorted by usage, ⭐ pinned first,
                          ‹ Prev / Next › pagination, « Back
    Level 2  Items         published covers for that tag; each item is a
                          clickable title (RichTextUrl) + a 📥 Get File #N
                          URL button → https://t.me/<bot>?start=get_<code>

Tapping an item opens the EXISTING `/start get_<code>` deep link, so the
ban → shortener → fsub gates, delivery batching, autodelete and the Similar
card are all reused untouched.

## DM vs group
  * Bot DM : `sendRichMessage`, then `editMessageText(rich_message=…)` —
             one message edits in place as you navigate.
  * Groups : `sendRichMessage` with
             `ephemeral_message_parameters={receiver_user_id}` so ONLY the
             requesting user sees the menu; navigation uses
             `editEphemeralMessageText(chat_id, receiver_user_id,
             ephemeral_message_id, rich_message=…)`.

## Admin commands (pinned tags)
    /browse_pin #tag [section]    pin to top of a section (all sections if omitted)
    /browse_unpin #tag [section]  unpin
    /browse_pins                  list pins
Pins are stored in the settings collection (`browse_pins` JSON) — works on
both the Mongo and dormant Turso backends.

## Optional env var
    SUGGEST_URL=https://t.me/<your_username_or_admin>
If set, a "💡 Suggest a tag" URL button is appended at the bottom of every
browse window. If unset, the button is simply omitted.

## Files
    app/services/browse.py        (NEW)  index builder + rich renderers + raw
                                         Bot API transport + brw: state machine
    app/handlers/browse_cmds.py   (NEW)  /browse, brw: callbacks, pin commands
    app/main.py                   (EDIT) router registration + /browse in
                                         USER_MENU + pin cmds in ADMIN_MENU
    tests/test_browse_v45.py      (NEW)  12 tests, exact-JSON-shape assertions
    tests/test_similar_v41.py     (EDIT) +1 word: "browse" added to the
                                         USER_MENU whitelist assertion

## Deploy (your usual drag-and-drop)
Upload the files above into the repo keeping the same paths, then redeploy on
Render. No new pip dependencies (uses stdlib + aiohttp, already in
requirements.txt). No DB migration needed.

## Notes / things to confirm on a real device
  * The strict-prompt doc table said `InputRichBlockButtons` has
    `buttons:Array of Array of RichMessageButton`; the LIVE Bot API docs
    (fetched 2026-09-27) define it as a flat `buttons:Array of
    RichMessageButton` — "List of 1-8 buttons ... shown in ONE ROW". This
    implementation follows the live docs: one InputRichBlockButtons block per
    row, max 8 per row, 2 per row by default. If your client renders rows
    oddly, the fix is `_button_rows(..., per_row=…)` in browse.py.
  * Callback data is `brw:s:<section>`, `brw:t:<section>:<tag10>`,
    `brw:p:<section>:<tag10>:<page>`, `brw:sp:<section>:<page>` — all ≤64
    bytes (asserted in tests).
  * The tag index is rebuilt at most every 5 minutes (in-memory TTL); after
    a big import, wait ~5 min or restart for the menu to see new tags.
  * Live render on a real client could NOT be verified from here (no bot
    token) — please open /browse in DM and in a group and confirm.

## Test evidence
See TEST_OUTPUT.txt: full suite run after the change.

## Known pre-existing failures (NOT from this change)
`tests/test_v42.py::test_delivery_files_go_out_in_parallel` and
`test_delivery_one_bad_file_does_not_block_others` fail identically on a
fresh clone BEFORE this change (they need a live TURSO_DATABASE_URL; same
class as test_repo.py / test_nhentai_api_key.py which the handoff says to
ignore). Baseline: 111 passed + these 2 failed. After v4.5: 122 passed +
the same 2 failed — zero regressions, all 12 new browse tests green.
