"""
GET /api/confirm -- newsletter DOI landing after the 2026-11-08 security review.

T-SEC-010 .. T-SEC-016 (fix 2: token replay / consent bypass):
  signature + max_age AND a matching, unused pending record (SHA-256, constant-time compare)
  AND the address is not DNC; the token is burnt on success (single use).
"""
import re

import httpx
import respx

import main as mauxy_main

CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
NEW_URL = "https://mautic.test/api/contacts/new"
ERR = "https://site.test/en/newsletter/confirm-error"
OK = "https://site.test/en/newsletter/confirmed"


def subscribe(client, respx_mock, outbox, email="alice@example.com"):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}, "total": 0}))
    resp = client.post("/api/subscribe", json={
        "email": email, "locale": "en", "source": "footer", "consent": True, "pageUrl": "https://site.test/en/",
    })
    assert resp.status_code == 200
    return re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)


def confirm(client, token):
    return client.get(f"/api/confirm?token={token}", follow_redirects=False)


@respx.mock
def test_t_sec_010_happy_path_creates_confirmed_contact(respx_mock, client, outbox):
    token = subscribe(client, respx_mock, outbox)
    create = respx_mock.post(NEW_URL).mock(return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    resp = confirm(client, token)
    assert resp.status_code == 302 and resp.headers["location"] == OK
    body = create.calls.last.request.read().decode()
    assert '"newsletter_confirmed"' in body and '"-newsletter_pending"' in body


@respx.mock
def test_t_sec_011_token_is_single_use(respx_mock, client, outbox):
    token = subscribe(client, respx_mock, outbox)
    create = respx_mock.post(NEW_URL).mock(return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    assert confirm(client, token).headers["location"] == OK
    second = confirm(client, token)
    assert second.headers["location"].startswith(ERR)
    assert create.call_count == 1  # replay changed nothing


@respx.mock(assert_all_called=False)
def test_t_sec_012_validly_signed_token_without_pending_record_is_refused(respx_mock, client, token_signer):
    search = respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    legacy = token_signer.dumps({"email": "alice@example.com", "locale": "en"})  # old flow / forged
    unknown = token_signer.dumps({"p": "does-not-exist"})
    for t in (legacy, unknown):
        assert confirm(client, t).headers["location"].startswith(ERR)
    assert not search.called


@respx.mock
def test_t_sec_013_dnc_at_confirmation_is_refused_and_burnt(respx_mock, client, outbox):
    token = subscribe(client, respx_mock, outbox)
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {"5": {
        "id": 5, "fields": {"core": {"email": {"value": "alice@example.com"}}},
        "doNotContact": [{"channel": "email", "reason": 1}], "tags": []}}}))
    edit = respx_mock.patch(re.compile(r".*/contacts/5/edit$")).mock(return_value=httpx.Response(200, json={"contact": {"id": 5}}))
    assert confirm(client, token).headers["location"].startswith(ERR)
    assert not edit.called


@respx.mock(assert_all_called=False)
def test_t_sec_014_bad_signature_and_expired(respx_mock, client, outbox, monkeypatch):
    token = subscribe(client, respx_mock, outbox)
    assert confirm(client, token[:-2] + "xx").headers["location"].startswith(ERR)
    monkeypatch.setattr(mauxy_main, "CONFIRM_TOKEN_MAX_AGE", -1)
    assert confirm(client, token).headers["location"].startswith(ERR)


@respx.mock
def test_t_sec_015_mautic_down_keeps_token_usable(respx_mock, client, outbox):
    token = subscribe(client, respx_mock, outbox)
    respx_mock.get(CONTACTS_URL).mock(side_effect=httpx.ConnectError("down"))
    assert "service_unavailable" in confirm(client, token).headers["location"]
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    respx_mock.post(NEW_URL).mock(return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    assert confirm(client, token).headers["location"] == OK


def test_t_sec_016_admin_key_checked(client):
    assert client.get("/api/actions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/actions", headers={"Authorization": "Bearer admin-test-key"}).status_code == 200
