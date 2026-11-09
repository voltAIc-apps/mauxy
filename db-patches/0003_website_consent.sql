-- 0003_website_consent.sql -- website consent service (flywheel SPEC-020 / SPEC-021, login).
-- Tables that used to be created at runtime by main.py / selfcheck_api / leads_api / lead_sync /
-- auth_api (never deployed in that form). All CREATE ... IF NOT EXISTS: safe on any DB at 0002.
-- Data minimisation: no IP / user agent; tokens only as SHA-256; retention in the purge jobs.

-- Newsletter double opt-in: pending signups live here, NOT in any sink, until confirmed.
-- site / segment / sink = the calling site (per-site Bearer key), its target and subscriber sink
-- at submit time (what the person consented to; '' sink = global SUBSCRIBER_SINK).
CREATE TABLE IF NOT EXISTS newsletter_pending (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT,
    email       TEXT NOT NULL,
    locale      TEXT NOT NULL,
    source      TEXT NOT NULL,
    page_url    TEXT NOT NULL,
    site        TEXT NOT NULL DEFAULT '',
    segment     TEXT NOT NULL DEFAULT '',
    sink        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_nl_pending_email ON newsletter_pending(email);

-- Selbst-Check (SPEC-020): what the DOI and the indicative quote need.
CREATE TABLE IF NOT EXISTS selfcheck_submission (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    email         TEXT NOT NULL,
    lang          TEXT NOT NULL,
    picks         TEXT NOT NULL,
    answers       TEXT NOT NULL,
    stay          TEXT,
    rules_version TEXT NOT NULL,
    complete      INTEGER NOT NULL,
    token_hash    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    confirmed_at  TEXT,
    mail_sent     INTEGER,
    notify_sent   INTEGER,
    radar_result  TEXT,
    company       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sc_email ON selfcheck_submission(email, created_at);

-- Anonymous self-check counters: day + event + lang, nothing else.
CREATE TABLE IF NOT EXISTS selfcheck_counter (
    day    TEXT NOT NULL,
    event  TEXT NOT NULL,
    lang   TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, event, lang)
);

-- CTA leads (SPEC-021): data = the validated kind-specific fields (JSON).
CREATE TABLE IF NOT EXISTS lead_submission (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    email        TEXT NOT NULL,
    lang         TEXT NOT NULL,
    newsletter   INTEGER NOT NULL DEFAULT 0,
    page_url     TEXT,
    data         TEXT NOT NULL,
    token_hash   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lead_email ON lead_submission(email, created_at);

-- Outbox of confirmed deliveries to the sinks (one row per confirmed submission).
CREATE TABLE IF NOT EXISTS lead_sync (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    origin          TEXT NOT NULL,
    origin_id       INTEGER NOT NULL,
    ref             TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    lead            TEXT,
    subscribe       TEXT,
    sub_results     TEXT NOT NULL DEFAULT '{}',
    lead_sink       TEXT,
    odoo_lead_id    INTEGER,
    state           TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    done_at         TEXT,
    failure_mailed  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (origin, origin_id)
);
CREATE INDEX IF NOT EXISTS idx_lead_sync_state ON lead_sync(state);

-- Visitor LinkedIn login: pending logins (hashes + timestamps only, no PII).
CREATE TABLE IF NOT EXISTS auth_state (
    id           TEXT PRIMARY KEY,
    cookie_hash  TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    used_at      TEXT
);

-- Finished logins waiting for the site's exchange: profile for max CODE_TTL seconds.
CREATE TABLE IF NOT EXISTS auth_code (
    code_hash    TEXT PRIMARY KEY,
    binding_hash TEXT NOT NULL,
    origin       TEXT NOT NULL,
    ret          TEXT NOT NULL,
    profile      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
