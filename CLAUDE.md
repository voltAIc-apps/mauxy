# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**mauxy** -- centralized newsletter subscription + unsubscribe proxy for Mautic. Python (FastAPI) microservice that accepts subscribe/unsubscribe requests from SPA frontends and manages contacts via the Mautic REST API. Mautic Basic Auth credentials stay server-side and are never exposed to the browser. Contact-specific responses return `{"status": "ok"}` to prevent email enumeration; returns 503 when Mautic is unreachable. Multi-site config via `sites.yaml`, GDPR double opt-in via SMTP confirmation emails.

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (set env vars first, see .env.example)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

No tests or linter are configured in this repo.

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

All application logic is in `main.py` (single-file service):

- **POST /api/subscribe** -- JSON signup payload from the website (see `website/src/config/api.ts`). Honeypot (`company_website`) short-circuits silently. Searches Mautic; returns `already_subscribed` when the contact is already tagged `newsletter_confirmed`, `already_unsubscribed_no_email_sent` on DNC, otherwise signs a 24h token (`itsdangerous`), upserts the contact with `confirmation_token` + metadata + tag `newsletter_pending`, and triggers Mautic's transactional send of `MAUTIC_CONFIRM_EMAIL_ID_{locale}`. Returns 503 on any Mautic failure. Rate-limited by `RATE_LIMIT`.
- **GET /api/confirm?token=...** -- double opt-in landing. Validates token (`itsdangerous` max_age = `CONFIRM_TOKEN_MAX_AGE`), flips the Mautic contact from `newsletter_pending` to `newsletter_confirmed`, clears the token field, and 302-redirects to `${PUBLIC_WEBSITE_BASE_URL}/{locale}/newsletter/confirmed`. Failures redirect to `.../newsletter/confirm-error?reason=...`. Rate-limited by `CONFIRM_RATE_LIMIT`.
- **POST /api/unsubscribe** -- accepts `{"email": "..."}`, looks up the contact in Mautic, adds to DNC list. Returns 503 when Mautic is unreachable (search phase); contact-specific outcomes always return 200. Rate-limited via `slowapi` (default 5/min per IP). Every attempt is logged to SQLite.
- **GET /api/actions** -- admin endpoint to query the action log. Requires `Authorization: Bearer {ADMIN_API_KEY}`. Supports `email`, `result`, `limit`, `offset` query params. Disabled (403) if `ADMIN_API_KEY` is unset.
- **GET /health** -- k8s liveness/readiness probe. Includes Mautic connectivity status in response body (always returns HTTP 200).
- **GET /health/detail** -- richer health endpoint showing degraded/ok status, Mautic detail, and cache age. Always returns HTTP 200.
- CORS is restricted to `ALLOWED_ORIGINS` (must be set via env).

### Newsletter prerequisites in Mautic (one-time)

Subscribe + confirm assume these exist in Mautic (not created by the code):

- Custom contact fields `confirmation_token`, `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (datetime), `newsletter_page_url`.
- Tags `newsletter_pending` and `newsletter_confirmed`.
- Two email templates (EN / DE). Their confirm link must be `${PUBLIC_BASE_URL}/api/confirm?token={contactfield=confirmation_token}`. Record the numeric IDs into `MAUTIC_CONFIRM_EMAIL_ID_EN` / `MAUTIC_CONFIRM_EMAIL_ID_DE`.

### Persistent storage

Action log uses SQLite via `aiosqlite`, stored at `ACTION_LOG_DB` (default `/data/actions.db`). In k8s, `/data` is backed by a 256Mi `ReadWriteOnce` PVC (`mauxy-data`). Apply `k8s/pvc.yaml` before the deployment.

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
| `MAUTIC_CONFIRM_EMAIL_ID_EN` | Mautic email template ID (English) |
| `MAUTIC_CONFIRM_EMAIL_ID_DE` | Mautic email template ID (German) |
