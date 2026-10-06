# v5.0.2 — hotfix: landing route never matched (404 "Not found" on every captcha link)

Root cause: the Door-1 route guard was

    path.length > 1 && !path.includes("/")

Every URL path starts with "/", so `path.includes("/")` is ALWAYS true and the
guard ALWAYS false — GET /<slug> fell through to the final 404 even for
freshly-minted slugs (confirmed live: mint -> 200 with slug in D1, then
GET /<slug> -> 404). Fixed to `!path.slice(1).includes("/")` (reject only
paths with a SECOND slash). Verified with a routing unit test over 7 paths.

Deploy: Workers & Pages -> linkguard -> Edit code -> paste this worker.js ->
Deploy. No env/binding/bot changes needed.
