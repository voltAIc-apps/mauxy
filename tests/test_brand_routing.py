"""
mauxy fully per site (owner requirement 2026-11-09, db-patches/0006): brand profile, action
routing ("hooks") with fan-out through the route_delivery outbox, signed webhook sink.

  T-BR-001..031  brand admin validation matrix (cases/brand_routing_cases.json "brand_admin")
  T-BR-040..043  rendering escapes every brand value (XSS attempts, "xss")
  T-BR-050..058  branded confirm / neutral page, CSP, redirects, mails, sender allow-list, admin
  T-BR-060..064  regression: unbranded sites (LUDO) byte-identical, simplify-erp seed = old texts
  T-RT-001..021  route admin validation matrix ("route_admin")
  T-RT-030..041  fan-out, partial failure + retry, idempotency, permanent failure + mail + admin
                 retry, unsubscribe reaches every target, routed leads / self-check, fallback
  T-WH-001..005  signature, headers, body, receiver reference check
  T-WH-010..024  SSRF matrix ("ssrf"), T-WH-030..039 response handling ("responses")
  T-WH-040..041  real TLS on 10.0.99.1: connection pinned to the checked address, SNI + certificate
                 checked on the host name; wrong certificate = permanent (MITM)
Odoo is the in-memory fake, Mautic is mocked with respx, webhooks go to an httpx.MockTransport
(or a local TLS server on 10.0.99.1). Nothing leaves the machine.
"""
import asyncio
import base64
import copy
import hashlib
import json
import re
import ssl
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import respx

import brand
import confirm_page
import lead_sync
import leads_api
import main as mauxy_main
import routing
import selfcheck_api
import sinks
import webhooks
from tests.conftest import (LUDO_KEY, MAUXY_ORIGIN, RECLOUD_KEY, WEB_KEY, click_confirm, confirm_form, drain,
                            solve_challenge)

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "brand_routing_cases.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((HERE / "cases" / "brand_golden.json").read_text(encoding="utf-8"))
SC_BASE = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
LEAD_BASE = json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))["base"]["contact"]
ADMIN = {"Authorization": "Bearer admin-test-key"}
PUBLIC_IP = "93.184.215.14"
SECRET = "flywheel-webhook-secret-0123456789abcdef"
FLYWHEEL = {"url": "https://flywheel.test/hooks/mauxy", "secret_ref": "flywheel"}
OTHER = {"url": "https://hooks.test/in", "secret_ref": "other"}


# ------------------------------------------------------------------------------------------ fixtures


def png(width=1, height=1, pad=0) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    extra = chunk(b"tEXt", b"pad\x00" + b"x" * pad) if pad else b""
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + extra + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


WEBP = b"RIFF" + struct.pack("<I", 26) + b"WEBPVP8 " + b"\x00" * 18
LOGOS = {"$PNG": base64.b64encode(png()).decode(), "$WEBP": base64.b64encode(WEBP).decode(),
         "$JPEG": base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode(),
         "$PNG_BIG": base64.b64encode(png(pad=70_000)).decode(), "$NOT_B64": "!!not*base64!!"}


def resolve_tokens(obj):
    if isinstance(obj, str):
        return LOGOS.get(obj, obj)
    if isinstance(obj, list):
        return [resolve_tokens(x) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve_tokens(v) for k, v in obj.items()}
    return obj


