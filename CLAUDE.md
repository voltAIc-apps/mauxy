# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**mauxy** -- the single newsletter (un)subscribe authority for all sites (ludo, simplify-erp, re-cloud, ...) and the website's interface to other systems (owner decision 2026-11-08, flywheel SPEC-021). Python (FastAPI). Every newsletter (un)subscribe is gated by (1) a **per-site Bearer key** (`sites` table, seeded from `MAUXY_SITES`: key -> site, segment, quiz topic, policy) and (2) the **bot-defence quiz** (`GET /api/challenge`, signed expiring token, answer verified server-side). Per-site policy (db-patches/0004): `subscribe_quiz` (off = the site relies on mauxy's DOI; unsubscribe ALWAYS needs the quiz), `subscriber_sink` (odoo / mautic / none; NULL = global `SUBSCRIBER_SINK`), `doi_owner` (`mauxy` = mauxy mails the DOI and nothing reaches a sink before the click; `sink` = upstream behaviour for ludo: contact into the Mautic segment at once, Mautic campaign runs the opt-in). Also: Selbst-Check (`selfcheck_api.py`, SPEC-020), every lead CTA (`leads_api.py`: contact, inquiry, webinar, checkout) and the visitor LinkedIn login (`auth_api.py`, sign-in only `openid profile email`, replaces app2li; no publishing scopes or member tokens). After the DOI click, `lead_sync.py` delivers to the sinks (`sinks.py`): `crm.lead` + Email Marketing lists in erp.wapsol (Odoo 18) via `odoo_client.py` -- the ONE sanctioned write path into erp.wapsol, create-only, own bot user, allowlist of (model, method). Mautic = deprecated sink (`mautic_sink.py`), delete with Mautic. Responses are uniform and immediate (no enumeration). Credentials from env only, never logged.

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

Numbered: T-MAIL-0xx (mailer: TLS, MIME, limits, injection, retries), T-NL-0xx (newsletter authority: site key, quiz, per-site policy, ludo upstream flow, db-patches; cases in `tests/cases/newsletter_gate_cases.json`), T-AUTH-0xx (LinkedIn login, LinkedIn mocked with respx, cases in `tests/cases/auth_cases.json`), T-SEC-0xx (security reviews 2026-11-08, incl. timing T-SEC-017; 2026-11-09: confirm GET page + POST click T-SEC-020..038, cases in `tests/cases/confirm_click_cases.json`; atomic caps / throttle race T-SEC-040..045), T-SC-1xx (self-check), T-ODOO-0xx (Odoo client), T-SINK-0xx (sinks), T-LEAD-0xx (CTA leads, outbox; cases in `tests/cases/`). Odoo is ALWAYS the in-memory fake (`tests/fake_odoo.py`, `odoo` fixture); legacy suites run with `LEAD_SINK=none`, `SUBSCRIBER_SINK=mautic`. Local run with the fake: `tests/scripts/run_local.py` (10.0.99.1:8020; also a mocked LinkedIn at `/__mock_linkedin`). Read-only check against the real erp.wapsol (only with the bot credentials): `scripts/odoo_smoke.py`.

## Build & Deploy

`scripts/deploy.py` is the single deploy tool — it reads everything from `.env`
(gitignored; no secrets in the repo) and runs into its own `mauxy` namespace:

```bash
python scripts/deploy.py --dry-run                  # preview manifests
python scripts/deploy.py --build                    # docker login + build + push
python scripts/deploy.py --secret                   # sync mauxy-credentials Secret
python scripts/deploy.py --apply                    # render + kubectl apply
python scripts/deploy.py --build --secret --apply   # full deploy
```

`--build` uses `REGISTRY_URL/USER/PASSWORD`; `--secret` writes every runtime variable
(`SECRET_KEYS` = `KNOWN_VARS` minus deploy/registry: Odoo, SMTP, LinkedIn, `MAUXY_SITES`, `CHALLENGE_SECRET`, …) into the
`mauxy-credentials` Secret that the deployment is `envFrom`. `k8s/secret.yaml.example`
is a documentation template only — the `.example` suffix keeps it out of the `k8s/*.yaml`
glob that `--apply` applies, so it can never clobber the real Secret. The Dockerfile ships the app modules, `vendor/`, `db_migrate.py`, the
`db-patches/` schema patches and `scripts/{deploy,sqlite_db}.py`. The deployment runs
`sqlite_db.py --migrate` as an initContainer to apply pending patches before the app starts.

