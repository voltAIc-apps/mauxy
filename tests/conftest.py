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

# Make the repo root importable so `import main` finds mauxy/main.py
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main as mauxy_main  # noqa: E402
import mailer  # noqa: E402


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
