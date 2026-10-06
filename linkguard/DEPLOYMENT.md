# LinkGuard — Three-Door Security: Deployment Guide (v5.0.1)

```
bot DM "🔓 Verify & Unlock"
   └─► https://<worker>.workers.dev/<public_slug>     DOOR 1 (Turnstile landing)
         └─► /finish?t=<entry-token>                  DOOR 3a (HMAC + referer + burn)
               └─► https://vplink.in/…                paid shortener
                     └─► /finish2?s=<g>&t=<grant>     DOOR 3b (allowlisted referer + burn)
                           └─► t.me/<bot>?start=verify_<TOKEN>
```

Worker down / off / misconfigured → the bot silently falls back to the old
direct-shortener flow (fail-open). Users are never locked out.

## Steps
1. **Storage & Databases → D1 → Create** (name `linkguard`) → **Console** →
   paste all of `schema-fresh.sql` → Run. (If you see "Requests without any
   query are not supported" the schema is already installed.)
2. **Workers & Pages → Create Worker → Edit code** → paste `worker.js` → Deploy.
3. **Settings → Bindings → Add → D1**: name `LINKGUARD_DB`, pick the database.
4. **Settings → Variables and Secrets**: `SIGNING_SECRET` (openssl rand -hex 32),
   `ADMIN_KEY` (16+ chars, shared with the bot), `TURNSTILE_SECRET_KEY`,
   `TURNSTILE_SITE_KEY`, `WORKER_HOSTNAME`, `TG_BOT_TOKEN`, `TG_ADMIN_IDS`,
   `LANDING_WAIT_SECONDS` (0).
5. **Turnstile → Add site** with the worker hostname → paste keys above.
6. Bot DM: `/linkguard setup https://<worker>.workers.dev <ADMIN_KEY>` then
   `/linkguard on`. Shortener host is pushed to the finish2 allowlist
   automatically on `/shortenerapi`; add a different redirect host with
   `/linkguard addrefhost <host>`.

## End-to-end test
0-token account → tap Get File → the DM button must point at
`https://<worker>/<slug>` (NOT vplink). Solve Turnstile → Get Link → shortener
→ back to Telegram → file delivers. Incognito: same flow completes via the
retry page if the cookie chain breaks. `curl -i https://<worker>/finish?t=x`
→ bad_referer denial. Re-open a used `/finish` URL → token_used.
`/linkguard logs 10` shows every event. Smoke: `BASE=… KEY=… node smoke.mjs`.

## Trade-offs
Telegram's in-app browser drops cookies, so /finish2 cannot always re-join the
Door-1 session — the grant + referer allowlist still guard that hop, and when
the session IS present we flag UA mismatches and auto-regenerate the grant.
Every bot-side failure fails open to today's behaviour.
