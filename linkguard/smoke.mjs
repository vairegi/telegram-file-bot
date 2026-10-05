// LinkGuard live smoke test — run with node after deploying:
//   BASE=https://<worker>.workers.dev KEY=<ADMIN_KEY> node smoke.mjs
// Requires a REAL Turnstile solve for the full happy path, so this script
// tests everything around the captcha (admin surface, denial matrices).
const BASE = process.env.BASE;
const KEY = process.env.KEY;
if (!BASE || !KEY) { console.error("Set BASE and KEY env vars"); process.exit(1); }

let pass = 0, fail = 0;
const ok = (name, cond) => { cond ? pass++ : fail++;
  console.log(`${cond ? "✅" : "❌"} ${name}`); };
const req = (path, opts = {}) => fetch(BASE + path, {
  ...opts,
  headers: { "x-admin-key": KEY, ...(opts.headers || {}) },
});

// 1. health
const h = await req("/api/admin/health").then(r => r.json());
ok("admin health", h.ok === true);

// 2. mint a door-1 slug wrapping a fake shortener URL
const m = await req("/api/admin/mint", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ url: "https://vplink.in/smoke-test", ttl_hours: 1,
                         grant_slug: "smokegrant1" }) }).then(r => r.json());
ok("mint", m.ok && /^[A-Za-z0-9_-]{1,32}$/.test(m.slug));
ok("mint url host", m.url.startsWith(BASE + "/"));

// 3. mint2 a grant for a fake deep link
const g = await req("/api/admin/mint2", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ url: "https://t.me/SmokeBot?start=verify_X",
                         ref_hosts: ["vplink.in"] }) }).then(r => r.json());
ok("mint2", g.ok && g.finish2_url.includes("/finish2?s="));

// 4. landing serves Door-1 page with session cookie
const land = await fetch(m.url);
const setCookie = land.headers.get("set-cookie") || "";
ok("landing 200", land.status === 200);
ok("landing sets lg_s cookie", /lg_s=[^;]+; HttpOnly; Secure; SameSite=Lax/.test(setCookie));
const body = await land.text();
ok("landing embeds Turnstile", body.includes("turnstile") && body.includes("nonce"));

// 5. claim without a captcha token must fail
const c = await fetch(BASE + "/api/claim", { method: "POST",
  headers: { "content-type": "application/json", "x-admin-key": KEY },
  body: JSON.stringify({ slug: m.slug, session_id: "nope", cf_token: "fake",
                         nonce: "nope" }) }).then(r => r.json());
ok("claim rejects bad session", c.error === "session_invalid");

// 6. /finish denial matrix
const noRef = await fetch(BASE + "/finish?t=forged.token");
ok("finish bad_referer (no referer)", noRef.status === 403);
const badRef = await fetch(BASE + "/finish?t=forged.token",
  { headers: { referer: "https://evil.example/" } });
ok("finish foreign referer denied", badRef.status === 403);

// 7. /finish2 denial matrix (needs the grant slug from step 3)
const f2none = await fetch(BASE + "/finish2?s=nope&t=nope",
  { headers: { referer: "https://vplink.in/x" } });
ok("finish2 unknown grant", f2none.status === 403);
const f2ref = await fetch(`${BASE}/finish2?s=${g.slug}&t=nope`,
  { headers: { referer: "https://evil.example/" } });
ok("finish2 bad referer", f2ref.status === 403);
const f2sig = await fetch(`${BASE}/finish2?s=${g.slug}&t=bad.signature`,
  { headers: { referer: "https://vplink.in/x" } });
ok("finish2 bad signature", f2sig.status === 403);

// 8. admin auth
const bad = await fetch(BASE + "/api/admin/logs",
  { headers: { "x-admin-key": "wrong" } });
ok("admin rejects bad key", bad.status === 401);
const logs = await req("/api/admin/logs?limit=10").then(r => r.json());
ok("logs readable", Array.isArray(logs.logs));

// 9. revoke
const rv = await req("/api/admin/revoke", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ slug: m.slug }) }).then(r => r.json());
ok("revoke", rv.ok === true);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
