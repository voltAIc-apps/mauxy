"""
POST /api/subscribe -- newsletter DOI after the 2026-11-08 security review.

T-SEC-001 .. T-SEC-009 (fixes 1, 3, 4 of the review):
  1 user enumeration      -> one uniform {"status": "pending_confirmation"}
  3 consent validation    -> consent must be true; server-side consent timestamp
  4 no CRM change before confirmation; source / page_url / consentedAt validated + limited
Mautic is mocked via respx; mails land in mailer.OUTBOX (MAIL_MODE=memory).
"""
import re

import httpx
import respx
from tests.conftest import click_confirm

CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
ANY_WRITE = re.compile(r"https://mautic\.test/api/(contacts/new|contacts/\d+/edit|emails/.*)$")
PENDING = {"status": "pending_confirmation"}


def payload(**overrides):
    base = {
        "email": "alice@example.com",
        "locale": "en",
        "source": "footer",
        "consent": True,
        "consentedAt": "2026-04-24T10:00:00Z",
        "pageUrl": "https://site.test/en/",
    }
    base.update(overrides)
    return base


def contact(contact_id, email, *, tags=None, dnc=None):
    return {"contacts": {str(contact_id): {
        "id": contact_id,
        "fields": {"core": {"email": {"value": email}}},
        "tags": [{"tag": t} for t in (tags or [])],
        "doNotContact": dnc or [],
    }}}


def no_contact(respx_mock):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}, "total": 0}))


@respx.mock(assert_all_called=False)
def test_t_sec_001_honeypot_uniform_no_calls(respx_mock, client, outbox):
    resp = client.post("/api/subscribe", json=payload(company_website="x"))
    assert resp.status_code == 200 and resp.json() == PENDING
    assert all(not r.called for r in respx_mock.routes)
    assert outbox == []


@respx.mock
def test_t_sec_002_new_address_mails_link_and_writes_nothing_to_mautic(respx_mock, client, outbox, app):
    no_contact(respx_mock)
    writes = respx_mock.route(url__regex=ANY_WRITE.pattern).mock(return_value=httpx.Response(500))

    resp = client.post("/api/subscribe", json=payload())

    assert resp.status_code == 200 and resp.json() == PENDING
    assert not writes.called  # fix 4: Mautic untouched until confirmation
    assert len(outbox) == 1 and outbox[0]["to"] == "alice@example.com"
    assert re.search(r"https://mauxy\.test/api/confirm\?token=\S+", outbox[0]["body"])
    assert "alice" not in outbox[0]["body"].split("token=")[1]  # no e-mail inside the URL


@respx.mock
def test_t_sec_003_already_confirmed_is_indistinguishable(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(
        200, json=contact(42, "alice@example.com", tags=["newsletter_confirmed"])))
    resp = client.post("/api/subscribe", json=payload())
    assert resp.status_code == 200 and resp.json() == PENDING  # fix 1
    assert outbox == []


@respx.mock
def test_t_sec_004_dnc_is_indistinguishable_and_gets_no_mail(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(
        200, json=contact(42, "alice@example.com", dnc=[{"channel": "email", "reason": 1}])))
    resp = client.post("/api/subscribe", json=payload())
    assert resp.status_code == 200 and resp.json() == PENDING  # fix 1
    assert outbox == []


@respx.mock
def test_t_sec_005_existing_unconfirmed_contact_not_patched(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(
        200, json=contact(9, "alice@example.com", tags=["something"])))
    patch = respx_mock.patch(re.compile(r"https://mautic\.test/api/contacts/9/edit$")).mock(
        return_value=httpx.Response(200, json={"contact": {"id": 9}}))
    resp = client.post("/api/subscribe", json=payload())
    assert resp.status_code == 200 and resp.json() == PENDING
    assert not patch.called  # fix 4
    assert len(outbox) == 1


def test_t_sec_006_consent_must_be_true(client):
    assert client.post("/api/subscribe", json=payload(consent=False)).status_code == 422
    body = payload()
    del body["consent"]
    assert client.post("/api/subscribe", json=body).status_code == 422


def test_t_sec_007_fields_validated_and_length_limited(client):
    assert client.post("/api/subscribe", json=payload(pageUrl="https://evil.test/x")).status_code == 422
    assert client.post("/api/subscribe", json=payload(pageUrl="https://site.test.evil.test/")).status_code == 422
    assert client.post("/api/subscribe", json=payload(pageUrl="https://site.test/" + "a" * 600)).status_code == 422
    assert client.post("/api/subscribe", json=payload(source="Footer <script>")).status_code == 422
    assert client.post("/api/subscribe", json=payload(source="a" * 41)).status_code == 422
    assert client.post("/api/subscribe", json=payload(consentedAt="x" * 41)).status_code == 422
    assert client.post("/api/subscribe", json=payload(email="not-an-email")).status_code == 422
    assert client.post("/api/subscribe", json=payload(locale="fr")).status_code == 422


@respx.mock
def test_t_sec_008_client_consent_time_is_not_stored(respx_mock, client, outbox, app):
    no_contact(respx_mock)
    client.post("/api/subscribe", json=payload(consentedAt="1999-01-01T00:00:00Z"))
    token = re.search(r"token=(\S+)", outbox[0]["body"]).group(1)
    create = respx_mock.post("https://mautic.test/api/contacts/new").mock(
        return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    resp = click_confirm(client, f"/api/confirm?token={token}")
    assert resp.status_code == 303 and resp.headers["location"].endswith("/en/newsletter/confirmed")
    sent = create.calls.last.request.read().decode()
    assert "1999-01-01" not in sent and "newsletter_consented_at" in sent  # server time used


@respx.mock
def test_t_sec_009_per_address_throttle_is_silent(respx_mock, client, outbox):
    no_contact(respx_mock)
    for _ in range(4):
        resp = client.post("/api/subscribe", json=payload())
        assert resp.status_code == 200 and resp.json() == PENDING
    assert len(outbox) == 3  # MAX_PENDING_PER_EMAIL


@respx.mock
def test_t_sec_018_mautic_errors_are_not_returned(respx_mock, client, outbox):
    """A 503 would depend on the address being processed -> uniform 200, logged, no mail."""
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(500))
    resp = client.post("/api/subscribe", json=payload())
    assert resp.status_code == 200 and resp.json() == PENDING
    respx_mock.get(CONTACTS_URL).mock(side_effect=httpx.ConnectError("boom"))
    resp = client.post("/api/subscribe", json=payload(email="carol@example.com"))
    assert resp.status_code == 200 and resp.json() == PENDING
    assert outbox == []


@respx.mock
def test_subscribe_de_mail_is_german(respx_mock, client, outbox):
    no_contact(respx_mock)
    client.post("/api/subscribe", json=payload(locale="de", pageUrl="https://site.test/de/"))
    assert outbox[0]["subject"].startswith("Bitte bestätigen")
