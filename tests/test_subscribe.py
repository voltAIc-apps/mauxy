"""
POST /api/subscribe -- covers honeypot, new-contact happy path, locale email
ID selection, already-confirmed short-circuit, DNC short-circuit, Mautic
search failure (503), and Mautic connection error.
"""
import re

import httpx
import pytest
import respx


CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
NEW_CONTACT_URL = "https://mautic.test/api/contacts/new"
EDIT_CONTACT_URL_RE = re.compile(r"https://mautic\.test/api/contacts/\d+/edit$")
SEND_EMAIL_URL_RE = re.compile(r"https://mautic\.test/api/emails/\d+/contact/\d+/send$")


def _subscribe_payload(**overrides):
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


def _mautic_contact_payload(contact_id, email, *, tags=None, dnc=None):
    return {
        "contacts": {
            str(contact_id): {
                "id": contact_id,
                "fields": {"core": {"email": {"value": email}}},
                "tags": [{"tag": t} for t in (tags or [])],
                "doNotContact": dnc or [],
            }
        }
    }


@respx.mock(assert_all_called=False)
def test_subscribe_honeypot_short_circuits(respx_mock, client):
    """When honeypot is filled, no Mautic call is made and response is success-shaped."""
    resp = client.post("/api/subscribe", json=_subscribe_payload(company_website="x"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_confirmation"}
    # No Mautic routes should have been hit
    assert all(not route.called for route in respx_mock.routes)


@respx.mock
def test_subscribe_new_contact_en_happy_path(respx_mock, client):
    """No existing contact -> creates, tags pending, triggers EN email template."""
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(200, json={"contacts": {}, "total": 0})
    )
    create_route = respx_mock.post(NEW_CONTACT_URL).mock(
        return_value=httpx.Response(
            201,
            json={"contact": {"id": 7, "fields": {"core": {"email": {"value": "alice@example.com"}}}}},
        )
    )
    send_route = respx_mock.post("https://mautic.test/api/emails/11/contact/7/send").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_confirmation"}
    assert create_route.called
    assert send_route.called

    # Body sent to Mautic must include tag + token + metadata
    create_body = create_route.calls.last.request.read().decode()
    assert '"newsletter_pending"' in create_body
    assert '"confirmation_token"' in create_body
    assert '"newsletter_locale"' in create_body
    assert '"newsletter_source"' in create_body


@respx.mock
def test_subscribe_new_contact_de_uses_de_template_id(respx_mock, client):
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(200, json={"contacts": {}, "total": 0})
    )
    respx_mock.post(NEW_CONTACT_URL).mock(
        return_value=httpx.Response(
            201,
            json={"contact": {"id": 9, "fields": {"core": {"email": {"value": "bob@example.com"}}}}},
        )
    )
    de_send = respx_mock.post("https://mautic.test/api/emails/22/contact/9/send").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    resp = client.post(
        "/api/subscribe",
        json=_subscribe_payload(email="bob@example.com", locale="de"),
    )

    assert resp.status_code == 200
    assert de_send.called


@respx.mock
def test_subscribe_already_confirmed_returns_already_subscribed(respx_mock, client):
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_mautic_contact_payload(42, "alice@example.com", tags=["newsletter_confirmed"]),
        )
    )
    create_route = respx_mock.post(NEW_CONTACT_URL).mock(return_value=httpx.Response(201))
    edit_route = respx_mock.patch(EDIT_CONTACT_URL_RE).mock(return_value=httpx.Response(200))
    send_route = respx_mock.post(SEND_EMAIL_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_subscribed"}
    assert not create_route.called
    assert not edit_route.called
    assert not send_route.called


@respx.mock
def test_subscribe_dnc_contact_returns_already_unsubscribed(respx_mock, client):
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_mautic_contact_payload(
                42,
                "alice@example.com",
                dnc=[{"channel": "email", "reason": 1}],
            ),
        )
    )
    create_route = respx_mock.post(NEW_CONTACT_URL).mock(return_value=httpx.Response(201))
    edit_route = respx_mock.patch(EDIT_CONTACT_URL_RE).mock(return_value=httpx.Response(200))
    send_route = respx_mock.post(SEND_EMAIL_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "already_unsubscribed_no_email_sent"}
    assert not create_route.called
    assert not edit_route.called
    assert not send_route.called


@respx.mock
def test_subscribe_mautic_search_5xx_returns_503(respx_mock, client):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(502, text="bad gateway"))

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 503
    assert resp.json() == {"status": "service_unavailable"}


@respx.mock
def test_subscribe_mautic_unreachable_returns_503(respx_mock, client):
    respx_mock.get(CONTACTS_URL).mock(side_effect=httpx.ConnectError("boom"))

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 503
    assert resp.json() == {"status": "service_unavailable"}


@respx.mock
def test_subscribe_existing_unconfirmed_contact_patches_and_sends(respx_mock, client):
    """Existing contact (no confirmed tag, no DNC) -> PATCH edit, then send."""
    respx_mock.get(CONTACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_mautic_contact_payload(42, "alice@example.com"),
        )
    )
    edit_route = respx_mock.patch("https://mautic.test/api/contacts/42/edit").mock(
        return_value=httpx.Response(
            200,
            json={"contact": {"id": 42, "fields": {"core": {"email": {"value": "alice@example.com"}}}}},
        )
    )
    send_route = respx_mock.post("https://mautic.test/api/emails/11/contact/42/send").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    # Make sure POST /contacts/new is NOT called
    create_route = respx_mock.post(NEW_CONTACT_URL).mock(return_value=httpx.Response(201))

    resp = client.post("/api/subscribe", json=_subscribe_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_confirmation"}
    assert edit_route.called
    assert send_route.called
    assert not create_route.called


def test_subscribe_rejects_invalid_email(client):
    resp = client.post(
        "/api/subscribe",
        json=_subscribe_payload(email="not-an-email"),
    )
    assert resp.status_code == 422


def test_subscribe_rejects_unknown_locale(client):
    resp = client.post(
        "/api/subscribe",
        json=_subscribe_payload(locale="fr"),
    )
    assert resp.status_code == 422
