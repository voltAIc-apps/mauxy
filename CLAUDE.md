# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**mauxy** -- the single newsletter (un)subscribe authority in front of Mautic. Python (FastAPI) microservice that accepts subscribe/unsubscribe requests and manages contacts/segments/DNC via the Mautic REST API. Mautic Basic Auth credentials stay server-side, never exposed to the browser. Every (un)subscribe is gated by (1) a **per-site Bearer key** (`MAUXY_SITES` registry maps key → site, Mautic segment, quiz topic) and (2) a **centralized bot-defence quiz** (`GET /api/challenge` issues a signed, expiring question per the site's topic; the answer is verified server-side). Unsubscribe stays enumeration-safe (`{"status":"ok"}` regardless); 503 when Mautic is unreachable. GDPR double-opt-in is owned by Mautic (campaign on the segment).

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (set env vars first, see .env.example)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

No tests or linter are configured in this repo.

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

`--build` uses `REGISTRY_URL/USER/PASSWORD`; `--secret` writes the runtime config
(Mautic creds, `ALLOWED_ORIGINS`, `MAUXY_SITES`, `CHALLENGE_SECRET`, …) into the
`mauxy-credentials` Secret that the deployment is `envFrom`. `k8s/secret.yaml` is a
documentation template only. The Dockerfile ships `main.py`, `db_migrate.py`, the
`db-patches/` schema patches and `scripts/{deploy,sqlite_db}.py`. The deployment runs
`sqlite_db.py --migrate` as an initContainer to apply pending patches before the app starts.

## Architecture

All application logic is in `main.py` (single-file service):

- **GET /api/challenge** -- per-site-keyed; returns a multiple-choice question for the site's topic (from the SQLite quiz bank) + a signed HMAC token. The correct answer never leaves the server.
- **POST /api/subscribe** -- per-site-keyed + quiz-gated. Find/create the contact, add it to the site's segment (creating it by alias if needed). Returns `pending_confirmation` (Mautic runs the opt-in); `409 quiz_failed` / `401` / `502` / `503` otherwise. Honeypot `company_website` drops bots.
- **POST /api/unsubscribe** -- per-site-keyed + quiz-gated. Adds the contact to the DNC list. Contact-specific outcomes always 200 (no enumeration); 503 when Mautic is unreachable.
- **GET /api/actions** -- admin endpoint to query the action log. Requires `Authorization: Bearer {ADMIN_API_KEY}`. Rows carry `action` + `site`. Supports `email`, `result`, `limit`, `offset`. Disabled (403) if `ADMIN_API_KEY` unset.
- **/api/admin/sites, /api/admin/quiz, /api/admin/mautic-instance** -- admin CRUD (Bearer `ADMIN_API_KEY`) over the SQLite-backed site registry, quiz bank and target Mautic version. Every write reloads the in-memory caches (`reload_config`), so changes are live without a restart. See `API.md`.
- **GET /health** -- k8s **liveness** probe. Mautic-independent (always HTTP 200, body `{"status":"ok"}`): only confirms the app/event loop is alive, so a Mautic outage never restarts healthy pods.
- **GET /ready** -- k8s **readiness** probe. Reflects cached Mautic connectivity (no live call — the cache is refreshed by a background task); 200 when reachable, 503 when not, so a pod whose Mautic is down is pulled from the Service.
- **GET /health/detail** -- richer health endpoint for operators showing degraded/ok status, Mautic detail, `mautic_version`, and cache age (reads the same background-refreshed cache). Always returns HTTP 200.
- CORS is restricted to `ALLOWED_ORIGINS` (must be set via env).

### Persistent storage

SQLite via `aiosqlite`, stored at `ACTION_LOG_DB` (default `/data/actions.db`). In k8s, `/data` is backed by a 256Mi `ReadWriteOnce` PVC (`mauxy-data`). Apply `k8s/pvc.yaml` before the deployment. Tables:

- `action_log` -- every (un)subscribe attempt (`action`, `site`, `result`, …).
- `sites` -- per-site Bearer key registry (`key`, `site`, `segment`, `topic`). **Source of truth at runtime.** Keys stored **plaintext** (deliberate decision; TLS + PVC RBAC are the controls — not hashed). Seeded from `MAUXY_SITES` on first boot (secret keys — kept out of git).
- `quiz_questions` -- bot-defence bank (`topic`, `qid`, `question`, `choices_json`, `answer`). Seeded by the SQL patch `db-patches/0002_seed_quiz.sql`.
- `mautic_instance` -- single row holding the target Mautic `base_url` + `version` (manual; `MAUTIC_VERSION` env or admin endpoint). `get_mautic_version()` is the resolver for future version-specific Mautic calls.
- `schema_migrations` -- which numbered patches have been applied (managed by `db_migrate.py`).

Onboard a site / edit a quiz at runtime via the admin CRUD — no redeploy.

### Schema as a versioned artifact (db-patches/)

The schema is **not** created at runtime. It is owned by integer-numbered SQL patches in `db-patches/` (`NNNN_name.sql`), committed to git. `db_migrate.py` is the shared runner (used by `main.py` startup + `scripts/sqlite_db.py`); `schema_migrations` records what's applied so patches run once, in order, idempotently.

- **Deployers apply** patches: `python scripts/sqlite_db.py --migrate` (k8s does this in an initContainer before the app container). First boot creates the schema this way.
- **App startup only checks**: if the DB is behind the shipped patches it refuses to start (set `MIGRATE_STRICT=false` to warn instead).
- **Dev workflow**: change the dev DB schema, then `python scripts/sqlite_db.py --generate <name>` auto-writes the next patch from a schema diff (additive changes auto; drops/retypes flagged for manual edit). `--new <name>` scaffolds an empty patch (for content/DML, e.g. new quiz questions); `--status` shows current vs shipped; `--dump-quiz` emits the quiz as `INSERT OR IGNORE` SQL.
- The local `data/actions.db` is git-ignored (`*.db`); rebuild it from patches with `--migrate`, or pull the live one with `--pull`.

## Environment Variables

| Variable | Purpose |
|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL |
| `MAUTIC_USERNAME` | Mautic API basic auth user |
| `MAUTIC_PASSWORD` | Mautic API basic auth password |
| `MAUTIC_VERSION` | Target Mautic version (manual). Seeds the `mautic_instance` row; surfaced in `/health/detail`. Empty = unknown |
| `MIGRATE_STRICT` | Refuse to start if the DB is behind `db-patches/` (default true; false = warn only) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `MAUXY_SITES` | JSON array of `{key, site, segment, topic}` — **seed-only** for the `sites` table (runtime source: SQLite) |
| `CHALLENGE_SECRET` | HMAC secret for bot-defence challenge tokens (quiz disabled if unset) |
| `CHALLENGE_TTL` | Challenge token lifetime in seconds (default 600) |
| `RATE_LIMIT` | slowapi rate limit string (e.g. `5/minute`) |
| `ACTION_LOG_DB` | SQLite database path (default `/data/actions.db`) |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` + `/api/admin/*` (disabled if unset) |
