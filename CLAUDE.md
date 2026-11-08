# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**mauxy** -- the website's interface to other systems (owner decision 2026-11-08, flywheel SPEC-021). Python (FastAPI). Public consent service of simplify-erp.de: newsletter + Migration Radar double opt-in, Selbst-Check (`selfcheck_api.py`, SPEC-020), every lead CTA (`leads_api.py`: contact, inquiry, webinar, checkout) and the visitor LinkedIn login (`auth_api.py`, sign-in only `openid profile email`, replaces app2li; LinkedIn posting is NOT mauxy's job, no publishing scopes or member tokens here). After the DOI click, `lead_sync.py` delivers to the sinks (`sinks.py`): `crm.lead` + Email Marketing lists in erp.wapsol (Odoo 18) via `odoo_client.py` -- the ONE sanctioned write path into erp.wapsol, create-only, own bot user, allowlist of (model, method). Mautic = deprecated `SUBSCRIBER_SINK=mautic` (`mautic_sink.py`), delete with Mautic. Responses are uniform and immediate (no enumeration); nothing is written to any sink before the confirmation. Credentials from env only, never logged.

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

Numbered: T-AUTH-0xx (LinkedIn login, LinkedIn mocked with respx, cases in `tests/cases/auth_cases.json`), T-SEC-0xx (security reviews 2026-11-08, incl. timing T-SEC-017), T-SC-1xx (self-check), T-ODOO-0xx (Odoo client), T-SINK-0xx (sinks), T-LEAD-0xx (CTA leads, outbox; cases in `tests/cases/`). Odoo is ALWAYS the in-memory fake (`tests/fake_odoo.py`, `odoo` fixture); legacy suites run with `LEAD_SINK=none`, `SUBSCRIBER_SINK=mautic`. Local run with the fake: `tests/scripts/run_local.py` (10.0.99.1:8020; also a mocked LinkedIn at `/__mock_linkedin`). Read-only check against the real erp.wapsol (only with the bot credentials): `scripts/odoo_smoke.py`.

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

- `main.py` newsletter (`/api/subscribe`, `/api/confirm`, `/api/unsubscribe` via the subscriber sink), `/health` (cheap), `/health/detail` (sinks), `/api/actions` (admin), lifespan (DB, purge, outbox resume, drain with timeout).
- `selfcheck_api.py` `/api/selfcheck*` (SPEC-020); confirm sends the result mail, then queues Radar + lead + operator mail in the outbox. `vendor/selfcheck/` = rules engine from flywheel (`scripts/sync_selfcheck.py` there; T-SC-150 checks the sha256, never edit).
- `leads_api.py` `POST /api/lead` (kinds contact/inquiry/webinar/checkout/selfcheck), `GET /api/lead/confirm`, admin `GET /api/lead/submissions`, `POST /api/lead/sync/{id}/retry`; renders the lead title + plain-text description.
- `lead_sync.py` outbox table `lead_sync` (UNIQUE per submission), retries with backoff via `jobs.py`, marker search against duplicates, failure mail to `LEAD_NOTIFY_TO`, data cleared after delivery.
- `sinks.py` `OdooLeadSink`, `OdooSubscriberSink`, none sinks, factory (`LEAD_SINK`, `SUBSCRIBER_SINK`). `odoo_client.py` JSON-RPC over verified HTTPS, `ALLOWED` / `WRITE_FIELDS`.
- `auth_api.py` `GET /api/auth/linkedin/start|callback`, `POST /api/auth/session`: return path = same-site path on `AUTH_ALLOWED_ORIGINS`; state signed 10 min, single use, bound by cookie `__Host-mauxy_li`; one-time code (60 s, single use, bound to the tab's `binding`) instead of PII in URLs; tables `auth_state`, `auth_code` (purged every 30 s); audit line without PII. Config problems disable only the login (`/health/detail` `auth_linkedin`).
- `mailer.py` SMTP / console / memory. `jobs.py` fire-and-forget tasks (after the response).
- Owner setup of erp.wapsol (bot user, group, rules, lists, UTM): README "erp.wapsol setup".

### Mautic prerequisites (DEPRECATED, only with SUBSCRIBER_SINK=mautic)

- Custom contact fields `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (datetime), `newsletter_page_url` (`confirmation_token` only for clearing old values).
- Tags `newsletter_confirmed`, `migration_radar_confirmed` (self-check Radar); optional segment id `MAUTIC_RADAR_SEGMENT_ID`.
- No Mautic e-mail templates needed any more (DOI mails go out via SMTP).

### Persistent storage

SQLite via `aiosqlite` at `ACTION_LOG_DB` (default `/data/actions.db`): `action_log`, `newsletter_pending` (deleted after `NEWSLETTER_PENDING_DAYS`), `selfcheck_submission` (pending 7 d / confirmed 90 d), `selfcheck_counter` (day, event, lang, count only), `lead_submission` (pending 7 d / confirmed 30 d), `lead_sync` (outbox; deleted 30 d after done / failed). One replica only (SQLite RWO + in-process outbox). In k8s, `/data` is backed by a 256Mi `ReadWriteOnce` PVC (`mauxy-data`). Apply `k8s/pvc.yaml` before the deployment.

## Environment Variables

| Variable | Purpose |
|---|---|
| `LEAD_SINK`, `SUBSCRIBER_SINK` | `odoo` (default) / `none`; subscriber also `mautic` (deprecated) |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`, `ODOO_TIMEOUT` | erp.wapsol, mauxy's own bot user, https only |
| `ODOO_LEAD_USER_ID`, `ODOO_LEAD_TEAM_ID`, `ODOO_LEAD_TAG_IDS` | salesperson (+ follower), optional team / tags |
| `ODOO_UTM_SOURCE`, `ODOO_UTM_MEDIUM` | UTM names (default `simplify-erp.de`, `Website`); campaign = kind |
| `ODOO_LIST_NEWSLETTER_ID`, `ODOO_LIST_RADAR_ID` | Email Marketing lists |
| `LEAD_ALLOWED_ORIGINS`, `LEAD_WEBSITE_URL`, `LEAD_RATE_LIMIT`, `LEAD_NOTIFY_TO`, `LEAD_PENDING_DAYS`, `LEAD_RETENTION_DAYS` | CTA leads |
| `LEAD_SYNC_MAX_ATTEMPTS`, `LEAD_SYNC_BACKOFF_SECONDS`, `SHUTDOWN_DRAIN_SECONDS` | outbox retries / shutdown |
| `MAUTIC_BASE_URL`, `MAUTIC_USERNAME`, `MAUTIC_PASSWORD` | DEPRECATED Mautic sink |
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
| `MAUTIC_RADAR_TAG`, `MAUTIC_RADAR_SEGMENT_ID` | DEPRECATED: Migration Radar in Mautic |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI` | LinkedIn login (secret); redirect default `PUBLIC_BASE_URL/api/auth/linkedin/callback` |
| `AUTH_ALLOWED_ORIGINS`, `AUTH_TOKEN_SECRET`, `AUTH_SESSION_TTL`, `AUTH_RATE_LIMIT`, `AUTH_SESSION_RATE_LIMIT` | login return sites, signing key (>= 32), session 3600 s, limits |
| `LINKEDIN_AUTH_URL`, `LINKEDIN_TOKEN_URL`, `LINKEDIN_USERINFO_URL` | only for a local mock on 10.0.99.1 (https enforced otherwise) |