class Hooks:
    """Webhook receivers behind an httpx.MockTransport: records every request, answers from a
    queue per host (default 200)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, list] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.answers.get(request.headers["host"].split(":")[0]) or []
        a = queue.pop(0) if queue else 200
        if a == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        return httpx.Response(a, headers={"Location": "https://169.254.169.254/latest"} if 300 <= a < 400 else {})

    def to(self, host):
        return [r for r in self.requests if r.headers["host"].split(":")[0] == host]

    def events(self, host):
        return [json.loads(r.content) for r in self.to(host)]


@pytest.fixture
def hooks(monkeypatch):
    h = Hooks()
    monkeypatch.setattr(webhooks, "TRANSPORT", httpx.MockTransport(h.handle))
    monkeypatch.setattr(webhooks, "RESOLVER", lambda host, port: [PUBLIC_IP])
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.test,flywheel.test,10.0.99.1")
    monkeypatch.setenv("WEBHOOK_SECRET_FLYWHEEL", SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET_OTHER", "other-webhook-secret-0123456789abcdef")
    monkeypatch.setenv("WEBHOOK_SECRET_SHORT", "too-short")
    monkeypatch.delenv("WEBHOOK_SECRET_MISSING", raising=False)
    return h


@pytest.fixture(autouse=True)
def sender_domains(monkeypatch):
    monkeypatch.setenv("MAIL_SENDER_DOMAINS", "site.test,re-cloud.test")
    # /health/detail caches sink reachability for 30 s: never leak this module's cache to others
    monkeypatch.setattr(mauxy_main, "_sink_health", {"checked_at": -1e9, "detail": {}})


def put_brand(client, site, body):
    return client.request("PUT", f"/api/admin/brands/{site}", headers=ADMIN, json=body)


def put_routes(client, site, action, targets):
    return client.request("PUT", f"/api/admin/routes/{site}/{action}", headers=ADMIN, json={"targets": targets})


async def _q(app, sql, params=()):
    async with app.state.db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


def q(client, sql, params=()):
    return client.portal.call(_q, client.app, sql, params)


def deliveries(client):
    return q(client, "SELECT * FROM route_delivery ORDER BY id")


RECLOUD = {"display_name": "Re-Cloud", "legal_entity": "wapsol GmbH", "color_primary": "#112233",
           "color_accent": "#445566", "wordmark": "re-cloud", "website_url": "https://re-cloud.test",
           "imprint_url": "https://re-cloud.test/{lang}/impressum", "privacy_url": "https://re-cloud.test/{lang}/datenschutz",
           "default_locale": "de", "mail_from_name": "Re-Cloud Team", "mail_from_address": "hello@re-cloud.test",
           "mail_reply_to": "info@re-cloud.test", "mail_footer": {"de": "Re-Cloud Team\nwapsol GmbH", "en": "Re-Cloud team\nwapsol GmbH"},
           "origins": ["https://re-cloud.test"], "logo": {"mime": "image/png", "data": LOGOS["$PNG"]}}


def nl_subscribe(client, outbox, email, key=WEB_KEY, locale="de"):
    """Newsletter subscribe on a site -> its DOI mail -> token."""
    body = {"email": email, "locale": locale, "source": "footer", "consent": True, "pageUrl": f"https://site.test/{locale}/"}
    if key == RECLOUD_KEY:
        body.update(solve_challenge(client, RECLOUD_KEY, send=client.request_raw))
    r = client.post("/api/subscribe", headers={"Authorization": f"Bearer {key}"}, json=body)
    assert r.status_code == 200, r.text
    mail = [m for m in outbox if m["to"] == email.lower()][-1]
    return re.search(r"/api/confirm\?token=(\S+)", mail["body"]).group(1), mail


def nl_confirm(client, outbox, email, key=WEB_KEY):
    token, _ = nl_subscribe(client, outbox, email, key)
    r = click_confirm(client, f"/api/confirm?token={token}")
    drain(client)
    return r


def unsubscribe(client, email, key=WEB_KEY):
    r = client.request_raw("POST", "/api/unsubscribe", headers={"Authorization": f"Bearer {key}"},
                           json={"email": email, **solve_challenge(client, key, send=client.request_raw)})
    drain(client)
    return r


# ------------------------------------------------------------------------------------------ brand admin


@pytest.mark.parametrize("case", CASES["brand_admin"], ids=[c["id"] for c in CASES["brand_admin"]])
def test_t_br_001_031_brand_admin_validation(case, client):
    r = put_brand(client, "brand-x", resolve_tokens(case["body"]))
    assert r.status_code == case["status"], (r.status_code, r.text[:300])
    if "error" in case:
        assert case["error"] in r.json()["error"], r.text
    stored = client.request("GET", "/api/admin/brands/brand-x", headers=ADMIN)
    assert stored.status_code == (200 if case["status"] in (200, 201) else 404)


@pytest.mark.parametrize("case", CASES["xss"], ids=[c["id"] for c in CASES["xss"]])
def test_t_br_040_043_render_escapes_brand_values(case, client):
    body = {"display_name": "Re-Cloud", case["field"]: case["value"]}
    if case.get("with_logo"):
        body["logo"] = {"mime": "image/png", "data": LOGOS["$PNG"]}
    assert put_brand(client, "brand-x", body).status_code == 201
    page = confirm_page.render("newsletter", "de", "/api/confirm", "tok", ["https://re-cloud.test"], brand=brand.get("brand-x"))
    neutral = confirm_page.neutral(brand=brand.get("brand-x"))
    for resp in (page, neutral):
        text = resp.body.decode()
        assert case["escaped"] in text and case["raw"] not in text
        assert "<script" not in text


# ------------------------------------------------------------------------------------------ branded pages


def _style_hash(text):
    style = re.search(r'<style id="mx-style">(.*?)</style>', text, re.S).group(1)
    return "'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'"


def test_t_br_050_confirm_page_per_brand(client, outbox):
    """re-cloud (test site on Mautic, mauxy DOI) with a full profile: wordmark, logo (data: URI,
    CSP img-src data:), colours inside the hashed stylesheet, legal line, redirect origins in
    form-action, unique ids, no script."""
    assert put_brand(client, "re-cloud", {**RECLOUD, "redirects": [
        {"flow": "newsletter", "locale": "de", "outcome": "confirmed", "url": "https://go.re-cloud.test/de/danke"}]}).status_code == 201
    with respx.mock(assert_all_called=False) as m:
        m.get(re.compile(r"https://mautic\.test/api/contacts.*")).mock(return_value=httpx.Response(200, json={"contacts": {}}))
        token, mail = nl_subscribe(client, outbox, "anna@example.com", key=RECLOUD_KEY)
    page = client.get(f"/api/confirm?token={token}")
    t, csp = page.text, page.headers["content-security-policy"]
    assert 'id="mx-brand" class="brand"><img id="mx-logo" class="logo" src="data:image/png;base64,' in t
    assert ">re-cloud</p>" in t and "wapsol GmbH" in t
    assert 'href="https://re-cloud.test/de/impressum"' in t and 'href="https://re-cloud.test/de/datenschutz"' in t
    assert "background:#112233" in t and "outline:3px solid #445566" in t
    assert "img-src data:" in csp and _style_hash(t) in csp and "unsafe-inline" not in csp and "script" not in t
    assert "form-action 'self' https://go.re-cloud.test https://re-cloud.test" in csp
    ids = re.findall(r'\bid="([^"]+)"', t)
    assert len(ids) == len(set(ids)) and t.count("<") == t.count('id="') + t.count("</") + 1  # every element has an id (+ doctype)
    assert confirm_form(page) is not None


def test_t_br_051_neutral_page_branded_and_uniform(client, outbox, odoo, monkeypatch):
    """The neutral page carries the brand of the token's site claim -- the same bytes for a
    tampered, a used and an expired token of that site; garbage / unknown site -> generic."""
    assert put_brand(client, "simplify-web", {"display_name": "Simplify Web", "color_primary": "#0074b2"}).status_code == 201
    token, _ = nl_subscribe(client, outbox, "nina@example.com")
    tampered = token[:-2] + "zz"
    bodies = {client.get(f"/api/confirm?token={tampered}").content}
    assert click_confirm(client, f"/api/confirm?token={token}").status_code == 303
    bodies.add(client.get(f"/api/confirm?token={token}").content)  # used
    bodies.add(client.post("/api/confirm", data={"token": token}, headers=MAUXY_ORIGIN).content)  # used, POST
    token2, _ = nl_subscribe(client, outbox, "nora@example.com")
    monkeypatch.setattr(mauxy_main, "CONFIRM_TOKEN_MAX_AGE", -1)
    bodies.add(client.get(f"/api/confirm?token={token2}").content)  # expired
    assert len(bodies) == 1 and b"Simplify Web" in bodies.pop()
    forged = mauxy_main._token_signer.dumps({"p": "x", "b": "no-such-site"})
    assert client.get("/api/confirm?token=garbage").content == client.get(f"/api/confirm?token={forged}").content \
        == confirm_page.NEUTRAL_HTML.encode()
    assert confirm_page.token_site(mauxy_main._token_signer, "." + token) is None  # compressed payloads never decoded


def test_t_br_052_redirect_resolution(client, outbox, odoo):
    """explicit row > website_url + standard path > env (unbranded)."""
    assert put_brand(client, "simplify-web", {"display_name": "Simplify Web", "website_url": "https://web.site.test",
                                              "redirects": [{"flow": "newsletter", "locale": "en", "outcome": "confirmed",
                                                             "url": "https://web.site.test/{lang}/thanks?src=nl"}]}).status_code == 201
    assert mauxy_main._nl_redirect("simplify-web", "de", "confirmed") == "https://web.site.test/de/newsletter/confirmed"
    assert mauxy_main._nl_redirect("simplify-web", "en", "confirmed") == "https://web.site.test/en/thanks?src=nl"
    assert mauxy_main._nl_redirect("simplify-web", "de", "error", "unavailable") == \
        "https://web.site.test/de/newsletter/confirm-error?reason=unavailable"
    assert mauxy_main._nl_redirect("ludo", "de", "confirmed") == "https://site.test/de/newsletter/confirmed"  # env
    assert mauxy_main._nl_redirect(None, "xx", "error", "server_misconfigured") == \
        "https://site.test/en/newsletter/confirm-error?reason=server_misconfigured"
    r = nl_confirm(client, outbox, "rita@example.com")
    assert r.headers["location"] == "https://web.site.test/de/newsletter/confirmed"


def test_t_br_053_newsletter_doi_mail_sender_footer(client, outbox, odoo):
    assert put_brand(client, "simplify-web", {"display_name": "Simplify Web", "mail_from_name": "Simplify Team",
                                              "mail_from_address": "team@site.test", "mail_reply_to": "help@site.test",
                                              "mail_footer": {"de": "Simplify Team\nwapsol GmbH"}}).status_code == 201
    _, mail = nl_subscribe(client, outbox, "lena@example.com")
    msg = mail["mime"].decode()
    assert "From: Simplify Team <team@site.test>" in msg and "Reply-To: help@site.test" in msg
    assert mail["message_id"].endswith("@site.test>")
    assert mail["body"].rstrip().endswith("Simplify Team\nwapsol GmbH")
    # an unbranded site keeps SMTP_FROM and no footer
    with respx.mock(assert_all_called=False) as m:
        m.get(re.compile(r"https://mautic\.test/api/contacts.*")).mock(return_value=httpx.Response(200, json={"contacts": {}}))
        _, plain = nl_subscribe(client, outbox, "otto@example.com", key=RECLOUD_KEY)
    assert "From: Test <noreply@site.test>" in plain["mime"].decode() and "wapsol" not in plain["body"]


def test_t_br_054_sender_allow_list_enforced_at_send_time(client, outbox, odoo, monkeypatch):
    """Profile valid when written; MAIL_SENDER_DOMAINS later narrowed -> SMTP_FROM, never the
    unlisted domain (mailcow sender ACL / SPF)."""
    assert put_brand(client, "simplify-web", {"display_name": "W", "mail_from_address": "team@re-cloud.test"}).status_code == 201
    monkeypatch.setenv("MAIL_SENDER_DOMAINS", "site.test")
    _, mail = nl_subscribe(client, outbox, "ulf@example.com")
    assert "From: Test <noreply@site.test>" in mail["mime"].decode() and "re-cloud.test" not in mail["mime"].decode()
    monkeypatch.delenv("MAIL_SENDER_DOMAINS")  # default: only the SMTP_FROM domain
    assert put_brand(client, "brand-y", {"display_name": "Y", "mail_from_address": "a@re-cloud.test"}).status_code == 400


def test_t_br_055_lead_and_selfcheck_per_brand(client, outbox, odoo, monkeypatch):
    """Origin https://re-cloud.test -> site re-cloud: lead DOI with its name / sender / footer,
    branded confirmation page, redirect to its website; Selbst-Check signature swapped."""
    assert put_brand(client, "re-cloud", RECLOUD).status_code == 201
    monkeypatch.setattr(leads_api, "LEAD_ALLOWED_ORIGINS", ["https://site.test", "https://re-cloud.test"])
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_ALLOWED_ORIGINS", ["https://site.test", "https://re-cloud.test"])
    origin = {"Origin": "https://re-cloud.test"}
    assert client.post("/api/lead", headers=origin, json={**copy.deepcopy(LEAD_BASE), "email": "kim@example.com"}).status_code == 200
    (mail,) = [m for m in outbox if m["to"] == "kim@example.com"]
    assert mail["subject"] == "Bitte bestätigen: Ihre Anfrage auf re-cloud.test"
    assert "Sie haben auf re-cloud.test eine Kontaktanfrage gesendet" in mail["body"]
    assert mail["body"].endswith("einfach.\n\nRe-Cloud Team\nwapsol GmbH") and "simplify-erp" not in mail["body"]
    assert "From: Re-Cloud Team <hello@re-cloud.test>" in mail["mime"].decode()
    token = re.search(r"/api/lead/confirm\?token=(\S+)", mail["body"]).group(1)
    page = client.get(f"/api/lead/confirm?token={token}")
    assert ">re-cloud</p>" in page.text and "form-action 'self' https://re-cloud.test" in page.headers["content-security-policy"]
    r = client.post("/api/lead/confirm", data={"token": token}, headers=MAUXY_ORIGIN, follow_redirects=False)
    assert r.headers["location"] == "https://re-cloud.test/de/anfrage/bestaetigt?art=contact"
    drain(client)
    (lead,) = odoo.leads()
    assert "Quelle: re-cloud.test / Website / Kampagne contact" in lead["description"]
    assert q(client, "SELECT site FROM lead_submission")[0]["site"] == "re-cloud"

    assert client.post("/api/selfcheck", headers=origin, json={**copy.deepcopy(SC_BASE), "email": "sam@example.com"}).status_code == 200
    (doi,) = [m for m in outbox if m["to"] == "sam@example.com"]
    assert doi["body"].endswith("Re-Cloud Team\nwapsol GmbH") and "Ashant Chalasani" not in doi["body"]
    token = re.search(r"/api/selfcheck/confirm\?token=(\S+)", doi["body"]).group(1)
    r = click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    assert r.headers["location"] == "https://re-cloud.test/de/selbst-check?bestaetigt=1"
    drain(client)
    result = [m for m in outbox if m["to"] == "sam@example.com" and "Ergebnis" in m["subject"]][0]
    assert result["body"].endswith("Re-Cloud Team\nwapsol GmbH") and "https://re-cloud.test/de/selbst-check" in result["body"]
    assert "From: Re-Cloud Team <hello@re-cloud.test>" in result["mime"].decode()


def test_t_br_056_admin_auth_logo_meta_delete_preview(client, monkeypatch):
    assert client.request("GET", "/api/admin/brands").status_code == 401
    assert client.request("PUT", "/api/admin/brands/x", json={"display_name": "X"}).status_code == 401
    assert put_brand(client, "re-cloud", RECLOUD).status_code == 201
    got = client.request("GET", "/api/admin/brands/re-cloud", headers=ADMIN).json()
    assert got["logo"]["mime"] == "image/png" and got["logo"]["bytes"] == len(png()) and "data" not in got["logo"]
    assert got["origins"] == ["https://re-cloud.test"] and got["mail_footer"]["de"].startswith("Re-Cloud")
    listed = client.request("GET", "/api/admin/brands", headers=ADMIN).json()
    assert {b["site"] for b in listed["brands"]} == {"simplify-erp", "re-cloud"} and listed["default_site"] == "simplify-erp"
    assert LOGOS["$PNG"] not in json.dumps(listed)
    prev = client.request("GET", "/api/admin/brands/re-cloud/preview?flow=lead&lang=en", headers=ADMIN)
    assert prev.status_code == 200 and "Confirm your request" in prev.text and ">re-cloud</p>" in prev.text
    # partial update keeps the rest; null clears an optional field, not a required one
    assert put_brand(client, "re-cloud", {"legal_entity": None, "logo": None}).status_code == 200
    b = brand.get("re-cloud")
    assert b.legal_entity is None and b.logo is None and b.display_name == "Re-Cloud" and b.origins == ("https://re-cloud.test",)
    assert put_brand(client, "re-cloud", {"display_name": None}).status_code == 400
    assert client.request("PUT", "/api/admin/brands/bad%20site", headers=ADMIN, json={"display_name": "X"}).status_code == 422
    assert client.request("DELETE", "/api/admin/brands/re-cloud", headers=ADMIN).status_code == 200
    assert brand.get("re-cloud") is None and brand.site_for_origin("https://re-cloud.test") == "simplify-erp"
    assert q(client, "SELECT * FROM site_origin WHERE site = 're-cloud'") == []
    monkeypatch.setattr(mauxy_main, "ADMIN_API_KEY", "")
    assert client.request("GET", "/api/admin/brands", headers=ADMIN).status_code == 403


def test_t_br_057_brand_only_on_person_facing_mails(client, outbox, odoo):
    """Operator / failure mails stay on SMTP_FROM (internal), whatever the site's sender."""
    assert put_brand(client, "simplify-erp", {"mail_from_address": "team@site.test", "mail_from_name": "Simplify"}).status_code == 200
    client.post("/api/selfcheck", headers={"Origin": "https://site.test"}, json={**copy.deepcopy(SC_BASE), "email": "eva@example.com"})
    token = re.search(r"/api/selfcheck/confirm\?token=(\S+)", outbox[-1]["body"]).group(1)
    click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    drain(client)
    by_to = {m["to"]: m for m in outbox}
    assert "From: Simplify <team@site.test>" in by_to["eva@example.com"]["mime"].decode()
    assert "From: Test <noreply@site.test>" in by_to["owner@site.test"]["mime"].decode()


