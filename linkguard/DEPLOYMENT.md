# LinkGuard — Three-Door Security: Deployment Guide (v5.0)

This guide takes you from zero to a fully protected gate. Free tier throughout.

## Architecture recap

```
bot DM "🔓 Verify & Unlock"
   └─► https://<worker>.workers.dev/<public_slug>     DOOR 1 (Turnstile landing)
         └─► /finish?t=<entry-token>                  DOOR 3a (HMAC + referer + burn)
               └─► https://vplink.in/…                paid shortener
                     └─► /finish2?s=<g>&t=<grant>     DOOR 3b (allowlisted referer + burn)
                           └─► t.me/<bot>?start=verify_<TOKEN>
```

If the worker is down or not configured, the bot silently falls back to today's
direct-VPLINK behaviour (fail-open) — users are never locked out.

## 1. Create the D1 database

Cloudflare dashboard → **Storage & Databases → D1 SQL Database → Create** →
name it `linkguard`. Open **Console**, paste the entire contents of
`schema-fresh.sql`, run it.

## 2. Create the Worker

**Workers & Pages → Create Worker → Deploy** (accept the hello-world), then
**Edit code**. Replace the entire contents with `worker.js`. **Deploy**.

## 3. Bind D1 + set secrets

- **Settings → Bindings → Add → D1**: variable name `LINKGUARD_DB`, pick the
  database from step 1.
- **Settings → Variables and Secrets** (encrypt the secrets — "Secret" type):
  - `SIGNING_SECRET` — `openssl rand -hex 32`
  - `ADMIN_KEY` — a long random string (min 16 chars; shared with the bot)
  - `TURNSTILE_SECRET_KEY`, `TURNSTILE_SITE_KEY`
  - `WORKER_HOSTNAME` — your worker's host, e.g. `linkguard.yourname.workers.dev`
  - `TG_BOT_TOKEN` — your bot token (anomaly alerts)
  - `TG_ADMIN_IDS` — comma-separated chat ids (super-admin + any admins)
  - `LANDING_WAIT_SECONDS` — `0` (or 5–15 if you want a countdown)

## 4. Turnstile

**Turnstile → Add site** → hostname = your worker hostname → copy the
site key and secret key into the variables above. Widget is in "managed"
mode — no user interaction beyond the checkbox.

## 5. Bot-side setup (in the bot DM)

```
/linkguard setup https://linkguard.yourname.workers.dev <ADMIN_KEY>
/linkguard on
```

`/shortenerapi` re-pushes its host to the finish2 referer allowlist
automatically. If your shortener redirects from a *different* host than its
API host (e.g. `links.vplink.in`), allow it manually:

```
/linkguard addrefhost links.vplink.in
/linkguard refhosts        # verify the merged list
```

Optional hardening:

```
/linkguard honeypot 10     # mint 10 decoy slugs (any tap = logged + alerted)
/linkguard logs 20         # watch denials live
/linkguard health          # worker reachability
```

## 6. End-to-end test

1. With a 0-token account, tap **📥 Get File** → the DM shows
   **🔓 Verify & Unlock** pointing at `https://<worker>.workers.dev/<slug>`.
2. Open it in a normal browser: Turnstile appears, solve it, tap **Get Link**
   → shortener → finish back to Telegram → file auto-delivers.
3. **Incognito bypass test**: copy the public slug URL, open an incognito
   window, paste it → you land on Door 1 again (cookie gone — that's fine).
   Solve it there → should still complete via the finish2 retry page if the
   cookie/referer chain broke.
4. **Direct-hit test**: `curl -i https://<worker>/finish?t=anything` →
   `bad_referer` denial page (check with `/linkguard logs`).
5. **Replay test**: re-open a `/finish?t=…` URL you already used →
   `token_used` / `grant_used` denial.

## Trade-offs (in plain language)

- **Cookie continuity**: the Telegram in-app browser drops cookies, so
  `/finish2` cannot always tie the visit back to the Door-1 session. The grant
  token + referer allowlist still fully protect that hop; when continuity
  *is* possible we additionally flag user-agent mismatches and auto-regenerate
  the grant with a friendly retry page.
- **Fail-open**: every bot-side LinkGuard failure (down worker, bad key, 500)
  reverts to the current direct-shortener flow. Availability beats strictness.
- **Rate limits** are per-IP in a 60s window (20 claims / 30 finish hits) —
  enough for real users, painful for replay scripts.
