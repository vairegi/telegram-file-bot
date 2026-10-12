-- v5.1.0 migration for EXISTING LinkGuard D1 databases (idempotent).
-- Run: npx wrangler d1 execute linkguard --remote --file=linkguard/schema-update-v510.sql
-- (or paste into the Cloudflare dashboard D1 console)
CREATE TABLE IF NOT EXISTS ref_observed (
  host TEXT PRIMARY KEY,
  first_seen INTEGER,
  last_seen INTEGER,
  hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS lg_state (
  key TEXT PRIMARY KEY,
  value TEXT
);
