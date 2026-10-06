/* ============================================================================
   LinkGuard — "Three-Door" link security gate (Cloudflare Workers + D1)
   v5.0.2 (2026-10-06)
   v5.0.2 hotfix: landing-route predicate `!path.includes("/")` could NEVER be
   true (every URL path starts with "/"), so GET /<slug> always fell through
   to the 404 "Not found" even for freshly-minted slugs. Fixed to
   `!path.slice(1).includes("/")` (no SECOND slash).
   v5.0.1 hotfix: crypto.timingSafeEqual is Node-only — replaced with the
   Workers-safe timingSafeEq below.

     bot DM button -> GET /<slug>      DOOR 1: Turnstile landing + session
                   -> POST /api/claim  DOOR 2: verify + mint entry token
                   -> GET /finish      DOOR 3a: HMAC token, referer, burn
                   -> shortener        (paid gate, outside this worker)
                   -> GET /finish2     DOOR 3b: grant token, referer allowlist
                   -> t.me/<bot>?start=verify_<TOKEN>

   Env (Settings -> Variables and Secrets):
     LINKGUARD_DB (D1 binding) · SIGNING_SECRET · ADMIN_KEY ·
     TURNSTILE_SECRET_KEY · TURNSTILE_SITE_KEY · WORKER_HOSTNAME ·
     TG_BOT_TOKEN · TG_ADMIN_IDS (comma-separated) · LANDING_WAIT_SECONDS (0)
   ========================================================================== */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
    const ua = request.headers.get("user-agent") || "";
    const path = url.pathname;

    try {
      if (request.method === "GET" && path === "/api/health")
        return json({ ok: true, slugs: (await countSlugs(env)) });
      if (path.startsWith("/api/admin/"))
        return await handleAdmin(request, url, env, ip, ua);
      if (request.method === "POST" && path === "/api/claim")
        return await handleClaim(request, env, ip, ua);
      if (request.method === "GET" && path === "/finish")
        return await handleFinish(request, url, env, ip, ua, ctx);
      if (request.method === "GET" && path === "/finish2")
        return await handleFinish2(request, url, env, ip, ua, ctx);
      // DOOR 1 landing: a single path segment after the leading slash, e.g.
      // /FeLPHf6W8r3mGA  (v5.0.2: slice(1) — the leading slash is always there)
      if (request.method === "GET" && path.length > 1 &&
          !path.slice(1).includes("/"))
        return await handleLanding(path.slice(1), request, url, env, ip, ua, ctx);
      return new Response("Not found", { status: 404 });
    } catch (e) {
      // Surface the real message (no secrets in it) so bugs are never silent.
      return new Response("Internal error: " + ((e && e.message) || String(e)),
                          { status: 500 });
    }
  },
};

/* ---------------------------------------------------------------- helpers */
const SLUG_RE = /^[A-Za-z0-9_-]{1,32}$/;
const SESSION_TTL = 5400;           // 90 min
const TOKEN_TTL = 90;               // /finish entry token
const GRANT_TTL = 900;              // /finish2 grant
const CLAIM_RATE = 20;              // claims per ip per window
const FINISH_RATE = 30;             // finish hits per ip per window

function json(obj, status = 200, headers = {}) {
  return new Response(JSON.stringify(obj), {
    status, headers: { "content-type": "application/json", ...headers },
  });
}
function b64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function hmac(secret, data) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key,
    new TextEncoder().encode(data)));
}
/* Constant-time string compare (Workers-safe: crypto.timingSafeEqual is a
   Node.js API and does NOT exist in the Workers runtime). */