def test_t_br_058_health_detail_counts_no_secrets(client, hooks):
    assert put_routes(client, "simplify-web", "unsubscribe", [{"sink": "webhook", "params": FLYWHEEL}]).status_code == 200
    d = client.get("/health/detail").json()
    assert d["brands"] == 1 and d["routes"] == 1 and d["webhooks"] == "3 allowed host(s)" and d["deliveries_failed"] == 0
    assert SECRET not in json.dumps(d)


# ------------------------------------------------------------------------------------------ regression


def test_t_br_060_unbranded_pages_byte_identical():
    """No profile (LUDO, any site without a site_brand row): the pages of before 0006."""
    for key, args in (("render_newsletter_de", ("newsletter", "de", "/api/confirm", "TOKEN-abc", "https://site.test")),
                      ("render_lead_en", ("lead", "en", "/api/lead/confirm", "TOKEN-abc", "https://site.test"))):
        r = confirm_page.render(*args)
        assert r.body.decode() == GOLDEN[key]["body"] and r.headers["content-security-policy"] == GOLDEN[key]["csp"]
    n = confirm_page.neutral()
    assert n.body.decode() == GOLDEN["neutral"]["body"] and n.headers["content-security-policy"] == GOLDEN["neutral"]["csp"]


@respx.mock
def test_t_br_061_ludo_flow_unchanged_with_other_sites_branded_and_routed(respx_mock, client, outbox, odoo, hooks):
    """Other sites with profiles + routes do not touch LUDO: Mautic segment at once, no mail, no
    outbox row; unsubscribe synchronous to Mautic DNC."""
    assert put_brand(client, "re-cloud", RECLOUD).status_code == 201
    assert put_routes(client, "simplify-web", "subscribe", [{"sink": "webhook", "params": FLYWHEEL}]).status_code == 200
    contacts = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
    respx_mock.get(contacts).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    respx_mock.post("https://mautic.test/api/contacts/new").mock(return_value=httpx.Response(201, json={"contact": {"id": 11}}))
    respx_mock.get("https://mautic.test/api/segments").mock(return_value=httpx.Response(200, json={"lists": {"4": {"id": 4, "alias": "ludo"}}}))
    add = respx_mock.post("https://mautic.test/api/segments/4/contact/11/add").mock(return_value=httpx.Response(200))
    ludo = {"Authorization": f"Bearer {LUDO_KEY}"}
    r = client.request_raw("POST", "/api/subscribe", headers=ludo,
                           json={"email": "lu@example.com", **solve_challenge(client, LUDO_KEY, send=client.request_raw)})
    assert r.json() == {"status": "pending_confirmation"} and add.called and outbox == [] and deliveries(client) == []
    respx_mock.get(contacts).mock(return_value=httpx.Response(200, json={"contacts": {"11": {
        "id": 11, "fields": {"core": {"email": {"value": "lu@example.com"}}}, "tags": [], "doNotContact": []}}}))
    dnc = respx_mock.post("https://mautic.test/api/contacts/11/dnc/email/add").mock(return_value=httpx.Response(200))
    assert unsubscribe(client, "lu@example.com", LUDO_KEY).json() == {"status": "ok"}
    assert dnc.called and deliveries(client) == [] and hooks.requests == [] and odoo.calls == []
    assert brand.get("ludo") is None and routing.unsubscribe_targets("ludo", "mautic") is None


