# mauxy tests

Tests for every endpoint. Mautic is mocked via `respx`; Odoo is ALWAYS the in-memory fake
`tests/fake_odoo.py` (`odoo` fixture) -- no test talks to the real erp.wapsol.

| Prefix | Suite |
|---|---|
| T-MAIL-0xx | mailer: real SMTP server (aiosmtpd) on 10.0.99.1 with a test CA (trustme): STARTTLS / implicit TLS, refusal without STARTTLS, untrusted cert, hostname mismatch, MIME (alternative / related / mixed, umlauts, RFC 2231), size / type / magic limits, header injection, audience policy, retries, idempotency, per-recipient limit, List-Unsubscribe, resume, log hygiene (test_mailer) |
| T-NL-0xx | newsletter authority: site key + quiz gate matrix (cases/newsletter_gate_cases.json), per-site policy (quiz off, pinned sink, doi_owner), website contract, ludo upstream flow, admin CRUD, db-patches upgrade / strict start, k8s Recreate (test_newsletter_gate) |
| T-SEC-0xx | newsletter DOI security (test_subscribe, test_confirm, test_timing) |
| T-SC-1xx | Selbst-Check (test_selfcheck, cases/selfcheck_api_cases.json) |
| T-ODOO-0xx | Odoo client: https, allowlist, errors, no key leaks (test_odoo_client) |
| T-SINK-0xx | Odoo sinks: partner match, lead vals, follower, lists, opt-out (test_sinks) |
| T-AUTH-0xx | LinkedIn login: email only when email_verified is exactly true (058, 089-093), return-path open redirects, Referer origin, config guard, state replay / cookie / expiry, code replay / binding / origin / 60 s, error passthrough, no PII (test_auth, cases/auth_cases.json; LinkedIn mocked with respx) |
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
not need a real `.env`. Tests use an in-memory SQLite DB (`:memory:`, the db-patches are applied
in-process) and point Mautic at `https://mautic.test` which `respx` intercepts.

Test sites (conftest): `simplify-erp` (quiz on, global sink), `simplify-web` (the website contract:
quiz off, Odoo), `ludo` (upstream: Mautic, `doi_owner=sink`), `re-cloud` (Mautic + segment, mauxy DOI).
The `client` fixture adds the `simplify-erp` key and a solved challenge to `/api/subscribe` and
`/api/unsubscribe` calls that do not set them; gate tests use `client.request_raw`.