function timingSafeEq(a, b) {
  const ea = new TextEncoder().encode(String(a));
  const eb = new TextEncoder().encode(String(b));
  if (ea.length !== eb.length) return false;
  let diff = 0;
  for (let i = 0; i < ea.length; i++) diff |= ea[i] ^ eb[i];
  return diff === 0;
}
const now = () => Math.floor(Date.now() / 1000);
function rnd(n = 16) {
  const b = new Uint8Array(n); crypto.getRandomValues(b);
  return b64url(b.buffer);
}
function parseCookies(req) {
  const out = {};
  for (const part of (req.headers.get("cookie") || "").split(";")) {
    const i = part.indexOf("="); if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}
async function logEvent(env, ctx, { event, reason = "", slug = "", ip = "", ua = "", detail = "" }) {
  ctx.waitUntil(env.LINKGUARD_DB.prepare(
    `INSERT INTO logs (ts, event, reason, slug, ip, ua, detail, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
    .bind(new Date().toISOString(), event, reason, slug, ip, ua.slice(0, 300),
      detail.slice(0, 500), now()).run().catch(() => {}));
}
async function alertTg(env, ctx, text) {
  const ids = (env.TG_ADMIN_IDS || "").split(",").map(s => s.trim()).filter(Boolean);
  const token = env.TG_BOT_TOKEN || "";
  if (!token || !ids.length) return;
  ctx.waitUntil((async () => {
    for (const id of ids) {
      try {
        await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ chat_id: id, text, parse_mode: "HTML" }),
        });
      } catch (e) { /* alert must never break the gate */ }
    }
  })());
}
async function rateLimited(env, ip, max) {
  const w = now() - (now() % 60);
  await env.LINKGUARD_DB.prepare(
    `INSERT INTO rate_limits (ip, count, window_start) VALUES (?, 1, ?)
     ON CONFLICT(ip) DO UPDATE SET
       count = CASE WHEN window_start = ? THEN count + 1 ELSE 1 END,
       window_start = ?`)
    .bind(ip, w, w, w).run();
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT count FROM rate_limits WHERE ip = ?").bind(ip).first();
  return (row?.count || 0) > max;
}
async function countSlugs(env) {
  const r = await env.LINKGUARD_DB.prepare(
    "SELECT COUNT(*) c FROM slugs WHERE kind = 'real'").first();
  return r?.c || 0;
}
async function verifyTurnstile(env, token, ip) {
  const body = new FormData();
  body.append("secret", env.TURNSTILE_SECRET_KEY || "");
  body.append("response", token || "");
  body.append("remoteip", ip);
  const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify",
    { method: "POST", body });
  const j = await r.json();
  return !!(j.success && j.hostname === env.WORKER_HOSTNAME);
}
function cookieHeader(sessionId) {
  return `lg_s=${sessionId}; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_TTL}; Path=/`;
}

/* ------------------------------------------------------- DOOR 1: landing */
async function handleLanding(slug, request, url, env, ip, ua, ctx) {
  if (!SLUG_RE.test(slug)) return new Response("Not found", { status: 404 });
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM slugs WHERE slug = ?").bind(slug).first();
  if (!row || (row.expires_at && now() > row.expires_at))
    return new Response("Not found", { status: 404 });
  if (row.kind === "decoy") {
    await logEvent(env, ctx, { event: "honeypot_hit", reason: "decoy_tap",
      slug, ip, ua });
    alertTg(env, ctx,
      `🍯 <b>LinkGuard honeypot hit</b>\nslug: <code>${slug}</code>\nip: <code>${ip}</code>`);
    return new Response("Not found", { status: 404 });
  }

  const sessionId = rnd(18);
  const nonce = rnd(18);
  await env.LINKGUARD_DB.prepare(
    `INSERT INTO sessions (session_id, slug, ip, user_agent, nonce, created_at, state)
     VALUES (?, ?, ?, ?, ?, ?, 'served')`)
    .bind(sessionId, slug, ip, ua.slice(0, 300), nonce, now()).run();
  await logEvent(env, ctx, { event: "landing", slug, ip, ua });

  const wait = Math.max(0, Math.min(15, parseInt(env.LANDING_WAIT_SECONDS || "0", 10) || 0));
  const html = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verify &amp; Continue</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
 body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 .card{background:#1e293b;border-radius:16px;padding:32px;max-width:380px;width:90%;
       text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.5)}
 h1{font-size:20px;margin:0 0 8px} p{color:#94a3b8;font-size:14px}
 button{background:#3b82f6;border:0;color:#fff;padding:12px 28px;border-radius:10px;
        font-size:16px;cursor:pointer;width:100%}
 button:disabled{background:#334155;color:#64748b;cursor:not-allowed}
 .cf-turnstile{margin:16px auto 8px;display:flex;justify-content:center}
 #wait{color:#f59e0b;font-size:13px;min-height:18px}
</style></head><body><div class="card">
<h1>🔒 Human Verification</h1>
<p>Complete the check below to unlock your download link.</p>
<div class="cf-turnstile" data-sitekey="${env.TURNSTILE_SITE_KEY || ""}"
     data-callback="onTs"></div>
<div id="wait">${wait > 0 ? `Link unlocks in ${wait}s…` : ""}</div>
<button id="go" disabled>Get Link</button>
<script>
const S = {sid:${JSON.stringify(sessionId)}, slug:${JSON.stringify(slug)},
           nonce:${JSON.stringify(nonce)}, wait:${wait}};
let tsOk = false, waited = S.wait <= 0, timer = null;
function refresh(){ document.getElementById("go").disabled = !(tsOk && waited); }
if (!waited){ let left = S.wait; timer = setInterval(()=>{ left--;
  document.getElementById("wait").textContent = left > 0 ? "Link unlocks in "+left+"s…" : "";
  if (left <= 0){ clearInterval(timer); waited = true; refresh(); } }, 1000); }
function onTs(){ tsOk = true; refresh(); }
document.getElementById("go").onclick = async () => {
  const btn = document.getElementById("go"); btn.disabled = true;
  btn.textContent = "Verifying…";
  try {
    const r = await fetch("/api/claim", {method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({slug:S.slug, session_id:S.sid,
        cf_token: document.querySelector("[name=cf-turnstile-response]")?.value || "",
        nonce: S.nonce})});
    const j = await r.json();
    if (j.token) { window.location.href = j.finish_url; }
    else { btn.textContent = "Get Link"; btn.disabled = false;
           document.getElementById("wait").textContent = j.error || "Try again"; }
  } catch(e){ btn.textContent = "Get Link"; btn.disabled = false; }
};
</script></div></body></html>`;
  return new Response(html, { status: 200, headers: {
    "content-type": "text/html; charset=utf-8",
    "set-cookie": cookieHeader(sessionId),
    "cache-control": "no-store" } });
}

/* ------------------------------------------------------- DOOR 2: claim */
async function handleClaim(request, env, ip, ua) {
  if (await rateLimited(env, ip, CLAIM_RATE))
    return json({ error: "rate_limited" }, 429);
  let body; try { body = await request.json(); } catch { return json({ error: "bad_request" }, 400); }
  const { slug, session_id, cf_token, nonce } = body || {};
  if (!SLUG_RE.test(slug || "") || !session_id || !nonce)
    return json({ error: "bad_request" }, 400);
  const s = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM sessions WHERE session_id = ?").bind(String(session_id)).first();
  if (!s || s.slug !== slug) return json({ error: "session_invalid" }, 403);
  if (now() - s.created_at > SESSION_TTL) return json({ error: "session_expired" }, 403);
  if (s.state !== "served") return json({ error: "session_used" }, 403);
  if (s.nonce !== String(nonce)) return json({ error: "nonce_mismatch" }, 403);
  if (s.user_agent && s.user_agent !== ua.slice(0, 300))
    return json({ error: "ua_mismatch" }, 403);
  if (!(await verifyTurnstile(env, cf_token, ip)))
    return json({ error: "captcha_failed" }, 403);

  await env.LINKGUARD_DB.prepare(
    "UPDATE sessions SET state = 'out_to_shortener' WHERE session_id = ?")
    .bind(String(session_id)).run();

  const exp = now() + TOKEN_TTL;
  const payload = b64url(new TextEncoder().encode(`${slug}.${session_id}.${exp}.${rnd(8)}`));
  const token = payload + "." + await hmac(env.SIGNING_SECRET, payload);
  await env.LINKGUARD_DB.prepare(
    `INSERT INTO claims (token, slug, session_id, exp, used, created_at)
     VALUES (?, ?, ?, ?, 0, ?)`)
    .bind(token, slug, String(session_id), exp, now()).run();

  return json({ token, expires_in: TOKEN_TTL, finish_url: `/finish?t=${token}` });
}

/* ----------------------------------------------------- DOOR 3a: /finish */
function denyPage(title, msg, retryUrl) {
  const html = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#1e293b;border-radius:16px;padding:32px;max-width:400px;width:90%;
text-align:center}a{color:#3b82f6}</style></head><body><div class="card">
<h1>${title}</h1><p>${msg}</p>${retryUrl ? `<p><a href="${retryUrl}">↩ Back to the start</a></p>` : ""}
</div></body></html>`;
  return new Response(html, { status: 403, headers: {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

async function handleFinish(request, url, env, ip, ua, ctx) {
  if (await rateLimited(env, ip, FINISH_RATE))
    return denyPage("Slow down", "Too many attempts. Please wait a minute.");
  const token = url.searchParams.get("t") || "";
  const referer = request.headers.get("referer") || "";
  const host = env.WORKER_HOSTNAME || "";

  if (!referer.toLowerCase().startsWith(`https://${host}/`)) {
    await logEvent(env, ctx, { event: "finish_denied", reason: "bad_referer",
      ip, ua, detail: referer.slice(0, 200) });
    return denyPage("Access denied", "This link must be opened from its landing page.");
  }
  const dot = token.lastIndexOf(".");
  let fields;
  try {
    if (dot <= 0) throw 0;
    const payload = token.slice(0, dot), sig = token.slice(dot + 1);
    const expect = await hmac(env.SIGNING_SECRET, payload);
    if (!sig || !timingSafeEq(sig, expect)) throw 0;
    fields = atob(payload.replace(/-/g, "+").replace(/_/g, "/")).split(".");
    if (fields.length !== 4) throw 0;
  } catch {
    await logEvent(env, ctx, { event: "finish_denied", reason: "bad_signature", ip, ua });
    return denyPage("Invalid link", "This link's signature did not verify.");
  }
  const slug = fields[0];
  const exp = parseInt(fields[2], 10);
  if (now() > exp) {
    await logEvent(env, ctx, { event: "finish_denied", reason: "expired", slug, ip, ua });
    return denyPage("Link expired", "This link has expired. Go back and unlock a new one.");
  }
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM claims WHERE token = ?").bind(token).first();
  if (!row) {
    await logEvent(env, ctx, { event: "finish_denied", reason: "unknown_token", slug, ip, ua });
    return denyPage("Invalid link", "This link is not recognised.");
  }
  if (row.used) {
    await logEvent(env, ctx, { event: "finish_denied", reason: "token_used", slug, ip, ua });
    return denyPage("Link already used", "Each link works once. Go back for a fresh one.");
  }
  // burn BEFORE redirecting
  await env.LINKGUARD_DB.prepare(
    "UPDATE claims SET used = 1 WHERE token = ? AND used = 0").bind(token).run();
  const s = await env.LINKGUARD_DB.prepare(
    "SELECT url FROM slugs WHERE slug = ?").bind(slug).first();
  if (!s) return new Response("Not found", { status: 404 });
  await logEvent(env, ctx, { event: "finish_ok", slug, ip });
  return Response.redirect(s.url, 302);
}

/* ----------------------------------------------------- DOOR 3b: /finish2 */
async function regenGrantPage(env, publicSlug) {
  const html = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>One more step</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#1e293b;border-radius:16px;padding:32px;max-width:400px;width:90%;
text-align:center}.btn{display:inline-block;background:#3b82f6;color:#fff;
padding:12px 28px;border-radius:10px;text-decoration:none;font-size:16px}</style>
</head><body><div class="card"><h1>🔗 Finish unlocking</h1>
<p>Your browser blocked the final step (a cookie or the shortener's redirect).
Tap below — your verification still counts.</p>
<p><a class="btn" href="https://HOST/SLUG">Continue to download</a></p>
</div></body></html>`.replace("HOST", env.WORKER_HOSTNAME || "").replace("SLUG", publicSlug);
  return new Response(html, { status: 200, headers: {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

async function handleFinish2(request, url, env, ip, ua, ctx) {
  if (await rateLimited(env, ip, FINISH_RATE))
    return denyPage("Slow down", "Too many attempts. Please wait a minute.");
  const slug = url.searchParams.get("s") || "";
  const grant = url.searchParams.get("t") || "";
  const referer = (request.headers.get("referer") || "").toLowerCase();
  const sid = parseCookies(request).lg_s || "";

  const deny = async (reason) => {
    await logEvent(env, ctx, { event: "finish2_denied", reason,
      slug: slug.slice(0, 32), ip, ua, detail: referer.slice(0, 200) });
    // If we know the session, regenerate its grant once and offer a retry.
    if (sid) {
      const sess = await env.LINKGUARD_DB.prepare(
        "SELECT slug, user_agent FROM sessions WHERE session_id = ?").bind(sid).first();
      if (sess) {
        const publicSlug = await env.LINKGUARD_DB.prepare(
          "SELECT slug FROM slugs WHERE grant_slug = ?").bind(slug).first();
        if (publicSlug && reason !== "grant_used" && reason !== "expired") {
          const exp = now() + GRANT_TTL;
          const payload = b64url(new TextEncoder().encode(`${slug}.${exp}.${rnd(8)}`));
          const tok = payload + "." + await hmac(env.SIGNING_SECRET, payload);
          await env.LINKGUARD_DB.prepare(
            "UPDATE grants SET token = ?, exp = ?, used = 0 WHERE slug = ?")
            .bind(tok, exp, slug).run();
          return regenGrantPage(env, publicSlug.slug);
        }
      }
    }
    return denyPage("Access denied",
      "The final verification step did not complete. Please start over from the bot.");
  };

  if (!SLUG_RE.test(slug)) return deny("bad_slug");
  const g = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM grants WHERE slug = ?").bind(slug).first();
  if (!g) return deny("unknown_grant");
  if (g.used) return deny("grant_used");

  const hosts = JSON.parse(g.ref_hosts || "[]");
  const refOk = hosts.some(h =>
    referer === `https://${h}` || referer.startsWith(`https://${h}/`) ||
    referer.startsWith(`https://${h}?`));
  if (!refOk) return deny("bad_referer");

  let fields;
  try {
    const dot = grant.lastIndexOf(".");
    if (dot <= 0) throw 0;
    const payload = grant.slice(0, dot), sig = grant.slice(dot + 1);
    const expect = await hmac(env.SIGNING_SECRET, payload);
    if (!sig || !timingSafeEq(sig, expect)) throw 0;
    fields = atob(payload.replace(/-/g, "+").replace(/_/g, "/")).split(".");
    if (fields.length !== 3 || fields[0] !== slug) throw 0;
  } catch { return deny("bad_signature"); }
  if (now() > parseInt(fields[1], 10)) return deny("expired");

  // Same-browser continuity (advisory). Cookie absent = Telegram in-app
  // browser / cross-browser flow -> still allow (documented trade-off).
  if (sid) {
    const sess = await env.LINKGUARD_DB.prepare(
      "SELECT user_agent FROM sessions WHERE session_id = ?").bind(sid).first();
    if (sess && sess.user_agent && sess.user_agent !== ua.slice(0, 300)) {
      await logEvent(env, ctx, { event: "ua_mismatch", slug, ip, ua });
      alertTg(env, ctx, `⚠️ <b>LinkGuard UA mismatch</b>\nslug: <code>${slug}</code>`);
    }
  }

  await env.LINKGUARD_DB.prepare(
    "UPDATE grants SET used = 1 WHERE slug = ? AND used = 0").bind(slug).run();
  await logEvent(env, ctx, { event: "finish2_ok", slug, ip });
  return Response.redirect(g.url, 302);
}