def test_t_br_062_seed_simplify_erp(client):
    async def rows(app):
        return await _q(app, "SELECT * FROM site_brand WHERE site = 'simplify-erp'")
    (b,) = client.portal.call(rows, client.app)
    assert b["display_name"] == "Simplify-ERP" and b["legal_entity"] == "wapsol GmbH"
    assert b["imprint_url"] == "https://simplify-erp.de/{lang}/impressum" and b["mail_from_address"] is None
    assert brand.site_for_origin("https://www.simplify-erp.de/") == "simplify-erp"
    assert brand.footer("simplify-erp", "en") == selfcheck_api.SIGNATURE["en"]


def test_t_br_063_simplify_erp_lead_doi_text_unchanged(client):
    """With the prod website the seeded profile reproduces the lead DOI mail of before 0006."""
    assert put_brand(client, "simplify-erp", {"website_url": "https://simplify-erp.de"}).status_code == 200
    old = {"de": ("Bitte bestätigen: Ihre Anfrage auf simplify-erp.de",
                  "Guten Tag,\n\nSie haben auf simplify-erp.de eine Kontaktanfrage gesendet.\n\n"
                  "Bitte bestätigen Sie mit diesem Link, dass die Anfrage von Ihnen stammt:\nLINK\n\n"
                  "Der Link gilt 24 Stunden und kann einmal verwendet werden. Ohne Bestätigung bearbeiten wir nichts "
                  "und löschen Ihre Angaben nach 7 Tagen.\n\nWenn Sie das nicht waren, ignorieren Sie diese Mail einfach.\n\n"
                  "Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/de/impressum"),
           "en": ("Please confirm: your request on simplify-erp.de",
                  "Hello,\n\nYou sent a contact request on simplify-erp.de.\n\n"
                  "Please confirm with this link that the request came from you:\nLINK\n\n"
                  "The link is valid for 24 hours and can be used once. Without confirmation we do not process anything "
                  "and delete your details after 7 days.\n\nIf this was not you, simply ignore this mail.\n\n"
                  "Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/en/impressum")}
    for lang in ("de", "en"):
        assert leads_api.doi_mail("simplify-erp", lang, kind="contact", newsletter=False, link="LINK") == old[lang]
    rules = selfcheck_api.RULES["mail"]["doi"]["body"]["de"]
    assert brand.replace_signature(rules, "simplify-erp", "de", selfcheck_api.SIGNATURE["de"]) == rules


