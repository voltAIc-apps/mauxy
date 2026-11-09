-- 0011_simplify_accent_cta.sql -- branded Selbst-Check mails (owner request 2026-11-09): the accent of
-- simplify-erp becomes the website's red CTA colour (#ea5562, SelfCheckResult.tsx submit / Meetly button);
-- it colours the "Ergebnis online ansehen" / "Bestätigen" mail buttons (mail_html.py) and the focus
-- outline of the confirmation page. Only replaces the 0006 seed value: an accent the owner already set
-- via PUT /api/admin/brands/simplify-erp stays untouched.
UPDATE site_brand SET color_accent = '#ea5562', updated_at = '2026-11-09T00:00:00+00:00'
WHERE site = 'simplify-erp' AND color_accent = '#80d2ff';
