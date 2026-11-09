-- 0012_selfcheck_salutation.sql -- Selbst-Check asks for the salutation (owner request 2026-11-09):
-- 'ms' | 'mr' | 'none' as STATED by the person on the website ("Anrede": Frau / Herr / Keine Angabe),
-- never inferred from the name. Used after the DOI click for the result mail greeting ("Sehr geehrte
-- Frau Dr. Muster," / "Dear Mr Muster,"; none = "Guten Tag {name}," as before), crm.lead.title
-- (res.partner.title by xml id, ms / mr only) and the operator mail / lead description ("Salutation:").
-- Same retention as the e-mail (the row is deleted). NULL for rows of before this patch; a missing
-- salutation (SELFCHECK_SALUTATION_REQUIRED=false, rollout transition) is stored as 'none'.
ALTER TABLE selfcheck_submission ADD COLUMN salutation TEXT;