/* ------------------------------------------------------ admin endpoints */
async function handleAdmin(request, url, env, ip, ua) {
  const key = request.headers.get("x-admin-key") || "";
  const expect = env.ADMIN_KEY || "";
  if (!expect || key.length !== expect.length || !timingSafeEq(key, expect))
    return json({ error: "unauthorized" }, 401);
  const db = env.LINKGUARD_DB;
  const p = url.pathname;

  if (p === "/api/admin/health")
    return json({ ok: true, slugs: await countSlugs(env) });
  if (p === "/api/admin/mint" && request.method === "POST") {
    const b = await request.json();
    const dest = String(b.url || "");
    if (!dest.startsWith("https://")) return json({ error: "bad_url" }, 400);
    const slug = rnd(10);
    const ttl = Math.min(24 * 365, parseInt(b.ttl_hours || "168", 10) || 168);
    await db.prepare(
      `INSERT INTO slugs (slug, kind, url, expires_at, grant_slug, created_at)
       VALUES (?, 'real', ?, ?, ?, ?)`)
      .bind(slug, dest, now() + ttl * 3600, b.grant_slug || null, now()).run();
    return json({ ok: true, slug, url: `https://${env.WORKER_HOSTNAME}/${slug}` });
  }
  if (p === "/api/admin/mint2" && request.method === "POST") {
    const b = await request.json();
    const dest = String(b.url || "");
    if (!dest.startsWith("https://t.me/")) return json({ error: "bad_url" }, 400);
    const slug = rnd(10);
    const exp = now() + GRANT_TTL;
    const payload = b64url(new TextEncoder().encode(`${slug}.${exp}.${rnd(8)}`));
    const tok = payload + "." + await hmac(env.SIGNING_SECRET, payload);
    const hosts = Array.isArray(b.ref_hosts) ? b.ref_hosts.map(String).slice(0, 20) : [];
    for (const h of hosts) {
      await db.prepare(
        `INSERT INTO ref_hosts (host, source) VALUES (?, 'auto')
         ON CONFLICT(host) DO NOTHING`).bind(h.toLowerCase()).run();
    }
    await db.prepare(
      `INSERT INTO grants (slug, url, token, exp, ref_hosts, used, created_at)
       VALUES (?, ?, ?, ?, ?, 0, ?)`)
      .bind(slug, dest, tok, exp, JSON.stringify(hosts), now()).run();
    return json({ ok: true, slug,
      finish2_url: `https://${env.WORKER_HOSTNAME}/finish2?s=${slug}&t=${tok}` });
  }
  if (p === "/api/admin/revoke" && request.method === "POST") {
    const b = await request.json();
    const slug = String(b.slug || "");
    if (!SLUG_RE.test(slug)) return json({ error: "bad_slug" }, 400);
    await db.prepare("DELETE FROM slugs WHERE slug = ?").bind(slug).run();
    await db.prepare("DELETE FROM grants WHERE slug = ?").bind(slug).run();
    return json({ ok: true });
  }
  if (p === "/api/admin/ref_hosts" && request.method === "POST") {
    const b = await request.json();
    for (const h of (b.hosts || [])) {
      await db.prepare(
        `INSERT INTO ref_hosts (host, source) VALUES (?, 'manual')
         ON CONFLICT(host) DO NOTHING`).bind(String(h).toLowerCase()).run();
    }
    return json({ ok: true });
  }
  if (p === "/api/admin/ref_hosts" && request.method === "GET") {
    const rows = await db.prepare("SELECT host FROM ref_hosts").all();
    return json({ ok: true, hosts: (rows.results || []).map(r => r.host) });
  }
  if (p === "/api/admin/decoys" && request.method === "POST") {
    const b = await request.json();
    const n = Math.max(1, Math.min(100, parseInt(b.n || "5", 10) || 5));
    const ttl = parseInt(b.ttl_hours || "720", 10) || 720;
    let created = 0;
    for (let i = 0; i < n; i++) {
      await db.prepare(
        `INSERT INTO slugs (slug, kind, url, expires_at, grant_slug, created_at)
         VALUES (?, 'decoy', '', ?, NULL, ?)`)
        .bind(rnd(10), now() + ttl * 3600, now()).run();
      created++;
    }
    return json({ ok: true, created });
  }
  if (p === "/api/admin/logs" && request.method === "GET") {
    const limit = Math.min(200, parseInt(url.searchParams.get("limit") || "50", 10) || 50);
    const reason = url.searchParams.get("reason");
    const slug = url.searchParams.get("slug");
    let sql = "SELECT * FROM logs"; const binds = []; const conds = [];
    if (reason) { conds.push("reason = ?"); binds.push(reason); }
    if (slug) { conds.push("slug = ?"); binds.push(slug); }
    if (conds.length) sql += " WHERE " + conds.join(" AND ");
    sql += " ORDER BY id DESC LIMIT ?"; binds.push(limit);
    const rows = await db.prepare(sql).bind(...binds).all();
    return json({ ok: true, logs: rows.results || [] });
  }
  if (p === "/api/admin/list" && request.method === "GET") {
    const rows = await db.prepare(
      `SELECT slug FROM slugs WHERE kind = 'real' AND
       (expires_at IS NULL OR expires_at > ?)`).bind(now()).all();
    return json({ ok: true, slugs: (rows.results || []).map(r => r.slug) });
  }
  return json({ error: "not_found" }, 404);
}
