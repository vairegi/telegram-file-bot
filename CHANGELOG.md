# v4.9 — Token wallet (replaces the 6-hour unlimited unlock)

## Model
  * Solve the shortener -> you get N tokens (default **11**, admin-tunable).
  * **1 post = 1 token** (a post with 3 attached PDFs still costs 1).
  * Tokens **expire at 4:00 AM IST** daily — unused ones are gone. A solve
    inside 60 min of 4 AM rolls the expiry to the NEXT 4 AM (no 2-minute
    window for anyone).
  * **0 tokens (or expired) -> the shortener appears again.** A user holding
    tokens never sees the shortener; they just get the file instantly.
  * Everyone starts fresh: the old verify_ttl_hours stamps are voided.

## Commands
  /tokenpersolve N      admin: tokens granted per solve (1-1000, default 11)
  /mystats              user: now a rich table incl. "🔑 Tokens" + per-solve
  /setverifytime        DEPRECATED — explains the token model instead
  /verified_users       table columns are now:
                        # · Name · Elapsed · File · Link Type · Count · Tokens left
                        - Name is a clickable profile link (tg://user?id=…)
                        - File = posts fetched today (was: Category, removed)
                        - Tokens left = today's remaining balance

## Implementation
  app/services/tokens.py   NEW — wallet (Mongo verified_users {tokens,
                           token_expiry}; Turso fallback = settings JSON
                           "token_wallets"). consume() is an atomic
                           find_one_and_update guarded by
                           token_expiry > now, so concurrent taps can't
                           overspend. Every call is failure-safe.
  app/services/shortener.py is_verified/has_tokens now mean "tokens left";
                           _mark_verified() grants the budget after a solve;
                           send_gate() shows the gate only at 0 tokens;
                           consume_for_post() spends 1 and returns the
                           "N tokens left · expire …" line.
  app/services/posting.py  after the gate passes: spend 1 token, log the
                           download, DM the remaining balance.
  app/handlers/token_cmds.py NEW — /mystats (rich) · /tokenpersolve ·
                           /setverifytime (deprecated). Registered BEFORE
                           setup_cmds/shortener_cmds so it shadows them
                           (same pattern as /banlist).
  app/services/richlists.py record_download() + the new column layout.
  app/main.py              router + /tokenpersolve menu entry.
  app/handlers/setup_cmds.py help text updated.

## Deploy
Drag-and-drop over the repo (same paths), redeploy on Render. No new pip
deps, no DB migration (the existing verified_users collection gains two
fields). Test: with a 0-token account tap Get File -> shortener appears;
solve -> 11 tokens; fetch a post -> DM says "10 tokens left · expire …";
after 11 posts -> shortener again. /mystats shows the balance.