def test_t_br_064_patch_upgrades_existing_db(tmp_path):
    """0006 on a DB that already has rows from 0003..0005: columns default '', seed present."""
    import sqlite3
    import db_migrate
    path = tmp_path / "a.db"
    conn = sqlite3.connect(path)
    for v, _n, p in db_migrate.list_patches():
        if v < 6:
            conn.executescript(p.read_text())
    conn.execute("INSERT INTO lead_submission (kind, created_at, email, lang, data) VALUES ('contact', 'x', 'a@b.c', 'de', '{}')")
    conn.commit()
    conn.executescript((db_migrate.PATCHES_DIR / "0006_site_brand_routing.sql").read_text())
    assert conn.execute("SELECT site FROM lead_submission").fetchall() == [("",)]
    assert conn.execute("SELECT COUNT(*) FROM site_brand").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):  # last line of defence against a bad colour
        conn.execute("INSERT INTO site_brand (site, display_name, color_primary, updated_at) VALUES ('z', 'Z', 'red', 'x')")
    conn.close()


# ------------------------------------------------------------------------------------------ routing admin


@pytest.mark.parametrize("case", CASES["route_admin"], ids=[c["id"] for c in CASES["route_admin"]])
def test_t_rt_001_021_route_admin_validation(case, client, hooks):
    targets = case["targets"]
    if targets == "$ELEVEN":
        targets = [{"sink": "webhook", "params": {"url": f"https://hooks.test/{i}", "secret_ref": "other"}} for i in range(11)]
    r = put_routes(client, "brand-x", case["action"], targets)
    assert r.status_code == case["status"], (r.status_code, r.text[:300])
    if "error" in case:
        assert case["error"] in r.json()["error"], r.text
    assert bool(routing.targets("brand-x", case["action"])) == (case["status"] == 200)
    assert SECRET not in r.text


def test_t_rt_022_admin_routes_crud(client, hooks):
    assert client.request("GET", "/api/admin/routes").status_code == 401
    assert put_routes(client, "re-cloud", "subscribe", [{"sink": "mautic", "params": {"segment": "re-cloud"}},
                                                        {"sink": "webhook", "params": FLYWHEEL, "enabled": False}]).status_code == 200
    listed = client.request("GET", "/api/admin/routes?site=re-cloud", headers=ADMIN).json()
    assert [(r["position"], r["sink"], r["enabled"]) for r in listed["routes"]] == [(1, "mautic", True), (2, "webhook", False)]
    assert listed["actions"]["lead"] == ["odoo", "webhook"] and "flywheel.test" in listed["webhook_allowed_hosts"]
    assert [t.sink for t in routing.targets("re-cloud", "subscribe")] == ["mautic"]  # disabled target skipped
    assert client.request("DELETE", "/api/admin/routes/re-cloud/subscribe", headers=ADMIN).status_code == 200
    assert routing.targets("re-cloud", "subscribe") == []
    assert client.request("DELETE", "/api/admin/routes/re-cloud/subscribe", headers=ADMIN).status_code == 404


# ------------------------------------------------------------------------------------------ fan-out


def test_t_rt_030_newsletter_fan_out_per_target(client, outbox, odoo, hooks):
    """subscribe routed to [odoo list 5, flywheel webhook]: one outbox row per target, the list of
    the target (not ODOO_LIST_NEWSLETTER_ID), a signed event at the webhook, confirmed page."""
    odoo.add("mailing.list", id=5, name="Web Newsletter")
    assert put_routes(client, "simplify-web", "subscribe",
                      [{"sink": "odoo", "params": {"lists": {"newsletter": 5}}}, {"sink": "webhook", "params": FLYWHEEL}]).status_code == 200
    r = nl_confirm(client, outbox, "Petra@Example.com")
    assert r.status_code == 303 and r.headers["location"] == "https://site.test/de/newsletter/confirmed"
    rows = deliveries(client)
    assert [(d["sink"], d["state"], d["attempts"]) for d in rows] == [("odoo", "done", 1), ("webhook", "done", 1)]
    assert all(d["payload"] is None for d in rows)  # address cleared once delivered
    (sub,) = odoo.db["mailing.subscription"].values()
    assert sub["list_id"] == 5 and not sub["opt_out"]
    (ev,) = hooks.events("flywheel.test")
    assert ev["event"] == "subscribe" and ev["site"] == "simplify-web" and ev["data"]["email"] == "petra@example.com"
    assert ev["id"] == rows[1]["idem_key"]


def test_t_rt_031_partial_failure_retry_no_duplicates(client, outbox, odoo, hooks):
    """The webhook fails twice (503) then accepts; Odoo succeeds at once: Odoo written exactly once,
    the webhook retried with the SAME body + idempotency key, fresh timestamp + valid signature."""
    odoo.add("mailing.list", id=5, name="Web Newsletter")
    hooks.answers["flywheel.test"] = [503, 503]
    put_routes(client, "simplify-web", "subscribe", [{"sink": "webhook", "params": FLYWHEEL},
                                                     {"sink": "odoo", "params": {"lists": {"newsletter": 5}}}])
    nl_confirm(client, outbox, "carl@example.com")
    by_sink = {d["sink"]: d for d in deliveries(client)}
    assert by_sink["webhook"]["state"] == "done" and by_sink["webhook"]["attempts"] == 3
    assert by_sink["odoo"]["state"] == "done" and by_sink["odoo"]["attempts"] == 1
    assert len(odoo.writes_to("mailing.contact")) == 1
    reqs = hooks.to("flywheel.test")
    assert len(reqs) == 3 and len({r.content for r in reqs}) == 1
    assert len({r.headers["idempotency-key"] for r in reqs}) == 1
    for r in reqs:
        assert webhooks.verify(SECRET.encode(), r.headers["x-mauxy-timestamp"], r.content, r.headers["x-mauxy-signature"])


