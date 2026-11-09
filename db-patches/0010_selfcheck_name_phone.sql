-- 0010_selfcheck_name_phone.sql -- Selbst-Check asks for the person's name (required, first + last in
-- one field, max 100) and an optional phone (max 40, digits spaces + ( ) / - only) (owner request
-- 2026-11-09). Stored with the submission, same retention as the e-mail (pending 7 d, confirmed 90 d:
-- the row is deleted). On delivery: crm.lead contact_name / phone, result mail greeting, operator mail.
-- NULL for rows of before this patch and while SELFCHECK_NAME_REQUIRED=false (rollout transition).
ALTER TABLE selfcheck_submission ADD COLUMN name TEXT;
ALTER TABLE selfcheck_submission ADD COLUMN phone TEXT;
