-- 0006_site_brand_routing.sql -- mauxy fully per site (owner requirement 2026-11-09):
--   1. brand profile per site (confirm / neutral page, person-facing mails): site_brand + its
--      per-locale mail footer, per flow/locale/outcome redirect targets, website origins -> site
--   2. action routing ("hooks") per site: ordered targets per (site, action), each a sink + params
--   3. route_delivery: fan-out outbox, ONE row per (event, target), idempotency key per action+target
-- Sites WITHOUT a site_brand row render and behave exactly as before (generic "wapsol GmbH" page,
-- SMTP_FROM, env redirect URLs); sites WITHOUT site_route rows keep the fallback sinks
-- (sites.subscriber_sink / SUBSCRIBER_SINK / LEAD_SINK, ODOO_LIST_*_ID). Validation of every value
-- happens in brand.py / routing.py (admin CRUD); the CHECKs below are the last line.

CREATE TABLE IF NOT EXISTS site_brand (
    site              TEXT PRIMARY KEY,               -- = sites.site (one brand per site, many keys possible)
    display_name      TEXT NOT NULL,
    legal_entity      TEXT,
    color_primary     TEXT CHECK (color_primary IS NULL OR color_primary GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'),
    color_accent      TEXT CHECK (color_accent IS NULL OR color_accent GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'),
    wordmark          TEXT,
    logo              BLOB,                           -- raster only (PNG / WebP), size-capped; served as data: URI
    logo_mime         TEXT CHECK (logo_mime IS NULL OR logo_mime IN ('image/png', 'image/webp')),
    website_url       TEXT,                           -- https; base of the default redirect targets
    imprint_url       TEXT,                           -- https; may contain {lang}
    privacy_url       TEXT,                           -- https; may contain {lang}
    default_locale    TEXT NOT NULL DEFAULT 'de' CHECK (default_locale IN ('de', 'en')),
    mail_from_name    TEXT,                           -- NULL = display name of SMTP_FROM
    mail_from_address TEXT,                           -- NULL = SMTP_FROM; domain must be in MAIL_SENDER_DOMAINS
    mail_reply_to     TEXT,                           -- NULL = SMTP_REPLY_TO
    updated_at        TEXT NOT NULL,
    CHECK ((logo IS NULL) = (logo_mime IS NULL))
);

-- Mail footer (signature block) per site and locale.
CREATE TABLE IF NOT EXISTS site_mail_footer (
    site   TEXT NOT NULL REFERENCES site_brand(site) ON DELETE CASCADE,
    locale TEXT NOT NULL CHECK (locale IN ('de', 'en')),
    footer TEXT NOT NULL,
    PRIMARY KEY (site, locale)
);

-- Explicit redirect target after a DOI click, per flow, locale and outcome. Missing row ->
-- site_brand.website_url + the flow's standard path -> env URL + standard path (before 0006).
CREATE TABLE IF NOT EXISTS site_redirect (
    site    TEXT NOT NULL REFERENCES site_brand(site) ON DELETE CASCADE,
    flow    TEXT NOT NULL CHECK (flow IN ('newsletter', 'lead', 'selfcheck')),
    locale  TEXT NOT NULL CHECK (locale IN ('de', 'en')),
    outcome TEXT NOT NULL CHECK (outcome IN ('confirmed', 'error')),
    url     TEXT NOT NULL,
    PRIMARY KEY (site, flow, locale, outcome)
);

-- Website origin -> site, for the endpoints without a site key (CTA leads, Selbst-Check). The
-- Origin header must still be on LEAD_/SELFCHECK_ALLOWED_ORIGINS; unmapped origin -> DEFAULT_SITE.
CREATE TABLE IF NOT EXISTS site_origin (
    origin TEXT PRIMARY KEY,
    site   TEXT NOT NULL REFERENCES site_brand(site) ON DELETE CASCADE
);

-- Action routing: ordered targets per (site, action). sink + params (JSON, validated per sink in
-- routing.py; webhook secrets are NOT stored here, only an env reference). No row for a
-- (site, action) = fallback sinks of before 0006.
CREATE TABLE IF NOT EXISTS site_route (
    site       TEXT NOT NULL,
    action     TEXT NOT NULL,
    position   INTEGER NOT NULL CHECK (position >= 1),
    sink       TEXT NOT NULL CHECK (sink IN ('odoo', 'mautic', 'webhook')),
    params     TEXT NOT NULL DEFAULT '{}',
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site, action, position)
);

-- Fan-out outbox: one row per (event, target). idem_key = sha256(action|event|target identity):
-- INSERT OR IGNORE makes a repeated enqueue a no-op, the webhook receiver gets it as
-- Idempotency-Key. payload holds the event data (contains the e-mail) and is cleared once delivered.
CREATE TABLE IF NOT EXISTS route_delivery (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key        TEXT NOT NULL UNIQUE,
    event_ref       TEXT NOT NULL,
    site            TEXT NOT NULL,
    action          TEXT NOT NULL,
    position        INTEGER NOT NULL,
    sink            TEXT NOT NULL CHECK (sink IN ('odoo', 'mautic', 'webhook')),
    params          TEXT NOT NULL,
    payload         TEXT,
    state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'done', 'failed')),
    result          TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    failure_mailed  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_route_delivery_state ON route_delivery(state);
CREATE INDEX IF NOT EXISTS idx_route_delivery_event ON route_delivery(event_ref);

-- Site of a CTA lead / Selbst-Check (from the Origin, site_origin); '' = DEFAULT_SITE.
ALTER TABLE lead_submission ADD COLUMN site TEXT NOT NULL DEFAULT '';
ALTER TABLE selfcheck_submission ADD COLUMN site TEXT NOT NULL DEFAULT '';
-- Routed CRM target of an outbox row (JSON {sink, params}); NULL = LEAD_SINK / SUBSCRIBER_SINK.
ALTER TABLE lead_sync ADD COLUMN target TEXT;

-- Seed: simplify-erp with the simplify-erp.de values (prod). Mail sender stays SMTP_FROM (NULL) so
-- the seed never depends on MAIL_SENDER_DOMAINS; footer = the signature the mails carried so far.
-- Privacy URL left NULL: simplify-erp.de has no separate privacy route yet (owner sets it).
INSERT OR IGNORE INTO site_brand (site, display_name, legal_entity, color_primary, color_accent, wordmark,
    website_url, imprint_url, privacy_url, default_locale, updated_at)
VALUES ('simplify-erp', 'Simplify-ERP', 'wapsol GmbH', '#0074b2', '#80d2ff', 'Simplify-ERP',
    'https://simplify-erp.de', 'https://simplify-erp.de/{lang}/impressum', NULL, 'de', '2026-11-09T00:00:00+00:00');
INSERT OR IGNORE INTO site_mail_footer (site, locale, footer) VALUES
    ('simplify-erp', 'de', 'Ashant Chalasani
Simplify-ERP, wapsol GmbH
https://simplify-erp.de/de/impressum'),
    ('simplify-erp', 'en', 'Ashant Chalasani
Simplify-ERP, wapsol GmbH
https://simplify-erp.de/en/impressum');
INSERT OR IGNORE INTO site_origin (origin, site) VALUES
    ('https://simplify-erp.de', 'simplify-erp'),
    ('https://www.simplify-erp.de', 'simplify-erp');
