# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**mauxy** -- centralized newsletter subscription + unsubscribe proxy for Mautic. Python (FastAPI) microservice that accepts subscribe/unsubscribe requests from SPA frontends and manages contacts via the Mautic REST API. Mautic Basic Auth credentials stay server-side and are never exposed to the browser. Contact-specific responses are uniform (`ok` / `pending_confirmation`) to prevent email enumeration; returns 503 when Mautic is unreachable. GDPR double opt-in with server-side pending state (SQLite) and SMTP confirmation mails (mailcow). Also the public consent service of the simplify-erp.de Selbst-Check (`selfcheck_api.py`, flywheel SPEC-020).

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (set env vars first, see .env.example)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Tests (respx mocks Mautic, mails captured with `MAIL_MODE=memory`):

```bash
PYTHONPATH=. tests/venv/bin/python -m pytest tests -q
```

Numbered: T-SEC-0xx (security review 2026-11-08), T-SC-1xx (self-check, cases in `tests/cases/`).

## Build & Deploy

```bash
docker build -t <your-registry>/mauxy:latest .
docker push <your-registry>/mauxy:latest
```

Deployed to Kubernetes with manifests in `k8s/`. The manifests contain `${VAR}` placeholders -- use `scripts/deploy.py` to render them from `.env` values:

```bash
python scripts/deploy.py --dry-run   # preview
python scripts/deploy.py --apply     # render + kubectl apply
```

Credentials are stored in a k8s Secret (`mauxy-credentials`), not committed -- create via `kubectl create secret generic` (see `k8s/secret.yaml` for the template).

## Architecture

Application logic: `main.py` (newsletter, unsubscribe, health, admin), `selfcheck_api.py` (self-check router, Mautic helpers injected from main), `mailer.py` (SMTP / console / memory), `vendor/selfcheck/` (rules engine + rules JSON, vendored from flywheel by `scripts/sync_selfcheck.py` there; never edit by hand, test T-SC-150 checks the sha256).

- **POST /api/subscribe** -- JSON signup payload from the website (see `website/src/config/api.ts`). `consent` must be `true`, `source` `^[a-z0-9-]{1,40}$`, `pageUrl` on an `ALLOWED_ORIGINS` origin (<= 500 chars), `consentedAt` ignored (server time counts). Honeypot (`company_website`) short-circuits. Reads Mautic only; NOTHING is written to Mautic here. Already confirmed / DNC / throttled (`MAX_PENDING_PER_EMAIL` per 24h) -> no mail. Otherwise a row in `newsletter_pending` (SQLite, SHA-256 of the signed token `{"p": id}`) and a DOI mail via SMTP. Always `200 {"status":"pending_confirmation"}` for contact-specific outcomes; 503 when Mautic/mail is down.
- **GET /api/confirm?token=...** -- DOI landing. Valid signature + max_age AND unused pending row with matching hash (constant time) AND not DNC; then upsert the contact (tag `newsletter_confirmed`, server-side `newsletter_consented_at`), burn the token (single use), 302 to `/{locale}/newsletter/confirmed`; else `.../newsletter/confirm-error?reason=...`. Tokens of the old flow (`{email, locale}`) are refused.
- **POST /api/selfcheck**, **GET /api/selfcheck/confirm**, **POST /api/selfcheck/event**, **GET /api/selfcheck/submissions|stats** (admin) -- see `selfcheck_api.py` docstring and README "Selbst-Check".
- **POST /api/unsubscribe** -- accepts `{"email": "..."}`, looks up the contact in Mautic, adds to DNC list. Returns 503 when Mautic is unreachable (search phase); contact-specific outcomes always return 200. Rate-limited via `slowapi` (default 5/min per IP). Every attempt is logged to SQLite.
- **GET /api/actions** -- admin endpoint to query the action log. Requires `Authorization: Bearer {ADMIN_API_KEY}`. Supports `email`, `result`, `limit`, `offset` query params. Disabled (403) if `ADMIN_API_KEY` is unset.
- **GET /health** -- k8s liveness/readiness probe. Includes Mautic connectivity status in response body (always returns HTTP 200).
- **GET /health/detail** -- richer health endpoint showing degraded/ok status, Mautic detail, and cache age. Always returns HTTP 200.
- CORS is restricted to `ALLOWED_ORIGINS` (must be set via env).

### Mautic prerequisites (one-time)

- Custom contact fields `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (datetime), `newsletter_page_url` (`confirmation_token` only for clearing old values).
- Tags `newsletter_confirmed`, `migration_radar_confirmed` (self-check Radar); optional segment id `MAUTIC_RADAR_SEGMENT_ID`.
- No Mautic e-mail templates needed any more (DOI mails go out via SMTP).

### Persistent storage

SQLite via `aiosqlite` at `ACTION_LOG_DB` (default `/data/actions.db`): `action_log`, `newsletter_pending` (deleted after `NEWSLETTER_PENDING_DAYS`), `selfcheck_submission` (pending 7 d / confirmed 90 d), `selfcheck_counter` (day, event, lang, count only). In k8s, `/data` is backed by a 256Mi `ReadWriteOnce` PVC (`mauxy-data`). Apply `k8s/pvc.yaml` before the deployment.

## Environment Variables

| Variable | Purpose |
|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL |
| `MAUTIC_USERNAME` | Mautic API basic auth user |
| `MAUTIC_PASSWORD` | Mautic API basic auth password |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `RATE_LIMIT` | slowapi rate limit for subscribe/unsubscribe (default `5/minute`) |
| `CONFIRM_RATE_LIMIT` | slowapi rate limit for `/api/confirm` (default `30/minute`) |
| `ACTION_LOG_DB` | SQLite database path (default `/data/actions.db`) |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` (disabled if unset) |
| `CONFIRM_TOKEN_SECRET` | `itsdangerous` signing key for double opt-in tokens |
| `CONFIRM_TOKEN_MAX_AGE` | Token lifetime in seconds (default 86400) |
| `PUBLIC_BASE_URL` | Public URL of mauxy; embedded in Mautic email template |
| `PUBLIC_WEBSITE_BASE_URL` | Public URL of the website; confirm endpoint redirects here |
| `NEWSLETTER_PENDING_DAYS`, `MAX_PENDING_PER_EMAIL` | Pending retention (7) / DOI mails per address and 24 h (3) |
| `MAIL_MODE` | `smtp` / `console` (dry run) / `memory` (tests) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | mailcow mc1.simplify-erp.de, 587 STARTTLS or 465 SSL |
| `SELFCHECK_ALLOWED_ORIGINS` | Origins allowed on the self-check endpoints (also in `ALLOWED_ORIGINS`) |
| `SELFCHECK_WEBSITE_URL`, `SELFCHECK_NOTIFY_TO` | Confirm redirect base; operator mailbox |
| `SELFCHECK_RATE_LIMIT`, `SELFCHECK_EVENT_RATE_LIMIT` | Per-IP submit limit; global counter limit (no IP) |
| `SELFCHECK_PENDING_DAYS`, `SELFCHECK_RETENTION_DAYS` | Retention 7 / 90 days |
| `MAUTIC_RADAR_TAG`, `MAUTIC_RADAR_SEGMENT_ID` | Migration Radar in Mautic (after DOI only) |