def test_t_rt_032_idempotent_enqueue(client, hooks):
    async def go(app):
        to = [routing.Target(1, "webhook", FLYWHEEL)]
        a = await routing.fan_out(app.state.db, site="s", action="unsubscribe", event_ref="unsub:1", data={"email": "a@b.de"}, to=to)
        b = await routing.fan_out(app.state.db, site="s", action="unsubscribe", event_ref="unsub:1", data={"email": "a@b.de"}, to=to)
        c = await routing.fan_out(app.state.db, site="s", action="subscribe", event_ref="unsub:1", data={"email": "a@b.de"}, to=to)
        return a, b, c
    a, b, c = client.portal.call(go, client.app)
    drain(client)
    assert len(a) == 1 and b == [] and len(c) == 1  # same action + event + target = one row; other action = own key
    assert len(hooks.requests) == 2


def test_t_rt_033_permanent_failure_mail_and_admin_retry(client, outbox, odoo, hooks):
    """A permanent failure (404) fails ONE target, the others are done; the operator gets a mail
    (no secret in it); after the fix the admin re-queues and it is delivered once."""
    odoo.add("mailing.list", id=5, name="Web Newsletter")
    hooks.answers["flywheel.test"] = [404]
    put_routes(client, "simplify-web", "subscribe", [{"sink": "odoo", "params": {"lists": {"newsletter": 5}}},
                                                     {"sink": "webhook", "params": FLYWHEEL}])
    nl_confirm(client, outbox, "fritz@example.com")
    by_sink = {d["sink"]: d for d in deliveries(client)}
    assert by_sink["odoo"]["state"] == "done" and by_sink["webhook"]["state"] == "failed"
    assert by_sink["webhook"]["failure_mailed"] == 1 and by_sink["webhook"]["payload"]  # kept for the retry
    (fail,) = [m for m in outbox if m["to"] == "owner@site.test"]
    assert "simplify-web / subscribe -> webhook" in fail["subject"] and "fritz@example.com" in fail["body"]
    assert SECRET not in fail["body"] and "/api/admin/deliveries/" in fail["body"]
    failed = client.request("GET", "/api/admin/deliveries?state=failed", headers=ADMIN).json()["deliveries"]
    assert [d["id"] for d in failed] == [by_sink["webhook"]["id"]] and "payload" not in failed[0]
    assert client.get("/health/detail").json()["deliveries_failed"] == 1
    r = client.request("POST", f"/api/admin/deliveries/{by_sink['webhook']['id']}/retry", headers=ADMIN)
    drain(client)
    assert r.json() == {"requeued": True}
    assert {d["sink"]: d["state"] for d in deliveries(client)} == {"odoo": "done", "webhook": "done"}
    assert len(odoo.writes_to("mailing.contact")) == 1 and len(hooks.to("flywheel.test")) == 2
    assert client.request("POST", f"/api/admin/deliveries/{by_sink['odoo']['id']}/retry", headers=ADMIN).status_code == 409


def test_t_rt_034_odoo_transient_retried_independently(client, outbox, odoo, hooks):
    odoo.add("mailing.list", id=5, name="Web Newsletter")
    put_routes(client, "simplify-web", "subscribe", [{"sink": "odoo", "params": {"lists": {"newsletter": 5}}},
                                                     {"sink": "webhook", "params": FLYWHEEL}])
    token, _ = nl_subscribe(client, outbox, "ida@example.com")
    odoo.fail[("mail.blacklist", "search_read")] = ["http502"]  # first Odoo call of the delivery
    click_confirm(client, f"/api/confirm?token={token}")
    drain(client)
    by_sink = {d["sink"]: d for d in deliveries(client)}
    assert by_sink["odoo"]["attempts"] == 2 and by_sink["odoo"]["state"] == "done"
    assert by_sink["webhook"]["attempts"] == 1 and len(hooks.requests) == 1


@respx.mock(assert_all_called=False)
def test_t_rt_035_unsubscribe_reaches_every_target(respx_mock, client, outbox, odoo, hooks):
    """simplify-web (fallback sink odoo) routes subscribe to [odoo list 5, mautic, webhook A] and
    leads to [webhook B]: an opt-out reaches Odoo (lists 1, 2 = fallback holders + 5), Mautic DNC,
    webhook A and webhook B -- each its own outbox row."""
    for lid in (5,):
        odoo.add("mailing.list", id=lid, name=f"L{lid}")
    cid = odoo.add("mailing.contact", name="x", email="tom@example.com")
    s1 = odoo.add("mailing.subscription", contact_id=cid, list_id=1, opt_out=False)  # from before the routes
    s5 = odoo.add("mailing.subscription", contact_id=cid, list_id=5, opt_out=False)
    put_routes(client, "simplify-web", "subscribe", [{"sink": "odoo", "params": {"lists": {"newsletter": 5}}},
                                                     {"sink": "mautic"}, {"sink": "webhook", "params": FLYWHEEL}])
    put_routes(client, "simplify-web", "lead", [{"sink": "webhook", "params": OTHER}])
    respx_mock.get(re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")).mock(return_value=httpx.Response(200, json={
        "contacts": {"11": {"id": 11, "fields": {"core": {"email": {"value": "tom@example.com"}}}, "tags": [], "doNotContact": []}}}))
    dnc = respx_mock.post("https://mautic.test/api/contacts/11/dnc/email/add").mock(return_value=httpx.Response(200))
    r = unsubscribe(client, "Tom@Example.com")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    rows = deliveries(client)
    assert sorted((d["sink"], d["state"]) for d in rows) == [("mautic", "done"), ("odoo", "done"), ("webhook", "done"), ("webhook", "done")]
    odoo_row = [d for d in rows if d["sink"] == "odoo"][0]
    assert json.loads(odoo_row["params"]) == {"list_ids": [1, 2, 5]}
    assert odoo.db["mailing.subscription"][s1]["opt_out"] and odoo.db["mailing.subscription"][s5]["opt_out"]
    assert dnc.called
    assert [e["event"] for e in hooks.events("flywheel.test")] == ["unsubscribe"]
    assert [e["data"] for e in hooks.events("hooks.test")] == [{"email": "tom@example.com"}]
    log = client.request("GET", "/api/actions?result=unsubscribe_queued", headers=ADMIN).json()["actions"]
    assert log[0]["site"] == "simplify-web" and log[0]["error_detail"] == "routed:4"


