-- 0001_init.sql -- mauxy schema baseline.
-- Captures the full schema as of the SQLite-config rollout: the action log plus the
-- runtime-config tables (sites, quiz_questions, mautic_instance). All CREATE ... IF NOT
-- EXISTS so applying this over a DB that already has the tables (created by the old inline
-- lifespan code) is a safe no-op; db_migrate then records it as applied (baseline adoption).

-- Every (un)subscribe attempt. `action` = subscribe|unsubscribe, `site` = calling site.
CREATE TABLE IF NOT EXISTS action_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    email         TEXT NOT NULL,
    source_origin TEXT,
    source_ip     TEXT,
    result        TEXT NOT NULL,
    contact_id    TEXT,
    error_detail  TEXT,
    action        TEXT NOT NULL DEFAULT 'unsubscribe',
    site          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_action_log_email  ON action_log(email);
CREATE INDEX IF NOT EXISTS idx_action_log_result ON action_log(result);
CREATE INDEX IF NOT EXISTS idx_action_log_action ON action_log(action);
CREATE INDEX IF NOT EXISTS idx_action_log_site   ON action_log(site);

-- Per-site registry. `key` is the per-site Bearer key (plaintext, by deliberate decision).
-- Seeded at runtime from MAUXY_SITES (secret); never seeded from a committed patch.
CREATE TABLE IF NOT EXISTS sites (
    key        TEXT PRIMARY KEY,
    site       TEXT NOT NULL,
    segment    TEXT NOT NULL,
    topic      TEXT NOT NULL,
    created_at TEXT
);

-- Bot-defence question bank. choices_json = JSON array; answer = 0-based index.
CREATE TABLE IF NOT EXISTS quiz_questions (
    topic        TEXT    NOT NULL,
    qid          TEXT    NOT NULL,
    question     TEXT    NOT NULL,
    choices_json TEXT    NOT NULL,
    answer       INTEGER NOT NULL,
    PRIMARY KEY (topic, qid)
);

-- Single-row identity of the target Mautic instance (base_url + version).
CREATE TABLE IF NOT EXISTS mautic_instance (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_url   TEXT,
    version    TEXT,
    source     TEXT,
    updated_at TEXT
);
