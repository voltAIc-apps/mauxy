"""
GET /api/confirm -- covers happy path (302 to confirmed, tags flipped),
invalid token, expired token, and missing Mautic contact.
"""
import re
from urllib.parse import urlparse, parse_qs

import httpx
import pytest
import respx


CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
EDIT_CONTACT_URL_RE = re.compile(r"https://mautic\.test/api/contacts/\d+/edit$")


def _contact(contact_id, email, *, tags=None):
    return {
        "contacts": {
            str(contact_id): {
                "id": contact_id,
                "fields": {"core": {"email": {"value": email}}},
                "tags": [{"tag": t} for t in (tags or [])],
                "doNotContact": [],
            }
        }
    }


@respx.mock
def test_confirm_happy_path_redirects_to_confirmed(respx_mock, client, token_signer):
    token = token_signer.dumps({"email": "alice@example.com", "locale": "de"})
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_contact(42, "alice@example.com", tags=["newsletter_pending"]),
        )
    )
    edit_route = respx_mock.patch("https://mautic.test/api/contacts/42/edit").mock(
        return_value=httpx.Response(200, json={"contact": {"id": 42}})
    )

    resp = client.get("/api/confirm", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://site.test/de/newsletter/confirmed"
    assert edit_route.called

    # Verify tag flip body
    body = edit_route.calls.last.request.read().decode()
    assert '"newsletter_confirmed"' in body
    assert '"-newsletter_pending"' in body
    # Token field should be cleared
    assert '"confirmation_token": ""' in body or '"confirmation_token":""' in body


@respx.mock
def test_confirm_bad_signature_redirects_to_error(respx_mock, client):
    # Touch the routes so that if code attempts Mautic, we'd notice
    search = respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))

    resp = client.get("/api/confirm", params={"token": "totally-invalid"}, follow_redirects=False)

    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.netloc == "site.test"
    assert location.path == "/en/newsletter/confirm-error"
    assert parse_qs(location.query).get("reason") == ["invalid_or_expired"]
    assert not search.called


@respx.mock
def test_confirm_expired_token_redirects_to_error(respx_mock, client, token_signer, monkeypatch):
    """Simulate expiry by setting CONFIRM_TOKEN_MAX_AGE=0 at the module level."""
    import main as mauxy_main
    monkeypatch.setattr(mauxy_main, "CONFIRM_TOKEN_MAX_AGE", 0)

    token = token_signer.dumps({"email": "alice@example.com", "locale": "en"})

    # Sleep ~1s so ts < now - max_age (0) is trivially true
    import time
    time.sleep(1.1)

    resp = client.get("/api/confirm", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.path == "/en/newsletter/confirm-error"
    assert parse_qs(location.query).get("reason") == ["invalid_or_expired"]


@respx.mock
def test_confirm_contact_missing_redirects_to_error(respx_mock, client, token_signer):
    token = token_signer.dumps({"email": "ghost@example.com", "locale": "en"})
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(200, json={"contacts": {}, "total": 0})
    )

    resp = client.get("/api/confirm", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.path == "/en/newsletter/confirm-error"
    assert parse_qs(location.query).get("reason") == ["contact_missing"]


@respx.mock
def test_confirm_mautic_unreachable_redirects_to_error(respx_mock, client, token_signer):
    token = token_signer.dumps({"email": "alice@example.com", "locale": "en"})
    respx_mock.get(CONTACTS_URL).mock(side_effect=httpx.ConnectError("boom"))

    resp = client.get("/api/confirm", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.path == "/en/newsletter/confirm-error"
    assert parse_qs(location.query).get("reason") == ["service_unavailable"]
