# v5.1 — multi-shortener rotation

## Model (owner spec)
  * Solve shortener A → get tokens → when you need a fresh solve you get
    shortener B, then C, then back to A (wrap). Pointer advances per solve
    and lives in the DB (settings JSON "shortener_rotation") — restart-proof.
  * Tokens expiring unused at 4 AM IST do NOT reset the pointer: the next
    solve still shows the NEXT shortener.
  * Provider 0 is always the legacy /shortenerapi base ("vplink"); extras via
    /addshortener. A missing/broken provider API falls back to vplink; a
    missing vplink API still fails open (delivery proceeds).
  * Also fixed: a duplicated LinkGuard line in /help (leftover from the v5.0
    upload); test mocks updated to the provider-aware make_short_url_for().

## Changes
  app/services/shortener.py   provider registry (get_providers /
                              all_provider_hosts), rotation pointer
                              (current_provider_index / advance_provider),
                              make_short_url_for(provider), send_gate picks
                              the user's provider; LinkGuard ref_hosts now
                              cover every provider; _mark_verified advances.
  app/handlers/shortener_cmds.py  /shorteners · /addshortener · /delshortener
                              (both push all provider hosts to the worker).
  app/handlers/linkguard_cmds.py  _derived_ref_hosts covers all providers.
  app/main.py · setup_cmds.py     menu + /help entries.
  tests/test_multishortener_v51.py NEW; test_v43_shortener.py and
  test_linkguard_v50.py mocks updated to make_short_url_for(api_base, url).

## Deploy
  Drag-and-drop the changed files, redeploy Render. No pip deps, no DB
  migration. Then: /addshortener arolinks | https://arolinks.com/api?api=KEY&url= | links.arolinks.com

---

# v5.0.1 — hotfix: Workers runtime has no crypto.timingSafeEqual
  Node-only API; in Workers every authenticated /api/admin/* call 500'd in
  ~1ms before any D1 write, so the bot silently fell back to plain
  shortener links. Replaced with a Workers-safe constant-time compare
  (timingSafeEq). Outer catch now reports the real error message.
  Diagnosed live: /api/health 200, wrong key 401, correct key 500.

# v5.0 — LinkGuard "Three-Door" link security

## What it stops
  * Copying the shortener URL mid-flow and replaying it after the timer.
  * Direct hits on the bot's verify deep links by scrapers.
  * Every hop is now HMAC-token + referer + single-use guarded and logged.

## Chain
  bot DM -> /<public_slug>  (Turnstile landing; server session + hidden nonce)
        -> /finish?t=       (DOOR 3a: own-host referer, HMAC, burn-before-redirect)
        -> VPLINK           (paid gate)
        -> /finish2?s=&t=   (DOOR 3b: grant token, allowlisted shortener referer, burn)
        -> t.me/<bot>?start=verify_<TOKEN>  (existing redemption, unchanged)

## Bot changes
  app/services/linkguard.py      NEW — fail-open client (mint/mint2/revoke/
                                 logs/decoys/ref_hosts/health).
  app/handlers/linkguard_cmds.py NEW — /linkguard admin surface.
  app/services/shortener.py      send_gate(): mint2 -> shorten(finish2) -> mint;
                                 button = public slug; deep link never exposed.
                                 ANY failure -> exact v4.x behaviour.
  app/handlers/shortener_cmds.py /shortenerapi re-pushes the referer host.
  app/main.py                    router + ADMIN_MENU entry.
  app/handlers/setup_cmds.py     /help entry.

## Worker (linkguard/)
  worker.js · schema-fresh.sql · DEPLOYMENT.md · smoke.mjs
  D1 tables: sessions, claims, grants, slugs, ref_hosts, rate_limits, logs.

## Deploy
  Worker per linkguard/DEPLOYMENT.md, then in bot DM:
      /linkguard setup <worker-url> <admin-key>
      /linkguard on
  No new pip deps, no DB migration. Fail-open everywhere.
  Tests: tests/test_linkguard_v50.py (crypto mirror + denial matrices + fail-open).

---

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
