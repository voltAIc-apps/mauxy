"""
Shared fixtures for mauxy tests.

Env vars are set BEFORE importing `main` because mauxy reads configuration at
module load time. Keep this file's top-level imports minimal.
"""
import html
import json
import os
import re
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

# Newsletter authority gates (per-site key + bot-defence quiz). One test site on the seeded quiz
# topic simplify-erp (db-patches/0002). The client fixture adds key + solved challenge to
# /api/subscribe and /api/unsubscribe unless the test sets them itself (see _gate).
# Sites (mirror of prod, db-patches/0004):
#   SITE_KEY    simplify-erp  quiz on, global sink, mauxy DOI, no segment (legacy suites: Mautic tag only)
#   WEB_KEY     simplify-web  the website contract: quiz OFF on subscribe, pinned to Odoo, mauxy DOI
#   LUDO_KEY    ludo          upstream behaviour: pinned to Mautic, Mautic runs the DOI (doi_owner sink)
#   RECLOUD_KEY re-cloud      pinned to Mautic with a segment, mauxy DOI (segment added after the click)
SITE_KEY = "test-site-key-0123456789"
WEB_KEY = "test-web-public-key-0123456789"
LUDO_KEY = "test-ludo-key-0123456789"
RECLOUD_KEY = "test-recloud-key-0123456789"
os.environ.setdefault("MAUXY_SITES", json.dumps([
    {"key": SITE_KEY, "site": "simplify-erp", "segment": "", "topic": "simplify-erp"},
    {"key": WEB_KEY, "site": "simplify-web", "segment": "", "topic": "simplify-erp",
     "subscribe_quiz": False, "subscriber_sink": "odoo"},
    {"key": LUDO_KEY, "site": "ludo", "segment": "ludo", "topic": "ludo",
     "subscriber_sink": "mautic", "doi_owner": "sink"},
    {"key": RECLOUD_KEY, "site": "re-cloud", "segment": "re-cloud", "topic": "re-cloud", "subscriber_sink": "mautic"},
]))
os.environ.setdefault("CHALLENGE_SECRET", "challenge-test-secret-for-pytest-only")
os.environ.setdefault("CHALLENGE_RATE_LIMIT", "10000/minute")
os.environ.setdefault("SUBSCRIBE_RATE_LIMIT", "10000/minute")

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


GATED = ("/api/subscribe", "/api/unsubscribe")


def solve_challenge(c, key: str = SITE_KEY, send=None) -> dict:
    """GET /api/challenge with the site key, answer from the server-side bank (what a human does).
    `send` = the undrained request function (the gate must not wait for running jobs)."""
    r = (send or c.request)("GET", "/api/challenge", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    token = r.json()["challenge"]
    topic, qid = token.split(".")[:2]
    (q,) = [q for q in mauxy_main.QUIZ_BANK[topic] if q["id"] == qid]
    return {"challenge": token, "answer": q["answer"]}


def _gate(c, send, method, url, kwargs):
    """Add the site key and a solved challenge to a newsletter call that does not set them.
    A test that checks the gates passes its own Authorization header / challenge."""
    if method.upper() == "POST" and str(url) in GATED:
        headers = dict(kwargs.get("headers") or {})
        if not any(h.lower() == "authorization" for h in headers):
            headers["Authorization"] = f"Bearer {SITE_KEY}"
        kwargs["headers"] = headers
        body = kwargs.get("json")
        if isinstance(body, dict) and "challenge" not in body and headers["Authorization"] == f"Bearer {SITE_KEY}":
            kwargs["json"] = {**body, **solve_challenge(c, send=send)}
    return send(method, url, **kwargs)


@pytest.fixture
def client(app):
    # `with TestClient(app)` triggers lifespan so the SQLite action log opens.
    with TestClient(app) as c:
        # db-patches/0006 seeds the simplify-erp brand with the PROD website (https://simplify-erp.de);
        # every environment sets its own (stag / dev: PUT /api/admin/brands/simplify-erp), so does the
        # test environment: the legacy suites expect redirects to PUBLIC_WEBSITE_BASE_URL.
        r = c.request("PUT", "/api/admin/brands/simplify-erp", headers={"Authorization": "Bearer admin-test-key"},
                      json={"website_url": "https://site.test"})
        assert r.status_code == 200, r.text
        # Contact-dependent work runs as fire-and-forget jobs after the response (jobs.py).
        # Tests see its effects by draining the jobs after every request; the timing tests
        # use c.request_nodrain to observe the response before any job ran.
        orig = c.request

        def request_nodrain(method, url, **kwargs):
            return _gate(c, orig, method, url, kwargs)

        def request_and_drain(method, url, **kwargs):
            resp = request_nodrain(method, url, **kwargs)
            c.portal.call(jobs.drain)
            return resp

        c.request_raw = orig  # no gate, no drain (tests of the gates themselves)
        c.request_nodrain = request_nodrain
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


# -- DOI links: two steps (security review 2026-11-09, link prefetch) --------------------------
CONFIRM_FORM = re.compile(r'<form id="mx-form" method="post" action="([^"]+)">\s*'
                          r'<input id="mx-token" type="hidden" name="token" value="([^"]+)">')
MAUXY_ORIGIN = {"Origin": "https://mauxy.test"}
NEUTRAL_MARK = 'id="mx-heading">Link ungültig oder abgelaufen<'


def confirm_form(page) -> tuple[str, str] | None:
    """(action, token) of the rendered confirmation page, None for any other response."""
    m = CONFIRM_FORM.search(page.text) if page.status_code == 200 else None
    return (html.unescape(m.group(1)), html.unescape(m.group(2))) if m else None


def click_confirm(client, url: str):
    """What a human does with a DOI mail link: open it (GET, read-only page), click "Bestätigen"
    (form POST from mauxy's own page). Returns the POST response, or the GET response when the
    page has no form (neutral page, misconfiguration redirect)."""
    page = client.get(url, follow_redirects=False)
    form = confirm_form(page)
    if form is None:
        return page
    action, token = form
    return client.post(action, data={"token": token}, headers=MAUXY_ORIGIN, follow_redirects=False)


def is_neutral(resp, status: int = 200) -> bool:
    """The uniform page for invalid / expired / used tokens."""
    return resp.status_code == status and NEUTRAL_MARK in resp.text and "mx-form" not in resp.text


def drain(client):
    """Run every pending job (DOI mails, lead_sync deliveries incl. retries)."""
    client.portal.call(jobs.drain)
