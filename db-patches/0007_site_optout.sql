-- 0007_site_optout.sql -- mauxy's own do-not-contact record per site (security review 2026-11-09,
-- finding: routed sites without a list-holding target had no do-not-contact source, so an opted-out
-- address got a DOI mail and reached webhooks again). Written on EVERY unsubscribe that passed the
-- gates (routed or fallback path); read before any DOI mail / marketing delivery of a ROUTED site.
-- Only the SHA-256 of the lower-cased address is stored. Never purged (compliance record); lifted
-- only by an admin on the person's explicit request (POST /api/admin/optouts/{site}/lift).
CREATE TABLE IF NOT EXISTS site_optout (
    site       TEXT NOT NULL,
    email_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (site, email_hash)
);
