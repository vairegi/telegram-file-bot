# Owner's Running Guide (v5.2)

## Colored post buttons (v5.2)
- The DOWNLOAD button is green automatically after this deploy.
- Add an extra button beside it:
  `/addbuttontopost BACKUP - https://t.me/yourbackupchannel - red`
  (first extra = beside DOWNLOAD; the next ones stack full-width below).
- Manage: `/listpostbuttons` · `/removebuttonfrompost <i>` · `/clearpostbuttons`.

## Shortener rotation (v5.1)
- `/shorteners` — list the rotation (index · name · api status · hosts).
- Add one:
  `/addshortener arolinks | https://arolinks.com/api?api=KEY&url= | links.arolinks.com`
  (3rd part = the hosts its redirects come from, comma/space separated —
  needed so LinkGuard's finish2 accept them; optional but recommended).
- Remove one: `/delshortener 1` or `/delshortener arolinks`
  (index 0 / "vplink" can't be removed — change it with /shortenerapi).
- Users cycle: solve → next shortener → … → wraps to vplink. The pointer is
  per-user, DB-stored, and does NOT reset at the 4 AM token expiry.
- Add/remove auto-pushes all provider hosts to the worker allowlist; verify
  with `/linkguard refhosts`.

## Daily / weekly checks
- `/linkguard health` — worker alive. If not, the gate silently fails open
  (users get the old direct-VPLINK flow), so nothing breaks — but fix it.
- `/linkguard logs 10` — scan for `finish_denied` bursts (scraper probes)
  and any `honeypot_hit` (someone is probing your slugs).

## When you add or change a shortener (vplink -> gplinks / arolinks / ...)
1. `/shortenerapi <new template ending in url=>` — the finish2 referer host
   is pushed to the worker automatically.
2. If that shortener redirects from a DIFFERENT host than its API host
   (e.g. `links.arolinks.com` while the API lives on `arolinks.com`), allow it:
   `/linkguard addrefhost links.arolinks.com`
3. `/linkguard refhosts` — confirm auto + manual + live lists agree.

## Monthly housekeeping
- Re-mint decoys: `/linkguard honeypot 10`.
- Prune old logs in the D1 console:
  `DELETE FROM logs WHERE created_at < strftime('%s','now') - 2592000;`

## If users report gate problems
1. `/linkguard off` — instant rollback to pre-v5 behaviour.
2. `/linkguard health`; check Turnstile keys + D1 binding in the worker.
3. Render logs for lines containing `linkguard`.

## Deploy ritual
- Drag-and-drop the changed files from the release zip (paths preserved),
  redeploy Render. Never commit `.env`.
