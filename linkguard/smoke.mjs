// BASE=https://<worker>.workers.dev KEY=<ADMIN_KEY> node smoke.mjs
const BASE = process.env.BASE, KEY = process.env.KEY;
if (!BASE || !KEY) { console.error("Set BASE and KEY"); process.exit(1); }
let pass = 0, fail = 0;
const ok = (n, c) => { c ? pass++ : fail++; console.log(`${c ? "✅" : "❌"} ${n}`); };
const req = (p, o = {}) => fetch(BASE + p, { ...o,
  headers: { "x-admin-key": KEY, ...(o.headers || {}) } });
const h = await req("/api/admin/health").then(r => r.json());
ok("admin health", h.ok === true);
const m = await req("/api/admin/mint", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ url: "https://vplink.in/smoke-test", ttl_hours: 1,
                         grant_slug: "smokegrant1" }) }).then(r => r.json());
ok("mint", m.ok && /^[A-Za-z0-9_-]{1,32}$/.test(m.slug));
const g = await req("/api/admin/mint2", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ url: "https://t.me/SmokeBot?start=verify_X",
                         ref_hosts: ["vplink.in"] }) }).then(r => r.json());
ok("mint2", g.ok && g.finish2_url.includes("/finish2?s="));
const land = await fetch(m.url);
ok("landing 200", land.status === 200);
ok("landing sets lg_s cookie",
   /lg_s=[^;]+; HttpOnly; Secure; SameSite=Lax/.test(land.headers.get("set-cookie") || ""));
ok("landing embeds Turnstile", (await land.text()).includes("turnstile"));
const c = await fetch(BASE + "/api/claim", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ slug: m.slug, session_id: "nope", cf_token: "fake",
                         nonce: "nope" }) }).then(r => r.json());
ok("claim rejects bad session", c.error === "session_invalid");
ok("finish bad_referer", (await fetch(BASE + "/finish?t=forged.token")).status === 403);
ok("finish2 unknown grant", (await fetch(BASE + "/finish2?s=nope&t=nope",
  { headers: { referer: "https://vplink.in/x" } })).status === 403);
ok("finish2 bad referer", (await fetch(`${BASE}/finish2?s=${g.slug}&t=nope`,
  { headers: { referer: "https://evil.example/" } })).status === 403);
ok("finish2 bad signature", (await fetch(`${BASE}/finish2?s=${g.slug}&t=bad.sig`,
  { headers: { referer: "https://vplink.in/x" } })).status === 403);
ok("admin rejects bad key", (await fetch(BASE + "/api/admin/logs",
  { headers: { "x-admin-key": "wrong" } })).status === 401);
const logs = await req("/api/admin/logs?limit=10").then(r => r.json());
ok("logs readable", Array.isArray(logs.logs));
ok("revoke", (await req("/api/admin/revoke", { method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ slug: m.slug }) }).then(r => r.json())).ok === true);
console.log(`\n${pass} passed, ${fail} failed`); process.exit(fail ? 1 : 0);