## Architecture

- `main.py` newsletter authority (below), `/api/actions` (admin), lifespan (schema check, site seed, config load, purge, outbox resume, drain with timeout).
- `selfcheck_api.py` `/api/selfcheck*` (SPEC-020); confirm sends the result mail, then queues Radar + lead + operator mail in the outbox. `vendor/selfcheck/` = rules engine from flywheel (`scripts/sync_selfcheck.py` there; T-SC-150 checks the sha256, never edit).
- `confirm_page.py` two-step DOI for every mail link (security review 2026-11-09, link prefetch): GET validates + renders a page with a POST "Bestätigen" form (strict CSP, no-store, noindex, ids on every element), POST (form `token`, Origin/Referer = mauxy) burns + writes; invalid/used -> one neutral page.
- `leads_api.py` `POST /api/lead` (kinds contact/inquiry/webinar/checkout/selfcheck), `GET+POST /api/lead/confirm`, admin `GET /api/lead/submissions`, `POST /api/lead/sync/{id}/retry`; renders the lead title + plain-text description.
- `lead_sync.py` outbox table `lead_sync` (UNIQUE per submission), retries with backoff via `jobs.py`, marker search against duplicates, failure mail to `LEAD_NOTIFY_TO`, data cleared after delivery.
- `sinks.py` `OdooLeadSink`, `OdooSubscriberSink`, none sinks, factory (`LEAD_SINK`, `subscriber_sink(kind)` per site, fallback `SUBSCRIBER_SINK`). `odoo_client.py` JSON-RPC over verified HTTPS, `ALLOWED` / `WRITE_FIELDS`.
- `auth_api.py` `GET /api/auth/linkedin/start|callback`, `POST /api/auth/session`: return path = same-site path on `AUTH_ALLOWED_ORIGINS`; state signed 10 min, single use, bound by cookie `__Host-mauxy_li`; one-time code (60 s, single use, bound to the tab's `binding`) instead of PII in URLs; tables `auth_state`, `auth_code` (purged every 30 s); audit line without PII. Config problems disable only the login (`/health/detail` `auth_linkedin`).
- `mailer.py` the ONE mail path: `send(to, subject, body, html=, attachments=[Attachment], audience=doi|confirmed|internal, key=, list_unsubscribe=)`; validation, MIME (alternative / related / mixed), policy, outbox `mail_outbox` (patch 0005, idempotent per Message-ID, retries, resume, per-recipient limit), SMTP with mandatory verified TLS. Await only from background jobs. Tests T-MAIL-0xx against aiosmtpd on 10.0.99.1 (`tests/test_mailer.py`). `jobs.py` fire-and-forget tasks (after the response).
- Owner setup of erp.wapsol (bot user, group, rules, lists, UTM): README "erp.wapsol setup".


- Newsletter authority in `main.py`: `GET /api/challenge` (per site key), `POST /api/subscribe` (key -> body model per policy -> honeypot -> quiz unless `subscribe_quiz` off -> mauxy DOI job, or immediate Mautic segment for `doi_owner=sink`), `GET /api/confirm` (read-only confirmation page) + `POST /api/confirm` (the click: atomic claim, sink stored in the pending row), `POST /api/unsubscribe` (key + quiz, the site's sink). Admin CRUD `/api/admin/sites|quiz|mautic-instance` (Bearer `ADMIN_API_KEY`, every write -> `reload_config`). `/health` liveness (always 200), `/ready` readiness (local SQLite only, NOT tied to a sink), `/health/detail` (sinks incl. per-site ones, login, sites, quiz, `mautic_version`).
- Website contract (simplify-erp.de): README "Website contract".

### Mautic prerequisites (DEPRECATED, only for sites on the Mautic sink)

- Custom contact fields `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (datetime), `newsletter_page_url` (`confirmation_token` only for clearing old values).
- Tags `newsletter_confirmed`, `migration_radar_confirmed` (self-check Radar); optional segment id `MAUTIC_RADAR_SEGMENT_ID`.
- `doi_owner=sink` sites (ludo): a DOI campaign on the site's segment (Mautic sends the opt-in). `doi_owner=mauxy` sites on Mautic: NO DOI campaign on their segment (mauxy already confirmed; else a second mail).

### Persistent storage

SQLite via `aiosqlite` at `ACTION_LOG_DB` (default `/data/actions.db`): `action_log` (`action`, `site`, `result`), `sites` (key registry + policy; keys plaintext, deliberate upstream decision: TLS + PVC RBAC are the controls), `quiz_questions`, `mautic_instance`, `schema_migrations`, `newsletter_pending` (deleted after `NEWSLETTER_PENDING_DAYS`), `selfcheck_submission` (pending 7 d / confirmed 90 d), `selfcheck_counter` (day, event, lang, count only), `lead_submission` (pending 7 d / confirmed 30 d), `lead_sync` (outbox; deleted 30 d after done / failed), `auth_state`, `auth_code`. One replica only, deployment strategy `Recreate` (SQLite on ONE RWO Longhorn PVC + in-process outbox). In k8s, `/data` is a 256Mi `ReadWriteOnce` PVC (`mauxy-data`); apply `k8s/pvc.yaml` before the deployment.

### Schema as a versioned artifact (db-patches/)

The schema is **not** created at runtime. It is owned by integer-numbered SQL patches in `db-patches/` (`NNNN_name.sql`): 0001 init, 0002 quiz seed, 0003 website consent tables (DOI pending, self-check, leads, outbox, login), 0004 per-site policy. `db_migrate.py` is the shared runner; `schema_migrations` records what's applied.

- **Deployers apply** patches: `python scripts/sqlite_db.py --migrate` (k8s initContainer). First boot creates the schema this way.
- **App startup only checks**: DB behind the shipped patches -> refuses to start (`MIGRATE_STRICT=false` warns instead). Exception: `ACTION_LOG_DB=:memory:` (tests) applies the patches in-process.
- **Dev workflow**: `--generate <name>` writes the next patch from a schema diff; `--new <name>` scaffolds one (DML); `--status`; `--dump-quiz`. Never edit a deployed patch, add a new one.
- The local `data/actions.db` is git-ignored (`*.db`); rebuild with `--migrate`, or pull the live one with `--pull`.

## Environment Variables

| Variable | Purpose |
|---|---|
| `LEAD_SINK`, `SUBSCRIBER_SINK` | `odoo` (default) / `none`; subscriber also `mautic` (deprecated); `SUBSCRIBER_SINK` = fallback for sites without own sink |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`, `ODOO_TIMEOUT` | erp.wapsol, mauxy's own bot user, https only |
| `ODOO_LEAD_USER_ID`, `ODOO_LEAD_TEAM_ID`, `ODOO_LEAD_TAG_IDS` | salesperson (+ follower), optional team / tags |
| `ODOO_UTM_SOURCE`, `ODOO_UTM_MEDIUM` | UTM names (default `simplify-erp.de`, `Website`); campaign = kind |
| `ODOO_LIST_NEWSLETTER_ID`, `ODOO_LIST_RADAR_ID` | Email Marketing lists |
| `LEAD_ALLOWED_ORIGINS`, `LEAD_WEBSITE_URL`, `LEAD_RATE_LIMIT`, `LEAD_NOTIFY_TO`, `LEAD_PENDING_DAYS`, `LEAD_RETENTION_DAYS` | CTA leads |
| `LEAD_SYNC_MAX_ATTEMPTS`, `LEAD_SYNC_BACKOFF_SECONDS`, `SHUTDOWN_DRAIN_SECONDS` | outbox retries / shutdown |
| `MAUTIC_BASE_URL`, `MAUTIC_USERNAME`, `MAUTIC_PASSWORD` | DEPRECATED Mautic sink (ludo: still in use) |
| `MAUTIC_VERSION` | Target Mautic version (manual), seeds `mautic_instance`; `/health/detail` |
| `MIGRATE_STRICT` | Refuse to start if the DB is behind `db-patches/` (default true) |
| `MAUXY_SITES` | JSON array `{key, site, segment, topic, subscribe_quiz?, subscriber_sink?, doi_owner?}` -- **seed-only** for an empty `sites` table |
| `CHALLENGE_SECRET`, `CHALLENGE_TTL` | HMAC secret of the quiz tokens (quiz + newsletter (un)subscribe off if unset); lifetime 600 s |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `RATE_LIMIT` | slowapi rate limit for unsubscribe (default `5/minute`) |
| `SUBSCRIBE_RATE_LIMIT`, `CHALLENGE_RATE_LIMIT` | subscribe (default `60/minute`, upstream) / challenge (`30/minute`) per IP |
| `CONFIRM_RATE_LIMIT` | slowapi rate limit for every confirm GET / POST (default `30/minute`) |
| `ACTION_LOG_DB` | SQLite database path (default `/data/actions.db`) |
| `ADMIN_API_KEY` | Bearer token for `/api/actions`, `/api/admin/*`, lead admin (disabled if unset) |
| `CONFIRM_TOKEN_SECRET` | `itsdangerous` signing key for double opt-in tokens |
| `CONFIRM_TOKEN_MAX_AGE` | Token lifetime in seconds (default 86400) |
| `PUBLIC_BASE_URL` | Public URL of mauxy (prod canonical `https://mauxy.engage.wapsol.de`); base of every DOI confirm link and the LinkedIn redirect URI |
| `PUBLIC_WEBSITE_BASE_URL` | Public URL of the website; confirm endpoint redirects here |
| `NEWSLETTER_PENDING_DAYS`, `MAX_PENDING_PER_EMAIL` | Pending retention (7) / DOI mails per address and 24 h (3) |
| `MAIL_MODE` | `smtp` (default) / `console` (dev only, prints bodies) / `memory` (tests) |
| `SMTP_HOST`, `SMTP_SECURITY`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_REPLY_TO`, `SMTP_TIMEOUT` | mailcow `mail.mc1.simplify-erp.de`; `starttls` 587 (default) or `ssl` 465; TLS mandatory, verified, fail closed |
| `SMTP_CA_FILE` | extra trust anchor for local mock servers / tests only |
| `MAIL_MAX_BYTES`, `MAIL_ATTACHMENT_TYPES`, `MAIL_RECIPIENT_HOURLY_LIMIT`, `MAIL_INTERNAL_RECIPIENTS`, `MAIL_MAX_ATTEMPTS`, `MAIL_BACKOFF_SECONDS`, `MAIL_RETENTION_DAYS` | mail policy: 10 MiB, pdf/png/jpeg/webp, 10 per recipient and hour, retries 4 / 30 s, outbox 7 d |
| `SELFCHECK_ALLOWED_ORIGINS` | Origins allowed on the self-check endpoints (also in `ALLOWED_ORIGINS`) |
| `SELFCHECK_WEBSITE_URL`, `SELFCHECK_NOTIFY_TO` | Confirm redirect base; operator mailbox |
| `SELFCHECK_RATE_LIMIT`, `SELFCHECK_EVENT_RATE_LIMIT` | Per-IP submit limit; global counter limit (no IP) |
| `SELFCHECK_PENDING_DAYS`, `SELFCHECK_RETENTION_DAYS` | Retention 7 / 90 days |
| `MAUTIC_RADAR_TAG`, `MAUTIC_RADAR_SEGMENT_ID` | DEPRECATED: Migration Radar in Mautic |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI` | LinkedIn login (secret); redirect default `PUBLIC_BASE_URL/api/auth/linkedin/callback` |
| `AUTH_ALLOWED_ORIGINS`, `AUTH_TOKEN_SECRET`, `AUTH_SESSION_TTL`, `AUTH_RATE_LIMIT`, `AUTH_SESSION_RATE_LIMIT` | login return sites, signing key (>= 32), session 3600 s, limits |
| `LINKEDIN_AUTH_URL`, `LINKEDIN_TOKEN_URL`, `LINKEDIN_USERINFO_URL` | only for a local mock on 10.0.99.1 (https enforced otherwise) |
