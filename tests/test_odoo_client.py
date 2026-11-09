"""
Odoo client (odoo_client.py, SPEC-021): T-ODOO-001 .. T-ODOO-008. Fake Odoo only.

  001 config: https only, all four credentials required
  002 client-side allowlist: no write/unlink on res.partner / crm.lead, opt_out-only writes
  003 login refused -> permanent; uid cached (one authenticate)
  004 error classification transient / permanent
  005 the API key never shows up in exceptions, repr or logs
  006 TLS verification on, no plain http
  007 guard runs before any request leaves
  008 JSON-RPC payload shape (service object, execute_kw with db / uid / key)
"""
import asyncio
import json
import logging

import httpx
import pytest

import odoo_client
from odoo_client import OdooClient, OdooPermanent, OdooRefused, OdooTransient
from tests.fake_odoo import KEY, FakeOdoo


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fake(monkeypatch):
    f = FakeOdoo()
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(f.handle))
    return f


def client(key=KEY):
    return OdooClient("https://odoo.test", "test-db", "website-bot", key)


def test_t_odoo_001_config_https_and_credentials():
    with pytest.raises(OdooPermanent, match="https"):
        OdooClient("http://odoo.test", "db", "u", "k")
    for args in (("", "db", "u", "k"), ("https://x", "", "u", "k"), ("https://x", "db", "", "k"), ("https://x", "db", "u", "")):
        with pytest.raises(OdooPermanent):
            OdooClient(*args)


@pytest.mark.parametrize("model,method,args", [
    ("res.partner", "write", [[1], {"email": "x@y.de"}]),
    ("res.partner", "create", [{"name": "x"}]),
    ("res.partner", "unlink", [[1]]),
    ("crm.lead", "write", [[1], {"stage_id": 2}]),
    ("crm.lead", "unlink", [[1]]),
    ("crm.lead", "merge_opportunity", [[1, 2]]),
    ("mailing.subscription", "write", [[1], {"opt_out": True, "list_id": 3}]),
    ("mailing.subscription", "write", [[1], {}]),
    ("mailing.contact", "write", [[1], {"email": "x@y.de"}]),
    ("utm.campaign", "create", [{"name": "x"}]),
    ("res.users", "search_read", [[]]),
])
def test_t_odoo_002_allowlist(fake, model, method, args):
    with pytest.raises(OdooRefused):
        run(client().execute(model, method, args))
    assert fake.calls == []


def test_t_odoo_003_login_refused_and_uid_cached(fake):
    with pytest.raises(OdooPermanent, match="login refused"):
        run(client(key="wrong").uid())
    c = client()

    async def two():
        await c.search_read("utm.source", [], ["name"])
        await c.search_read("utm.medium", [], ["name"])
    before = fake.authenticates
    run(two())
    assert fake.authenticates - before == 1


@pytest.mark.parametrize("failure,exc", [
    ("http502", OdooTransient), ("timeout", OdooTransient), ("ConcurrencyError", OdooTransient),
    ("AccessError", OdooPermanent), ("ValidationError", OdooPermanent),
])
def test_t_odoo_004_error_classification(fake, failure, exc):
    fake.fail[("crm.lead", "create")] = [failure]
    with pytest.raises(exc):
        run(client().execute("crm.lead", "create", [{"name": "x"}]))


def test_t_odoo_004b_http_status_classes(monkeypatch):
    for status, exc in ((503, OdooTransient), (429, OdooTransient), (403, OdooPermanent), (404, OdooPermanent)):
        monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(lambda r, s=status: httpx.Response(s)))
        with pytest.raises(exc):
            run(client().version())
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    with pytest.raises(OdooTransient):
        run(client().version())


def test_t_odoo_005_key_never_leaks(fake, caplog):
    caplog.set_level(logging.DEBUG)
    c = client()
    assert KEY not in repr(c)
    fake.fail[("crm.lead", "create")] = ["AccessError"]
    with pytest.raises(OdooPermanent) as ei:
        run(c.execute("crm.lead", "create", [{"name": "x"}]))
    assert KEY not in str(ei.value)
    with pytest.raises(OdooPermanent) as ei:
        run(client(key="secret-wrong-key").uid())
    assert "secret-wrong-key" not in str(ei.value)
    assert KEY not in caplog.text and "secret-wrong-key" not in caplog.text


def test_t_odoo_006_tls_verification_on(monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)

    f = FakeOdoo()
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(f.handle))
    monkeypatch.setattr(odoo_client.httpx, "AsyncClient", Spy)
    run(client().version())
    assert seen["verify"] is True


def test_t_odoo_007_guard_before_network(monkeypatch):
    def boom(request):
        raise AssertionError("no request may leave for a refused call")
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(OdooRefused):
        run(client().execute("res.partner", "write", [[1], {"name": "x"}]))


def test_t_odoo_008_payload_shape(monkeypatch):
    bodies = []
    f = FakeOdoo()

    def spy(request):
        bodies.append(json.loads(request.content))
        return f.handle(request)
    monkeypatch.setattr(odoo_client, "TRANSPORT", httpx.MockTransport(spy))
    run(client().search_read("utm.source", [["name", "=", "simplify-erp.de"]], ["id"], limit=1))
    call = bodies[-1]["params"]
    assert call["service"] == "object" and call["method"] == "execute_kw"
    db, uid, key, model, method, args, kw = call["args"]
    assert (db, model, method) == ("test-db", "utm.source", "search_read")
    assert kw["limit"] == 1 and kw["context"] == {"active_test": True}
