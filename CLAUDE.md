# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python (FastAPI) proxy microservice that accepts unsubscribe requests from an SPA frontend and adds contacts to Mautic's Do-Not-Contact (DNC) list via the Mautic REST API. Mautic Basic Auth credentials stay server-side and are never exposed to the browser. Contact-specific responses return `{"status": "ok"}` to prevent email enumeration; returns 503 when Mautic is unreachable.

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

- **POST /api/unsubscribe** -- accepts `{"email": "..."}`, looks up the contact in Mautic, adds to DNC list. Returns 503 when Mautic is unreachable (search phase); contact-specific outcomes always return 200. Rate-limited via `slowapi` (default 5/min per IP). Every attempt is logged to SQLite.
- **GET /api/actions** -- admin endpoint to query the action log. Requires `Authorization: Bearer {ADMIN_API_KEY}`. Supports `email`, `result`, `limit`, `offset` query params. Disabled (403) if `ADMIN_API_KEY` is unset.
- **GET /health** -- k8s liveness/readiness probe. Includes Mautic connectivity status in response body (always returns HTTP 200).
- **GET /health/detail** -- richer health endpoint showing degraded/ok status, Mautic detail, and cache age. Always returns HTTP 200.
- CORS is restricted to `ALLOWED_ORIGINS` (must be set via env).

### Persistent storage

Action log uses SQLite via `aiosqlite`, stored at `ACTION_LOG_DB` (default `/data/actions.db`). In k8s, `/data` is backed by a 256Mi `ReadWriteOnce` PVC (`mauxy-data`). Apply `k8s/pvc.yaml` before the deployment.

## Environment Variables

| Variable | Purpose |
|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL |
| `MAUTIC_USERNAME` | Mautic API basic auth user |
| `MAUTIC_PASSWORD` | Mautic API basic auth password |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `RATE_LIMIT` | slowapi rate limit string (e.g. `5/minute`) |
| `ACTION_LOG_DB` | SQLite database path (default `/data/actions.db`) |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` (disabled if unset) |
