-- 0004_site_policy.sql -- per-site newsletter policy (newsletter authority + website consent service).
--   subscribe_quiz   1 = POST /api/subscribe needs the bot-defence answer; 0 = the site relies on
--                    mauxy's double opt-in instead (unsubscribe ALWAYS needs the quiz).
--   subscriber_sink  odoo | mautic | none; NULL = global SUBSCRIBER_SINK.
--   doi_owner        mauxy = mauxy mails the DOI link, nothing reaches the sink before the click;
--                    sink  = upstream behaviour: contact goes into the Mautic segment at once and a
--                            Mautic campaign runs the opt-in (only valid with the mautic sink).
-- Existing rows (upstream prod: ludo, ...) keep upstream behaviour exactly; simplify-erp (the
-- website, flywheel SPEC-021) switches to mauxy DOI + Odoo lists without quiz on subscribe.
ALTER TABLE sites ADD COLUMN subscribe_quiz INTEGER NOT NULL DEFAULT 1 CHECK (subscribe_quiz IN (0, 1));
ALTER TABLE sites ADD COLUMN subscriber_sink TEXT CHECK (subscriber_sink IS NULL OR subscriber_sink IN ('odoo', 'mautic', 'none'));
ALTER TABLE sites ADD COLUMN doi_owner TEXT NOT NULL DEFAULT 'mauxy' CHECK (doi_owner IN ('mauxy', 'sink'));

UPDATE sites SET doi_owner = 'sink', subscriber_sink = 'mautic' WHERE site <> 'simplify-erp';
UPDATE sites SET doi_owner = 'mauxy', subscriber_sink = 'odoo', subscribe_quiz = 0 WHERE site = 'simplify-erp';
