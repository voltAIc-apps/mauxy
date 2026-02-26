# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python (FastAPI) proxy microservice that accepts unsubscribe requests from an SPA frontend and adds contacts to Mautic's Do-Not-Contact (DNC) list via the Mautic REST API. Mautic Basic Auth credentials stay server-side and are never exposed to the browser. All responses return `{"status": "ok"}` to prevent email enumeration.

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (set env vars first, see .env.example)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

No tests or linter are configured in this repo.

## Build & Deploy

Container image: `crepo.re-cloud.io/re-cloud/mautic-unsubscribe-proxy`

```bash
docker build -t crepo.re-cloud.io/re-cloud/mautic-unsubscribe-proxy:latest .
docker push crepo.re-cloud.io/re-cloud/mautic-unsubscribe-proxy:latest
```

Deployed to Kubernetes namespace `simplify-web` with manifests in `k8s/`. The deployment pulls the image using `harbor-re-cloud` pull secret. Credentials are stored in a k8s Secret (`mautic-unsubscribe-credentials`), not committed — create via `kubectl create secret generic` (see `k8s/secret.yaml` for the template).

Public endpoint: `https://unsubscribe.engage.wapsol.de`

## Architecture

All application logic is in `main.py` (single-file service):

- **POST /api/unsubscribe** — accepts `{"email": "..."}`, looks up the contact in Mautic, adds to DNC list. Rate-limited via `slowapi` (default 5/min per IP).
- **GET /health** — k8s liveness/readiness probe.
- CORS is restricted to `ALLOWED_ORIGINS` (defaults to simplify-erp.de).

## Environment Variables

| Variable | Purpose |
|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL |
| `MAUTIC_USERNAME` | Mautic API basic auth user |
| `MAUTIC_PASSWORD` | Mautic API basic auth password |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `RATE_LIMIT` | slowapi rate limit string (e.g. `5/minute`) |
