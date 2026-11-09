"""
T-SEC-017: no enumeration through timing (security review 2026-11-08, second finding).

For a new address and an already-confirmed one, /api/subscribe and /api/selfcheck return the
identical response (status, body, header names) BEFORE any Mautic lookup or mail send happened:
the contact-dependent work runs as a job after the response (jobs.py). The mailer is slowed down
to 0.6 s; the responses must come back well below that, and the mail must still arrive afterwards.
"""
import asyncio
import re
import time

import httpx
import pytest
import respx

import jobs
import mailer

CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
ORIGIN = {"Origin": "https://site.test"}
SLOW = 0.6


def _contacts(request):
    """Mautic: alice is new, bob is already confirmed."""
    if "bob%40example.com" in str(request.url) or "bob@example.com" in str(request.url):
        return httpx.Response(200, json={"contacts": {"9": {
            "id": 9, "fields": {"core": {"email": {"value": "bob@example.com"}}},
            "tags": [{"tag": "newsletter_confirmed"}], "doNotContact": []}}})
    return httpx.Response(200, json={"contacts": {}})


@pytest.fixture
def slow_mailer(monkeypatch):
    real = mailer.send

    async def slow(*a, **kw):
        await asyncio.sleep(SLOW)
        return await real(*a, **kw)

    monkeypatch.setattr(mailer, "send", slow)


def _timed(client, path, json, headers=None):
    t0 = time.perf_counter()
    resp = client.request_nodrain("POST", path, json=json, headers=headers or {})
    return resp, time.perf_counter() - t0


def _same(a, b):
    assert a.status_code == b.status_code == 200
    assert a.content == b.content == b'{"status":"pending_confirmation"}'
    assert sorted(k for k in a.headers if k != "date") == sorted(k for k in b.headers if k != "date")


@respx.mock
def test_t_sec_017a_subscribe_identical_and_not_waiting(respx_mock, client, outbox, slow_mailer):
    respx_mock.get(CONTACTS_URL).mock(side_effect=_contacts)
    body = {"locale": "en", "source": "footer", "consent": True, "pageUrl": "https://site.test/en/"}
    new, t_new = _timed(client, "/api/subscribe", {**body, "email": "alice@example.com"})
    known, t_known = _timed(client, "/api/subscribe", {**body, "email": "bob@example.com"})
    _same(new, known)
    assert t_new < SLOW / 2 and t_known < SLOW / 2  # nobody waits for Mautic or the mailer
    client.portal.call(jobs.drain)
    assert [m["to"] for m in outbox] == ["alice@example.com"]  # the mail still went out


@respx.mock(assert_all_called=False)
def test_t_sec_017b_selfcheck_identical_and_not_waiting(respx_mock, client, outbox, slow_mailer):
    body = {"lang": "de", "picks": ["mail"], "answers": {"system": ["ecc"]}, "rules_version": "x", "consent": True,
            "salutation": "none", "name": "Dora Beispiel"}
    first, t1 = _timed(client, "/api/selfcheck", {**body, "email": "dora@example.com"}, ORIGIN)
    client.portal.call(jobs.drain)
    # throttled address (3 earlier requests) must look exactly like a fresh one
    for _ in range(2):
        _timed(client, "/api/selfcheck", {**body, "email": "dora@example.com"}, ORIGIN)
        client.portal.call(jobs.drain)
    throttled, t2 = _timed(client, "/api/selfcheck", {**body, "email": "dora@example.com"}, ORIGIN)
    fresh, t3 = _timed(client, "/api/selfcheck", {**body, "email": "erik@example.com"}, ORIGIN)
    _same(first, throttled)
    _same(throttled, fresh)
    assert max(t1, t2, t3) < SLOW / 2
    client.portal.call(jobs.drain)
    assert [m["to"] for m in outbox].count("dora@example.com") == 3
    assert "erik@example.com" in [m["to"] for m in outbox]