def test_t_rt_036_unsubscribe_down_target_still_reaches_others(client, odoo, hooks):
    """Compliance: Odoo down for every attempt -> the webhook still gets the opt-out, the Odoo row
    fails and the operator is told to apply it by hand."""
    put_routes(client, "simplify-web", "unsubscribe", [{"sink": "webhook", "params": FLYWHEEL}])
    odoo.fail[("mailing.subscription", "search_read")] = ["http502"] * 5
    r = unsubscribe(client, "uwe@example.com")
    assert r.json() == {"status": "ok"}  # no 503: the opt-out is durable in the outbox
    by_sink = {d["sink"]: d for d in deliveries(client)}
    assert by_sink["webhook"]["state"] == "done" and by_sink["odoo"]["state"] == "failed"
    assert by_sink["odoo"]["attempts"] == lead_sync.MAX_ATTEMPTS


def test_t_rt_037_routed_lead_odoo_overrides_webhook_and_newsletter(client, outbox, odoo, hooks):
    """Lead on simplify-erp routed to [odoo team 9 / tags / UTM, flywheel webhook]; the ticked
    newsletter follows the site's subscribe route (list 5)."""
    odoo.add("mailing.list", id=5, name="Web Newsletter")
    put_routes(client, "simplify-erp", "lead", [{"sink": "odoo", "params": {"team_id": 9, "tag_ids": [3], "utm_campaign": "webinar"}},
                                                {"sink": "webhook", "params": FLYWHEEL}])
    put_routes(client, "simplify-erp", "subscribe", [{"sink": "odoo", "params": {"lists": {"newsletter": 5}}}])
    client.post("/api/lead", headers={"Origin": "https://site.test"},
                json={**copy.deepcopy(LEAD_BASE), "email": "lea@example.com", "newsletter": True})
    token = re.search(r"/api/lead/confirm\?token=(\S+)", outbox[-1]["body"]).group(1)
    assert click_confirm(client, f"/api/lead/confirm?token={token}").status_code == 303
    drain(client)
    (lead,) = odoo.leads()
    assert lead["team_id"] == 9 and lead["tag_ids"] == [[6, 0, [3]]] and lead["campaign_id"] == 23
    (sub,) = odoo.db["mailing.subscription"].values()
    assert sub["list_id"] == 5
    (ev,) = hooks.events("flywheel.test")
    assert ev["event"] == "lead" and ev["data"]["kind"] == "contact" and ev["data"]["newsletter"] is True
    sync = q(client, "SELECT target, sub_results, state FROM lead_sync")[0]
    assert json.loads(sync["target"])["params"]["team_id"] == 9 and sync["state"] == "done" and sync["sub_results"] == "{}"
    assert sorted((d["action"], d["sink"]) for d in deliveries(client)) == [("lead", "webhook"), ("subscribe", "odoo")]


def test_t_rt_038_routed_selfcheck_webhook_only(client, outbox, odoo, hooks):
    """Selbst-Check routed to a webhook only: no crm.lead, no list write, the operator mail still
    goes out (nothing lost), the webhook gets the picks."""
    put_routes(client, "simplify-erp", "selfcheck", [{"sink": "webhook", "params": FLYWHEEL}])
    client.post("/api/selfcheck", headers={"Origin": "https://site.test"}, json={**copy.deepcopy(SC_BASE), "email": "ben@example.com"})
    token = re.search(r"/api/selfcheck/confirm\?token=(\S+)", outbox[-1]["body"]).group(1)
    click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    drain(client)
    assert odoo.leads() == [] and odoo.writes_to("mailing.contact") == []
    (ev,) = hooks.events("flywheel.test")
    assert ev["event"] == "selfcheck" and ev["data"]["picks"] == ["mail", "call", "radar"]
    note = [m for m in outbox if m["to"] == "owner@site.test"][0]
    assert "Migration Radar: routed" in note["body"]  # handled by the routed targets (route_delivery)


def test_t_rt_039_routed_odoo_lead_retry_keeps_one_lead(client, outbox, odoo, hooks):
    """The routed CRM target keeps lead_sync's duplicate guard (lost response -> no second lead)."""
    put_routes(client, "simplify-erp", "lead", [{"sink": "odoo", "params": {"team_id": 9}}])
    odoo.fail[("crm.lead", "create")] = ["lost"]
    client.post("/api/lead", headers={"Origin": "https://site.test"}, json={**copy.deepcopy(LEAD_BASE), "email": "max@example.com"})
    token = re.search(r"/api/lead/confirm\?token=(\S+)", outbox[-1]["body"]).group(1)
    click_confirm(client, f"/api/lead/confirm?token={token}")
    drain(client)
    assert len(odoo.leads()) == 1 and q(client, "SELECT state, attempts FROM lead_sync")[0] == {"state": "done", "attempts": 2}


def test_t_rt_040_resume_after_restart(client, hooks):
    async def go(app):
        db = app.state.db
        await db.execute("INSERT INTO route_delivery (idem_key, event_ref, site, action, position, sink, params, payload, "
                         "created_at) VALUES ('k1', 'unsub:x', 's', 'unsubscribe', 1, 'webhook', ?, ?, ?)",
                         (json.dumps(FLYWHEEL), json.dumps({"email": "r@example.com"}), routing.iso(routing.now())))
        await db.commit()
        return await routing.resume(db)
    assert client.portal.call(go, client.app) == 1
    drain(client)
    assert deliveries(client)[0]["state"] == "done" and len(hooks.requests) == 1


def test_t_rt_041_no_routes_fallback_unchanged(client, outbox, odoo):
    """No route for the site: the confirm writes synchronously to the fallback list (ODOO_LIST_
    NEWSLETTER_ID) and no outbox row exists -- exactly as before 0006."""
    nl_confirm(client, outbox, "gerd@example.com")
    (sub,) = odoo.db["mailing.subscription"].values()
    assert sub["list_id"] == 1 and deliveries(client) == []
    assert unsubscribe(client, "gerd@example.com").json() == {"status": "ok"} and deliveries(client) == []
    assert odoo.db["mailing.subscription"][sub["id"]]["opt_out"]


# ------------------------------------------------------------------------------------------ webhooks


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def deliver(params, **kw):
    return run(webhooks.deliver(params, idem_key=kw.get("idem_key", "k" * 64), action="unsubscribe", site="simplify-erp",
                                data={"email": "a@example.com"}, occurred_at="2026-11-09T10:00:00+00:00"))


