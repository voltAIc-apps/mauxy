#!/usr/bin/env python3
"""
Local end-to-end run of mauxy with a FAKE erp.wapsol (tests/fake_odoo.py) and console mails.
Nothing leaves the machine: Odoo calls go to the in-memory fake, mails are printed to the log.

  tests/venv/bin/python tests/scripts/run_local.py          # http://10.0.99.1:8020
  GET http://10.0.99.1:8020/__fake_odoo                      # what mauxy wrote into the fake
DOI links are printed in the console mail; open them in the browser to confirm.
Env defaults below can be overridden from the shell (never put real Odoo credentials here).
"""
import os
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
    "RATE_LIMIT": "60/minute",
    "SELFCHECK_RATE_LIMIT": "60/minute",
    "LEAD_RATE_LIMIT": "60/minute",
}
for k, v in DEFAULTS.items():
    os.environ.setdefault(k, v)
if not os.environ["ODOO_URL"].endswith(".test"):
    sys.exit("run_local.py only runs against the fake Odoo (ODOO_URL https://odoo.test)")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import odoo_client  # noqa: E402
from tests.fake_odoo import FakeOdoo  # noqa: E402

fake = FakeOdoo()
odoo_client.TRANSPORT = httpx.MockTransport(fake.handle)

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


if __name__ == "__main__":
    uvicorn.run(main.app, host=HOST, port=PORT, log_level="info")
