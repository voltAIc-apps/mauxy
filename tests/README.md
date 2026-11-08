# mauxy tests

Tests for every endpoint. Mautic is mocked via `respx`; Odoo is ALWAYS the in-memory fake
`tests/fake_odoo.py` (`odoo` fixture) -- no test talks to the real erp.wapsol.

| Prefix | Suite |
|---|---|
| T-SEC-0xx | newsletter DOI security (test_subscribe, test_confirm, test_timing) |
| T-SC-1xx | Selbst-Check (test_selfcheck, cases/selfcheck_api_cases.json) |
| T-ODOO-0xx | Odoo client: https, allowlist, errors, no key leaks (test_odoo_client) |
| T-SINK-0xx | Odoo sinks: partner match, lead vals, follower, lists, opt-out (test_sinks) |
| T-AUTH-0xx | LinkedIn login: return-path open redirects, Referer origin, config guard, state replay / cookie / expiry, code replay / binding / origin / 60 s, error passthrough, no PII (test_auth, cases/auth_cases.json; LinkedIn mocked with respx) |
| T-LEAD-0xx | CTA leads + outbox: validation (cases/lead_api_cases.json), DOI, retries, idempotency (test_leads) |

## Run

```bash
cd mauxy
python -m venv tests/venv
source tests/venv/bin/activate
pip install -r requirements.txt -r tests/requirements.txt
PYTHONPATH=. pytest tests/ -v
```

Env vars are set by `tests/conftest.py` before `main` is imported, so you do
not need a real `.env`. Tests use an in-memory SQLite action log and point
Mautic at `https://mautic.test` which `respx` intercepts.