def test_t_wh_001_005_signature_headers_body(hooks):
    status = deliver(FLYWHEEL)
    assert status == 200
    (r,) = hooks.requests
    assert str(r.url) == f"https://{PUBLIC_IP}/hooks/mauxy"  # pinned to the checked address
    assert r.headers["host"] == "flywheel.test" and r.extensions.get("sni_hostname") == "flywheel.test"
    assert r.headers["content-type"] == "application/json" and r.headers["x-mauxy-event"] == "unsubscribe"
    assert r.headers["x-mauxy-site"] == "simplify-erp" and r.headers["x-mauxy-delivery"] == r.headers["idempotency-key"] == "k" * 64
    ts, sig = r.headers["x-mauxy-timestamp"], r.headers["x-mauxy-signature"]
    expected = "v1=" + __import__("hmac").new(SECRET.encode(), ts.encode() + b"." + r.content, hashlib.sha256).hexdigest()
    assert sig == expected and webhooks.verify(SECRET.encode(), ts, r.content, sig)
    assert not webhooks.verify(SECRET.encode(), ts, r.content + b" ", sig)  # body tampered
    assert not webhooks.verify(b"x" * 32, ts, r.content, sig)                # other secret
    assert not webhooks.verify(SECRET.encode(), ts, r.content, sig, now=int(ts) + 301)  # replay window
    assert json.loads(r.content) == {"id": "k" * 64, "event": "unsubscribe", "site": "simplify-erp",
                                     "occurred_at": "2026-11-09T10:00:00+00:00", "data": {"email": "a@example.com"}}
    assert SECRET not in json.dumps(dict(r.headers)) and SECRET.encode() not in r.content


@pytest.mark.parametrize("case", CASES["ssrf"], ids=[c["id"] for c in CASES["ssrf"]])
def test_t_wh_010_024_ssrf(case, hooks, monkeypatch, client):
    monkeypatch.setattr(webhooks, "RESOLVER", lambda host, port: case["resolve"])
    params = {"url": case["url"], "secret_ref": "flywheel"}
    if case["expect"] == "ok":
        assert deliver(params) == 200
        (r,) = hooks.requests
        ip = case["resolve"][0]
        assert r.url.host == ip and r.extensions["sni_hostname"] == "hooks.test"
        port = re.search(r":(\d+)/", case["url"])
        assert r.headers["host"] == ("hooks.test" + (f":{port.group(1)}" if port else ""))
    else:
        with pytest.raises(sinks.SinkPermanent):
            deliver(params)
        assert hooks.requests == []
    if "admin" in case:
        assert put_routes(client, "brand-x", "unsubscribe", [{"sink": "webhook", "params": params}]).status_code == case["admin"]


@pytest.mark.parametrize("case", CASES["responses"], ids=[c["id"] for c in CASES["responses"]])
def test_t_wh_030_039_responses(case, hooks):
    hooks.answers["flywheel.test"] = [case["status"]]
    if case["expect"] == "ok":
        assert 200 <= deliver(FLYWHEEL) < 300
    else:
        with pytest.raises(sinks.SinkTransient if case["expect"] == "transient" else sinks.SinkPermanent):
            deliver(FLYWHEEL)
    assert len(hooks.requests) == case["requests"]  # a redirect is never followed


def test_t_wh_006_proxy_env_ignored_dns_failure_transient(hooks, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.9:3128")
    assert deliver(FLYWHEEL) == 200 and hooks.requests[0].url.host == PUBLIC_IP
    monkeypatch.setattr(webhooks, "RESOLVER", lambda host, port: [])
    with pytest.raises(sinks.SinkTransient):
        deliver(FLYWHEEL)
    monkeypatch.delenv("WEBHOOK_ALLOWED_HOSTS")  # allow-list empty = every webhook refused
    with pytest.raises(sinks.SinkPermanent):
        deliver(FLYWHEEL)


# ------------------------------------------------------------------------------------------ real TLS on 10.0.99.1


class _Recv(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Recv.seen.append((self.headers["Host"], self.headers["X-Mauxy-Signature"], self.headers["X-Mauxy-Timestamp"], body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def tls_receiver(tmp_path, monkeypatch):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))

    def start(cert_name):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ca.issue_cert(cert_name).configure_cert(ctx)
        srv = ThreadingHTTPServer(("10.0.99.1", 0), _Recv)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    servers = []
    _Recv.seen = []
    monkeypatch.setenv("WEBHOOK_CA_FILE", str(ca_file))
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "hooks.test")
    monkeypatch.setenv("WEBHOOK_SECRET_FLYWHEEL", SECRET)
    monkeypatch.setattr(webhooks, "TRANSPORT", None)  # real network stack
    monkeypatch.setattr(webhooks, "RESOLVER", lambda host, port: ["10.0.99.1"])
    monkeypatch.setattr(webhooks, "public_ip", lambda addr: True)  # the loopback alias stands in for a public host
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def test_t_wh_040_real_tls_pinned_with_sni(tls_receiver):
    port = tls_receiver("hooks.test")
    assert deliver({"url": f"https://hooks.test:{port}/in", "secret_ref": "flywheel"}) == 204
    ((host, sig, ts, body),) = _Recv.seen
    assert host == f"hooks.test:{port}" and webhooks.verify(SECRET.encode(), ts, body, sig)


def test_t_wh_041_wrong_certificate_is_permanent(tls_receiver):
    """Certificate for another name (a MITM would present one): refused, never retried."""
    port = tls_receiver("other.test")
    with pytest.raises(sinks.SinkPermanent, match="TLS"):
        deliver({"url": f"https://hooks.test:{port}/in", "secret_ref": "flywheel"})
    assert _Recv.seen == []


def test_t_br_065_image_and_secret_carry_the_new_parts():
    """The image ships brand / routing / webhooks; deploy.py syncs the new env vars and every
    WEBHOOK_SECRET_<REF>, never the test-only WEBHOOK_CA_FILE."""
    import importlib.util
    docker = (HERE.parent / "Dockerfile").read_text()
    assert all(m in docker for m in ("brand.py", "routing.py", "webhooks.py"))
    spec = importlib.util.spec_from_file_location("deploy_script", HERE.parent / "scripts" / "deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    for var in ("MAIL_SENDER_DOMAINS", "WEBHOOK_ALLOWED_HOSTS", "WEBHOOK_TIMEOUT", "DEFAULT_SITE", "BRAND_LOGO_MAX_BYTES"):
        assert var in deploy.SECRET_KEYS
    assert "WEBHOOK_CA_FILE" not in deploy.KNOWN_VARS
    assert deploy.WEBHOOK_SECRET_RE.match("WEBHOOK_SECRET_FLYWHEEL") and not deploy.WEBHOOK_SECRET_RE.match("WEBHOOK_SECRET_")
