"""
Shared fixtures for mauxy tests.

Env vars are set BEFORE importing `main` because mauxy reads configuration at
module load time. Keep this file's top-level imports minimal.
"""
import os
import sys
from pathlib import Path

# -- Env vars must be set before `main` is imported --------------------------
os.environ.setdefault("MAUTIC_BASE_URL", "https://mautic.test")
os.environ.setdefault("MAUTIC_USERNAME", "u")
os.environ.setdefault("MAUTIC_PASSWORD", "p")
os.environ.setdefault("ALLOWED_ORIGINS", "http://testserver,https://site.test")
os.environ.setdefault("RATE_LIMIT", "10000/minute")
os.environ.setdefault("CONFIRM_RATE_LIMIT", "10000/minute")
os.environ.setdefault("ACTION_LOG_DB", ":memory:")
os.environ.setdefault("ADMIN_API_KEY", "admin-test-key")
os.environ.setdefault("CONFIRM_TOKEN_SECRET", "test-secret-for-pytest-only")
os.environ.setdefault("CONFIRM_TOKEN_MAX_AGE", "86400")
os.environ.setdefault("PUBLIC_BASE_URL", "https://mauxy.test")
os.environ.setdefault("PUBLIC_WEBSITE_BASE_URL", "https://site.test")
# mails are captured in mailer.OUTBOX, never sent
os.environ.setdefault("MAIL_MODE", "memory")
os.environ.setdefault("SMTP_FROM", "Test <noreply@site.test>")
# self-check (SPEC-020)
os.environ.setdefault("SELFCHECK_ALLOWED_ORIGINS", "https://site.test")
os.environ.setdefault("SELFCHECK_NOTIFY_TO", "owner@site.test")
os.environ.setdefault("SELFCHECK_WEBSITE_URL", "https://site.test")
os.environ.setdefault("SELFCHECK_RATE_LIMIT", "10000/minute")
os.environ.setdefault("SELFCHECK_EVENT_RATE_LIMIT", "10000/minute")
# CTA leads + sinks (SPEC-021). The legacy suites (T-SEC, T-SC) run against the deprecated Mautic
# subscriber sink and without a CRM (LEAD_SINK=none); the Odoo suites switch per test (odoo fixture).
os.environ.setdefault("LEAD_ALLOWED_ORIGINS", "https://site.test")
os.environ.setdefault("LEAD_RATE_LIMIT", "10000/minute")
os.environ.setdefault("LEAD_NOTIFY_TO", "owner@site.test")
os.environ.setdefault("LEAD_SINK", "none")
os.environ.setdefault("SUBSCRIBER_SINK", "mautic")
os.environ.setdefault("LEAD_SYNC_BACKOFF_SECONDS", "0")
os.environ.setdefault("LEAD_SYNC_MAX_ATTEMPTS", "3")
# fake Odoo (tests/fake_odoo.py) -- never the real erp.wapsol
os.environ.setdefault("ODOO_URL", "https://odoo.test")
os.environ.setdefault("ODOO_DB", "test-db")
os.environ.setdefault("ODOO_USER", "website-bot")
os.environ.setdefault("ODOO_API_KEY", "test-key-not-real")
os.environ.setdefault("ODOO_LEAD_USER_ID", "7")
os.environ.setdefault("ODOO_LIST_NEWSLETTER_ID", "1")
os.environ.setdefault("ODOO_LIST_RADAR_ID", "2")

# LinkedIn login (auth_api.py): fake client, LinkedIn itself is mocked with respx (tests/test_auth.py).
# Redirect URI defaults to PUBLIC_BASE_URL + /api/auth/linkedin/callback (https://mauxy.test/...).
os.environ.setdefault("LINKEDIN_CLIENT_ID", "test-client-id")
os.environ.setdefault("LINKEDIN_CLIENT_SECRET", "test-client-secret-not-real")
os.environ.setdefault("AUTH_ALLOWED_ORIGINS", "https://site.test,https://www.site.test")
os.environ.setdefault("AUTH_TOKEN_SECRET", "auth-test-secret-for-pytest-only-0123456789")
os.environ.setdefault("AUTH_RATE_LIMIT", "10000/minute")
os.environ.setdefault("AUTH_SESSION_RATE_LIMIT", "10000/minute")

# Make the repo root importable so `import main` finds mauxy/main.py
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main as mauxy_main  # noqa: E402
import jobs  # noqa: E402
import mailer  # noqa: E402
import sinks  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_sinks():
    """Sinks are cached per process; every test starts from the env defaults above."""
    sinks.reset()
    yield
    sinks.reset()


@pytest.fixture
def app():
    return mauxy_main.app


@pytest.fixture(autouse=True)
def outbox():
    """Captured mails of the test (MAIL_MODE=memory)."""
    mailer.OUTBOX.clear()
    yield mailer.OUTBOX
    mailer.OUTBOX.clear()


@pytest.fixture
def client(app):
    # `with TestClient(app)` triggers lifespan so the SQLite action log opens.
    with TestClient(app) as c:
        # Contact-dependent work runs as fire-and-forget jobs after the response (jobs.py).
        # Tests see its effects by draining the jobs after every request; the timing tests
        # use c.request_nodrain to observe the response before any job ran.
        orig = c.request

        def request_and_drain(*args, **kwargs):
            resp = orig(*args, **kwargs)
            c.portal.call(jobs.drain)
            return resp

        c.request_nodrain = orig
        c.request = request_and_drain
        yield c
    # slowapi keeps in-memory counters; reset so other tests start fresh.
    try:
        mauxy_main.limiter.reset()
    except Exception:
        pass


@pytest.fixture
def token_signer():
    """Expose the module's signer for tests that need to mint / tamper with tokens."""
    return mauxy_main._token_signer


@pytest.fixture
def odoo(monkeypatch):
    """Fake erp.wapsol (tests/fake_odoo.py) wired in as transport; Odoo sinks switched on."""
    import httpx
    import odoo_client
    from tests.fake_odoo import FakeOdoo

    fake = FakeOdoo()
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("LEAD_SINK", "odoo")
    monkeypatch.setenv("SUBSCRIBER_SINK", "odoo")
    sinks.reset()
    yield fake
    sinks.reset()


def drain(client):
    """Run every pending job (DOI mails, lead_sync deliveries incl. retries)."""
    client.portal.call(jobs.drain)
