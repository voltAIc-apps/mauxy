"""
Canonical public host (owner decision 2026-11-09: https://mauxy.engage.wapsol.de).

Every public link mauxy hands out derives from ONE value, PUBLIC_BASE_URL: the DOI confirm links
(newsletter, Selbst-Check, CTA leads) and the LinkedIn redirect URI (start and callback on the same
host, the state cookie is host-only). The shipped config names only the canonical host; the old
unsubscribe.engage.wapsol.de is an alias added later outside k8s/.

  T-HOST-001  module constants: one PUBLIC_BASE_URL, redirect URI = PUBLIC_BASE_URL + callback path
  T-HOST-002  DOI mails of newsletter, Selbst-Check and lead link to PUBLIC_BASE_URL
  T-HOST-003  config templates (.env.example, k8s/) name the canonical host, never the old one
"""
import copy
import json
import re
from pathlib import Path

import auth_api
import leads_api
import main as mauxy_main
import selfcheck_api
from tests.conftest import WEB_KEY, drain

HERE = Path(__file__).parent
ROOT = HERE.parent
CANONICAL = "mauxy.engage.wapsol.de"
OLD = "unsubscribe.engage.wapsol.de"


def test_t_host_001_one_public_base_url():
    base = mauxy_main.PUBLIC_BASE_URL
    assert base and base == leads_api.PUBLIC_BASE_URL == selfcheck_api.PUBLIC_BASE_URL == auth_api.PUBLIC_BASE_URL
    assert auth_api.LINKEDIN_REDIRECT_URI == base + auth_api.CALLBACK_PATH


def test_t_host_002_doi_links_use_public_base_url(client, outbox, odoo):
    base = mauxy_main.PUBLIC_BASE_URL
    # newsletter (website key, quiz off, mauxy DOI)
    page = {"email": "nina@example.com", "locale": "de", "source": "footer", "consent": True,
            "pageUrl": "https://site.test/de/"}
    r = client.post("/api/subscribe", json=page, headers={"Authorization": f"Bearer {WEB_KEY}"})
    assert r.json() == {"status": "pending_confirmation"}
    # Selbst-Check
    sc = copy.deepcopy(json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"])
    assert client.post("/api/selfcheck", json=sc, headers={"Origin": "https://site.test"}).status_code == 200
    # CTA lead
    lead = copy.deepcopy(json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))["base"]["contact"])
    assert client.post("/api/lead", json=lead, headers={"Origin": "https://site.test"}).status_code == 200
    drain(client)
    links = [m for mail in outbox for m in re.findall(r"https?://[^\s\"'<>]+\?token=", mail["body"])]
    paths = {l.split("?")[0] for l in links}
    assert paths == {f"{base}/api/confirm", f"{base}/api/selfcheck/confirm", f"{base}/api/lead/confirm"}, links


def test_t_host_003_config_templates_name_the_canonical_host():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^DEPLOY_DOMAIN={re.escape(CANONICAL)}$", env, re.M)
    assert f"https://{CANONICAL}/api/auth/linkedin/callback" in env
    secret = (ROOT / "k8s" / "secret.yaml.example").read_text(encoding="utf-8")
    assert f"PUBLIC_BASE_URL=https://{CANONICAL}" in secret
    ingress = (ROOT / "k8s" / "ingress.yaml").read_text(encoding="utf-8")
    assert re.findall(r"host: (\S+)", ingress) == ["${DEPLOY_DOMAIN}"]  # one host, no alias here
    for f in [ROOT / ".env.example", *(ROOT / "k8s").glob("*.y*ml*")]:
        assert OLD not in f.read_text(encoding="utf-8"), f
