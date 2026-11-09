-- 0008_optout_reconfirm.sql -- re-signup after an opt-out (owner decision 2026-11-09: "Keep the block.
-- Allow re-signup after a new confirmation click."). Hybrid:
--   self-service opt-outs (source 'self': the person's own unsubscribe via mauxy, and the Odoo list
--     opt-outs mauxy wrote for it) do NOT suppress a new DOI mail, but block every marketing delivery
--     until the person clicks a NEW confirm link; that click lifts the row (audited below)
--   hard blocks (source 'admin', Odoo mail.blacklist, Mautic DNC) still suppress the DOI mail; manual lift
-- Abuse guard: at most one re-signup DOI per site and address per OPTOUT_RESIGNUP_DAYS (30), on top of
-- MAX_PENDING_PER_EMAIL; resignup_at = time of the last one.
ALTER TABLE site_optout ADD COLUMN source TEXT NOT NULL DEFAULT 'self' CHECK (source IN ('self', 'admin'));
ALTER TABLE site_optout ADD COLUMN resignup_at TEXT;
-- the DOI link was issued as a re-signup (after a self-service opt-out); only such a link can lift it
ALTER TABLE newsletter_pending ADD COLUMN resignup INTEGER NOT NULL DEFAULT 0 CHECK (resignup IN (0, 1));
-- audit of every lift: hashed address only, never purged
CREATE TABLE IF NOT EXISTS site_optout_lift (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site         TEXT NOT NULL,
    email_hash   TEXT NOT NULL,
    source       TEXT NOT NULL,
    reason       TEXT NOT NULL CHECK (reason IN ('doi_reconfirm', 'admin')),
    opted_out_at TEXT NOT NULL,
    lifted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_optout_lift ON site_optout_lift(site, email_hash);
