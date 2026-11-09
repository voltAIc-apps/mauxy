#!/usr/bin/env python3
"""
Local end-to-end run of mauxy with a FAKE erp.wapsol (tests/fake_odoo.py) and console mails.
Nothing leaves the machine: Odoo calls go to the in-memory fake, mails are printed to the log.

  tests/venv/bin/python tests/scripts/run_local.py          # http://10.0.99.1:8020
  GET http://10.0.99.1:8020/__fake_odoo                      # what mauxy wrote into the fake
DOI links are printed in the console mail; open them in the browser to confirm.
LinkedIn login: a MOCKED LinkedIn is mounted at /__mock_linkedin (consent page with "sign in" /
"cancel", token + userinfo with a fake person). Start the site with VITE_MAUXY_URL=http://HOST:PORT
and log in from there; nothing goes to linkedin.com.
Env defaults below can be overridden from the shell (never put real Odoo / LinkedIn credentials here).
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HOST = os.environ.get("MAUXY_HOST", "10.0.99.1")
PORT = int(os.environ.get("MAUXY_PORT", "8020"))
SITE = os.environ.get("SITE_ORIGIN", "http://10.0.99.1:5182")
DEFAULTS = {
    "MAIL_MODE": "console",
    "ACTION_LOG_DB": str(Path(os.environ.get("TMPDIR", "/tmp")) / "mauxy_local.db"),
    "ALLOWED_ORIGINS": SITE,
    "SELFCHECK_ALLOWED_ORIGINS": SITE,
    "LEAD_ALLOWED_ORIGINS": SITE,
    "PUBLIC_BASE_URL": f"http://{HOST}:{PORT}",
    "PUBLIC_WEBSITE_BASE_URL": SITE,
    "SELFCHECK_WEBSITE_URL": SITE,
    "CONFIRM_TOKEN_SECRET": "local-dev-only-not-a-secret",
    "ADMIN_API_KEY": "local-admin",
    "SELFCHECK_NOTIFY_TO": "owner@localhost",
    "LEAD_NOTIFY_TO": "owner@localhost",
    "LEAD_SINK": "odoo",
    "SUBSCRIBER_SINK": "odoo",
    "LEAD_SYNC_BACKOFF_SECONDS": "2",
    "ODOO_URL": "https://odoo.test",
    "ODOO_DB": "fake",
    "ODOO_USER": "website-bot",
    "ODOO_API_KEY": "test-key-not-real",
    "ODOO_LEAD_USER_ID": "7",
    "ODOO_LIST_NEWSLETTER_ID": "1",
    "ODOO_LIST_RADAR_ID": "2",
    # LinkedIn login against the mock below (plain http allowed only on the loopback alias)
    "LINKEDIN_CLIENT_ID": "local-client-id",
    "LINKEDIN_CLIENT_SECRET": "local-client-secret-not-real",
    "LINKEDIN_AUTH_URL": f"http://{HOST}:{PORT}/__mock_linkedin/oauth/v2/authorization",
    "LINKEDIN_TOKEN_URL": f"http://{HOST}:{PORT}/__mock_linkedin/oauth/v2/accessToken",
    "LINKEDIN_USERINFO_URL": f"http://{HOST}:{PORT}/__mock_linkedin/v2/userinfo",
    "AUTH_ALLOWED_ORIGINS": SITE,
    "AUTH_TOKEN_SECRET": "local-dev-only-auth-secret-not-a-secret",
    "RATE_LIMIT": "60/minute",
    # newsletter gate: one local site key + quiz (topic simplify-erp from db-patches/0002)
    "MAUXY_SITES": '[{"key":"local-site-key-not-a-secret","site":"simplify-erp","segment":"simplify-erp","topic":"simplify-erp"}]',
    "CHALLENGE_SECRET": "local-dev-only-challenge-secret",
    "SELFCHECK_RATE_LIMIT": "60/minute",
    "LEAD_RATE_LIMIT": "60/minute",
}
for k, v in DEFAULTS.items():
    os.environ.setdefault(k, v)
if not os.environ["ODOO_URL"].endswith(".test"):
    sys.exit("run_local.py only runs against the fake Odoo (ODOO_URL https://odoo.test)")
if "linkedin.com" in os.environ["LINKEDIN_TOKEN_URL"]:
    sys.exit("run_local.py only runs against the mocked LinkedIn")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import odoo_client  # noqa: E402
from tests.fake_odoo import FakeOdoo  # noqa: E402

fake = FakeOdoo()
odoo_client.TRANSPORT = httpx.MockTransport(fake.handle)

import db_migrate  # noqa: E402

db_migrate.apply_pending(os.environ["ACTION_LOG_DB"])  # the deployer step (k8s initContainer)
# dev environment = its own DB values (db-patches/0006 seeds the PROD website of simplify-erp):
# redirects after a DOI click go to the local site, like PUT /api/admin/brands/simplify-erp would set
with sqlite3.connect(os.environ["ACTION_LOG_DB"]) as _conn:
    _conn.execute("UPDATE site_brand SET website_url = ? WHERE site = 'simplify-erp'", (SITE,))

import main  # noqa: E402


@main.app.get("/__fake_odoo")
async def fake_state():
    """What mauxy wrote into the fake Odoo (local runs only)."""
    return {
        "leads": fake.leads(),
        "mailing_contacts": list(fake.db["mailing.contact"].values()),
        "mailing_subscriptions": list(fake.db["mailing.subscription"].values()),
        "write_calls": [c[:2] for c in fake.calls if c[1] not in ("search_read", "read")],
    }


# -- mocked LinkedIn (local runs only): fake person, no real data ----------------------------
from html import escape  # noqa: E402
from urllib.parse import parse_qs, urlencode  # noqa: E402

from fastapi import Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

FAKE_PERSON = {"sub": "mock-member-0001", "name": "Max Beispiel", "given_name": "Max", "family_name": "Beispiel",
               "email": "max.beispiel@example.com", "email_verified": True, "locale": {"country": "DE", "language": "de"}}


@main.app.get("/__mock_linkedin/oauth/v2/authorization", response_class=HTMLResponse)
async def mock_authorize(request: Request):
    q = request.query_params
    back = q.get("redirect_uri", "")
    ok = back + "?" + urlencode({"code": "mock-code-0001", "state": q.get("state", "")})
    cancel = back + "?" + urlencode({"error": "user_cancelled_login", "error_description": "cancelled", "state": q.get("state", "")})
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Mock LinkedIn</title></head>
<body id="mock-li-body" style="font-family:sans-serif;max-width:520px;margin:60px auto">
<div id="mock-li-card" style="border:1px solid #ccc;border-radius:12px;padding:24px">
<h1 id="mock-li-title" style="color:#0a66c2">LinkedIn (Mock)</h1>
<p id="mock-li-scope">client_id {escape(q.get("client_id", ""))} m&ouml;chte: {escape(q.get("scope", ""))}</p>
<p id="mock-li-person">Angemeldet als {FAKE_PERSON["name"]} (Testperson)</p>
<a id="mock-li-allow" href="{escape(ok)}" style="background:#0a66c2;color:#fff;padding:10px 18px;border-radius:20px;text-decoration:none">Zulassen</a>
<a id="mock-li-cancel" href="{escape(cancel)}" style="margin-left:16px">Abbrechen</a>
</div></body></html>"""


@main.app.post("/__mock_linkedin/oauth/v2/accessToken")
async def mock_token(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("code") != "mock-code-0001" or form.get("client_secret") != os.environ["LINKEDIN_CLIENT_SECRET"]:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    return {"access_token": "mock-access-token", "expires_in": 3600, "scope": "openid,profile,email"}


@main.app.get("/__mock_linkedin/v2/userinfo")
async def mock_userinfo(request: Request):
    if request.headers.get("authorization") != "Bearer mock-access-token":
        return JSONResponse({"message": "unauthorized"}, status_code=401)
    return FAKE_PERSON


if __name__ == "__main__":
    uvicorn.run(main.app, host=HOST, port=PORT, log_level="info")
