# Mauxy — Deployment Instructions

Mauxy is the newsletter (un)subscribe authority for all sites and the website consent service
(DOI, Selbst-Check, leads into erp.wapsol, LinkedIn login). It deploys to its
own `mauxy` Kubernetes namespace and is fronted by `https://mauxy.engage.wapsol.de`.
All config lives in a gitignored `.env`; `scripts/deploy.py` does build/push, secret
sync, and manifest apply.

## 1. Prerequisites
- Cluster access (`kubectl` context set), Docker, registry creds.
- DNS: an A/CNAME for `mauxy.engage.wapsol.de` → the nginx ingress.
- The `mauxy` namespace and an image-pull secret in it:
  ```bash
  kubectl create namespace mauxy
  kubectl -n mauxy create secret docker-registry <pull-secret-name> \
    --docker-server=crepo.re-cloud.io --docker-username=<user> --docker-password=<pass>
  ```

## 2. Fill `.env` (copy from `.env.example`, never commit)
```ini
DEPLOY_NAMESPACE=mauxy
DEPLOY_IMAGE=crepo.re-cloud.io/re-cloud/mauxy:latest
DEPLOY_DOMAIN=mauxy.engage.wapsol.de
DEPLOY_IMAGE_PULL_SECRET=<pull-secret-name>

REGISTRY_URL=crepo.re-cloud.io
REGISTRY_USER=<user>
REGISTRY_PASSWORD=<pass>

# Reuse the existing Mautic creds (currently in secret `mautic-unsubscribe-credentials`,
# namespace `simplify-web`): kubectl -n simplify-web get secret mautic-unsubscribe-credentials -o yaml
MAUTIC_BASE_URL=https://engage.wapsol.de
MAUTIC_USERNAME=<user>
MAUTIC_PASSWORD=<pass>

ALLOWED_ORIGINS=https://ludo.de,https://simplify-erp.de,https://www.simplify-erp.de
CHALLENGE_SECRET=<openssl rand -hex 32>
# One object per calling site. The `key` is the site's Bearer secret; `segment` =
# Mautic segment alias; `topic` = quiz bank key. The ludo `key` MUST equal ludo's
# prod MAUXY_API_KEY.
# Seed only (empty sites table). Optional per site: subscribe_quiz, subscriber_sink, doi_owner
# (README "Per-site policy"); patch 0004 sets them for rows that already exist.
MAUXY_SITES=[{"key":"<ludo-site-key>","site":"ludo","segment":"ludo","topic":"odoo","subscriber_sink":"mautic","doi_owner":"sink"},{"key":"<simplify public key>","site":"simplify-erp","segment":"","topic":"simplify-erp","subscribe_quiz":false,"subscriber_sink":"odoo"}]
ADMIN_API_KEY=<optional, for /api/actions>

# Website consent service: DOI signing, SMTP, Odoo bot, LinkedIn -- see .env.example
CONFIRM_TOKEN_SECRET=<openssl rand -hex 48>
PUBLIC_BASE_URL=https://mauxy.engage.wapsol.de
PUBLIC_WEBSITE_BASE_URL=https://simplify-erp.de
# SMTP_*, ODOO_*, LEAD_*, SELFCHECK_*, LINKEDIN_*, AUTH_* ...
```

## 3. Deploy
```bash
python scripts/deploy.py --build --secret --apply
# or step-by-step: --build  (login+build+push) · --secret (sync Secret) · --apply (manifests)
python scripts/deploy.py --dry-run     # preview manifests without applying
```

## 4. Verify
```bash
curl https://mauxy.engage.wapsol.de/health                       # {"status":"ok"}
curl https://mauxy.engage.wapsol.de/ready                        # {"status":"ready"}
curl https://mauxy.engage.wapsol.de/health/detail                # sinks, login, sites, quiz
curl https://mauxy.engage.wapsol.de/api/challenge \
  -H "Authorization: Bearer <ludo-site-key>"                     # question + choices + challenge
```
A subscribe returns `pending_confirmation` (ludo: Mautic sends the opt-in; simplify-erp: mauxy mails it).

## 5. Mautic (admin/config — not code)
Configure a **double-opt-in campaign on the `ludo` segment** (`doi_owner=sink` sites only) so
contacts added by `/api/subscribe` actually receive the confirmation email. Without it, contacts
land in the segment but no opt-in mail is sent. Sites with `doi_owner=mauxy` on the Mautic sink
must NOT have such a campaign (mauxy already confirmed them).

## Notes
- Strategy `Recreate` (one RWO PVC, SQLite, in-process outbox): short downtime per rollout.
- The Dockerfile ships the app modules, `vendor/`, `db_migrate.py`, the `db-patches/` schema
  patches and `scripts/{deploy,sqlite_db}.py`. Schema is applied by an initContainer
  (`sqlite_db.py --migrate`) before the app starts; the app refuses to start if the DB is
  behind the shipped patches (`MIGRATE_STRICT=false` to warn instead).
- Add a new calling site at runtime via `POST /api/admin/sites` (+ quiz questions via
  `POST /api/admin/quiz`), or seed keys by appending to `MAUXY_SITES` and re-running
  `--secret --apply`. Schema changes ship as new `db-patches/NNNN_*.sql`.
- Canonical host `mauxy.engage.wapsol.de` (owner decision 2026-11-09): `DEPLOY_DOMAIN`,
  `PUBLIC_BASE_URL`, the site's `VITE_MAUXY_URL` and the LinkedIn portal redirect URL
  (`https://mauxy.engage.wapsol.de/api/auth/linkedin/callback`) all name this one host.
- `unsubscribe.engage.wapsol.de` stays only as an alias for old mail links; the alias ingress is
  a separate later step (the ingress here serves `DEPLOY_DOMAIN` only).
