-- 0009_odoo_optout_provenance.sql -- provenance of Odoo list opt-outs written by mauxy (security
-- review of 7b52b69: an Odoo opt-out counted as self-service whenever a site_optout self row
-- existed, even if Odoo's own unsubscribe link or staff set it later). One row per site, hashed
-- address and list: the subscription mauxy opted out, mauxy's write time and the subscription's
-- write_date read back from Odoo right after the write. An Odoo opt-out is self-service (soft)
-- ONLY if it matches such a row (same subscription) AND its current write_date is not newer;
-- anything else is a hard block (manual lift).
CREATE TABLE IF NOT EXISTS odoo_optout_mark (
    site            TEXT NOT NULL,
    email_hash      TEXT NOT NULL,
    list_id         INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    write_date      TEXT NOT NULL,
    marked_at       TEXT NOT NULL,
    PRIMARY KEY (site, email_hash, list_id)
);
