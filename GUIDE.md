# Owner's Running Guide (v5.0)

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
