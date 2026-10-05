-- LinkGuard — fresh-install D1 schema (run once in the D1 console)
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  slug       TEXT NOT NULL,
  ip         TEXT,
  user_agent TEXT,
  nonce      TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  state      TEXT NOT NULL DEFAULT 'served'
);
CREATE INDEX IF NOT EXISTS idx_sessions_slug ON sessions(slug);

CREATE TABLE IF NOT EXISTS claims (
  token      TEXT PRIMARY KEY,
  slug       TEXT NOT NULL,
  session_id TEXT NOT NULL,
  exp        INTEGER NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_exp ON claims(exp);

CREATE TABLE IF NOT EXISTS grants (
  slug       TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  token      TEXT NOT NULL,
  exp        INTEGER NOT NULL,
  ref_hosts  TEXT NOT NULL DEFAULT '[]',
  used       INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS slugs (
  slug       TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,               -- 'real' | 'decoy' | 'grant'
  url        TEXT NOT NULL DEFAULT '',
  expires_at INTEGER,
  grant_slug TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slugs_grant ON slugs(grant_slug);

CREATE TABLE IF NOT EXISTS ref_hosts (
  host   TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'manual'   -- 'auto' | 'manual'
);

CREATE TABLE IF NOT EXISTS rate_limits (
  ip           TEXT PRIMARY KEY,
  count        INTEGER NOT NULL DEFAULT 0,
  window_start INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  event      TEXT NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  slug       TEXT NOT NULL DEFAULT '',
  ip         TEXT NOT NULL DEFAULT '',
  ua         TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_event ON logs(event, id);
CREATE INDEX IF NOT EXISTS idx_logs_slug ON logs(slug, id);
