# Mauxy — Deployment Instructions

Mauxy is the newsletter (un)subscribe authority in front of Mautic. It deploys to its
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

ALLOWED_ORIGINS=https://ludo.de,https://www.simplify-erp.de
CHALLENGE_SECRET=<openssl rand -hex 32>
# One object per calling site. The `key` is the site's Bearer secret; `segment` =
# Mautic segment alias; `topic` = quiz bank key. The ludo `key` MUST equal ludo's
# prod MAUXY_API_KEY.
MAUXY_SITES=[{"key":"<ludo-site-key>","site":"ludo","segment":"ludo","topic":"odoo"}]
ADMIN_API_KEY=<optional, for /api/actions>
```

## 3. Deploy
```bash
python scripts/deploy.py --build --secret --apply
# or step-by-step: --build  (login+build+push) · --secret (sync Secret) · --apply (manifests)
python scripts/deploy.py --dry-run     # preview manifests without applying
```

## 4. Verify
```bash
curl https://mauxy.engage.wapsol.de/health                       # {"status":"ok","mautic":"reachable"}
curl https://mauxy.engage.wapsol.de/api/challenge \
  -H "Authorization: Bearer <ludo-site-key>"                     # question + choices + challenge
```
A subscribe returns `pending_confirmation` (Mautic sends the opt-in).

## 5. Mautic (admin/config — not code)
Configure a **double-opt-in campaign on the `ludo` segment** so contacts added by
`/api/subscribe` actually receive the confirmation email. Without it, contacts land in
the segment but no opt-in mail is sent.

## Notes
- The Dockerfile ships `main.py`, `db_migrate.py`, the `db-patches/` schema patches and
  `scripts/{deploy,sqlite_db}.py`. Schema is applied by an initContainer
  (`sqlite_db.py --migrate`) before the app starts; the app refuses to start if the DB is
  behind the shipped patches (`MIGRATE_STRICT=false` to warn instead).
- Add a new calling site at runtime via `POST /api/admin/sites` (+ quiz questions via
  `POST /api/admin/quiz`), or seed keys by appending to `MAUXY_SITES` and re-running
  `--secret --apply`. Schema changes ship as new `db-patches/NNNN_*.sql`.
- The legacy `unsubscribe.engage.wapsol.de` / `mautic-unsubscribe-proxy` deployment can
  keep running during the transition; retire it once consumers point at `mauxy.engage`.
