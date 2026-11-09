"""
Newsletter authority after the merge of upstream main (2026-11-09).

Upstream made mauxy THE newsletter authority for all sites: per-site Bearer key (sites table,
seeded from MAUXY_SITES, admin CRUD) + bot-defence quiz (GET /api/challenge). This branch keeps
those gates in front of its own double opt-in (nothing to any sink before the click) and the
subscriber sinks (Odoo default, Mautic deprecated). Schema: db-patches/ (0003 = our tables).

  T-NL-001..021  gate matrix (tests/cases/newsletter_gate_cases.json)
  T-NL-022..026  challenge endpoint, site + segment + sink through the DOI
  T-NL-030..033  readiness / health / action log
  T-NL-040..046  admin CRUD (sites incl. policy, quiz, Mautic instance)
  T-NL-050..055  db-patches: in-memory schema, strict start, upgrade from upstream 0002, seed, policy
  T-NL-060..066  ludo (doi_owner sink): upstream behaviour unchanged, per-site sink
  T-NL-070       k8s strategy Recreate
Odoo is the in-memory fake; Mautic is mocked with respx. Nothing leaves the process.
"""
import json
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import db_migrate
import jobs
import main as mauxy_main
from tests.conftest import LUDO_KEY, RECLOUD_KEY, SITE_KEY, WEB_KEY, solve_challenge

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "newsletter_gate_cases.json").read_text(encoding="utf-8"))
ADMIN = {"Authorization": "Bearer admin-test-key"}
PAGE = {"locale": "de", "source": "footer", "consent": True, "pageUrl": "https://site.test/de/"}
CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
AUTH = {"site": f"Bearer {SITE_KEY}", "web": f"Bearer {WEB_KEY}", "wrong": "Bearer not-a-site-key-000000",
        "none": None, "basic": "Basic dXNlcjpwYXNz"}
KEYS = {"web": WEB_KEY}


def _challenge(client, kind: str, auth: str) -> dict:
    key = KEYS.get(auth, SITE_KEY)
    send = client.request_raw
    if kind == "missing":
        return {"challenge": "", "answer": -1}
    if kind == "garbage":
        return {"challenge": "a.b.c", "answer": 0}
    if kind == "solved":
        return solve_challenge(client, key, send=send)
    if kind == "wrong_answer":
        ok = solve_challenge(client, key, send=send)
        return {**ok, "answer": ok["answer"] + 1}
    if kind == "other_topic":  # correctly solved, but for another site's topic
        return solve_challenge(client, RECLOUD_KEY, send=send)
    if kind == "expired":
        q = mauxy_main.QUIZ_BANK["simplify-erp"][0]
        return {"challenge": mauxy_main._sign_challenge("simplify-erp", q["id"], int(time.time()) - 1), "answer": q["answer"]}
    if kind == "tampered":
        ok = solve_challenge(client, key, send=send)
        topic, qid, exp, sig = ok["challenge"].split(".")
        other = next(q for q in mauxy_main.QUIZ_BANK[topic] if q["id"] != qid)
        return {"challenge": f"{topic}.{other['id']}.{exp}.{sig}", "answer": other["answer"]}
    raise AssertionError(kind)


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["id"] for c in CASES["cases"]])
def test_gate_matrix(case, client, outbox, odoo):
    odoo.add("mailing.contact", name="x", email="nina@example.com", email_normalized="nina@example.com")
    body = {"email": "nina@example.com"} if case["path"] == "/api/unsubscribe" else {**PAGE, "email": "nina@example.com"}
    body.update(_challenge(client, case["challenge"], case["auth"]))
    if case.get("honeypot"):
        body["company_website"] = "http://spam"
    headers = {"Authorization": AUTH[case["auth"]]} if AUTH[case["auth"]] else {}
    r = client.request_raw("POST", case["path"], json=body, headers=headers)
    client.portal.call(jobs.drain)
    assert r.status_code == case["expect_status"] and r.json() == case["expect_body"]
    assert bool(outbox) == case["expect_mail"]
    # no subscription written before a confirmation, whatever the gate did
    assert odoo.writes_to("mailing.contact") == [] and odoo.writes_to("mailing.subscription") == []
    if case["expect_status"] != 200 or case.get("honeypot"):
        assert not [c for c in odoo.calls if c[0] in ("mailing.subscription", "mail.blacklist")]  # no lookup either


def test_t_nl_022_challenge_endpoint(client):
    assert client.get("/api/challenge").status_code == 401
    assert client.get("/api/challenge", headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code == 401
    r = client.get("/api/challenge", headers={"Authorization": f"Bearer {SITE_KEY}"})
    body = r.json()
    assert r.status_code == 200 and set(body) == {"challenge", "question", "choices"}  # never the answer
    topic, qid, exp, sig = body["challenge"].split(".")
    assert topic == "simplify-erp" and int(exp) > time.time() and len(sig) == 64
    ludo = client.get("/api/challenge", headers={"Authorization": f"Bearer {LUDO_KEY}"}).json()
    assert ludo["challenge"].startswith("ludo.")
    # the quiz-off website key still gets questions (needed for unsubscribe)
    assert client.get("/api/challenge", headers={"Authorization": f"Bearer {WEB_KEY}"}).status_code == 200


def test_t_nl_023_challenge_disabled_without_secret(client, monkeypatch):
    monkeypatch.setattr(mauxy_main, "CHALLENGE_SECRET", "")
    r = client.get("/api/challenge", headers={"Authorization": f"Bearer {SITE_KEY}"})
    assert r.status_code == 503
    assert client.post("/api/subscribe", json={**PAGE, "email": "a@example.com", "challenge": "x.y.1.z", "answer": 0}).status_code == 409


@respx.mock
def test_t_nl_024_mautic_sink_adds_the_site_segment_after_confirm_only(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    segs = respx_mock.get("https://mautic.test/api/segments").mock(
        return_value=httpx.Response(200, json={"lists": {"3": {"id": 3, "alias": "re-cloud"}}}))
    new = respx_mock.post("https://mautic.test/api/contacts/new").mock(
        return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    add = respx_mock.post("https://mautic.test/api/segments/3/contact/7/add").mock(return_value=httpx.Response(200))
    body = {**PAGE, "email": "lu@example.com", **solve_challenge(client, RECLOUD_KEY, send=client.request_raw)}
    r = client.post("/api/subscribe", json=body, headers={"Authorization": f"Bearer {RECLOUD_KEY}"})
    assert r.json() == {"status": "pending_confirmation"}
    assert not new.called and not segs.called and not add.called  # nothing before the click
    row = client.portal.call(_one, client, "SELECT site, segment, sink FROM newsletter_pending")
    assert tuple(row) == ("re-cloud", "re-cloud", "mautic")
    token = re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)
    loc = client.get(f"/api/confirm?token={token}", follow_redirects=False).headers["location"]
    assert loc.endswith("/de/newsletter/confirmed")
    assert new.called and add.called


@respx.mock
def test_t_nl_025_mautic_segment_created_when_missing(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    respx_mock.get("https://mautic.test/api/segments").mock(return_value=httpx.Response(200, json={"lists": {}}))
    created = respx_mock.post("https://mautic.test/api/segments/new").mock(
        return_value=httpx.Response(201, json={"list": {"id": 5}}))
    respx_mock.post("https://mautic.test/api/contacts/new").mock(return_value=httpx.Response(201, json={"contact": {"id": 7}}))
    add = respx_mock.post("https://mautic.test/api/segments/5/contact/7/add").mock(return_value=httpx.Response(200))
    body = {**PAGE, "email": "lu@example.com", **solve_challenge(client, RECLOUD_KEY, send=client.request_raw)}
    client.post("/api/subscribe", json=body, headers={"Authorization": f"Bearer {RECLOUD_KEY}"})
    token = re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)
    client.get(f"/api/confirm?token={token}", follow_redirects=False)
    assert json.loads(created.calls.last.request.read()) == {"name": "re-cloud", "alias": "re-cloud", "isPublished": True}
    assert add.called


def test_t_nl_026_website_contract_public_key_no_quiz_odoo_pinned(client, outbox, odoo, monkeypatch):
    """What simplify-erp.de sends: public site key, NO quiz on subscribe; lands in Odoo although the
    global sink is Mautic (per-site sink), only after the click."""
    monkeypatch.setenv("SUBSCRIBER_SINK", "mautic")  # global fallback != the site's pinned sink
    import sinks
    sinks.reset()
    r = client.request_raw("POST", "/api/subscribe", json={**PAGE, "email": "web@example.com"},
                           headers={"Authorization": f"Bearer {WEB_KEY}"})
    client.portal.call(jobs.drain)
    assert r.json() == {"status": "pending_confirmation"}
    assert odoo.writes_to("mailing.contact") == []
    token = re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)
    client.get(f"/api/confirm?token={token}", follow_redirects=False)
    (contact,) = odoo.db["mailing.contact"].values()
    assert contact["email"] == "web@example.com"
    assert [s["list_id"] for s in odoo.db["mailing.subscription"].values()] == [1]  # ODOO_LIST_NEWSLETTER_ID
    # opt-out of the website: quiz required, Odoo (pinned), not Mautic
    r = client.request_raw("POST", "/api/unsubscribe", json={"email": "web@example.com", **solve_challenge(
        client, WEB_KEY, send=client.request_raw)}, headers={"Authorization": f"Bearer {WEB_KEY}"})
    assert r.json() == {"status": "ok"} and all(s["opt_out"] for s in odoo.db["mailing.subscription"].values())


async def _one(client, sql):
    async with client.app.state.db.execute(sql) as cur:
        return await cur.fetchone()


async def _all(client, sql):
    async with client.app.state.db.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


def test_t_nl_030_ready_is_local_only(client, odoo):
    odoo.fail[("mail.blacklist", "search_read")] = ["http502"] * 5
    r = client.get("/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}
    assert client.get("/health").json() == {"status": "ok"}


def test_t_nl_031_health_detail_reports_gate_and_mautic_version(client, monkeypatch):
    monkeypatch.setattr(mauxy_main, "_sink_health", {"checked_at": -1e9, "detail": {}})
    d = client.get("/health/detail").json()
    assert d["sites"] == 4 and d["quiz"] == "configured" and d["mautic_version"] == "unknown"
    assert SITE_KEY not in json.dumps(d) and "challenge-test-secret" not in json.dumps(d)


def test_t_nl_032_action_log_has_action_and_site(client, outbox, odoo):
    client.post("/api/subscribe", json={**PAGE, "email": "nina@example.com"})
    token = re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)
    client.get(f"/api/confirm?token={token}", follow_redirects=False)
    client.post("/api/unsubscribe", json={"email": "nina@example.com"})
    rows = client.get("/api/actions", headers=ADMIN).json()["actions"]
    got = {(r["action"], r["site"], r["result"]) for r in rows}
    assert {("subscribe", "simplify-erp", "subscribe_ok"), ("confirm", "simplify-erp", "confirm_ok"),
            ("unsubscribe", "simplify-erp", "ok")} <= got


def test_t_nl_033_cors_allows_bearer_and_get(client):
    r = client.options("/api/challenge", headers={
        "Origin": "https://site.test", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://site.test"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


def test_t_nl_040_admin_auth(client, monkeypatch):
    assert client.get("/api/admin/sites").status_code == 401
    assert client.get("/api/admin/sites", headers={"Authorization": f"Bearer {SITE_KEY}"}).status_code == 401
    monkeypatch.setattr(mauxy_main, "ADMIN_API_KEY", "")
    assert client.get("/api/admin/sites", headers=ADMIN).status_code == 403


def test_t_nl_041_admin_site_crud_is_live(client):
    new_key = "new-site-key-abcdefghijkl"
    hdr = {"Authorization": f"Bearer {new_key}"}
    assert client.get("/api/challenge", headers=hdr).status_code == 401
    r = client.post("/api/admin/sites", headers=ADMIN, json={"key": new_key, "site": "re-cloud", "segment": "", "topic": "re-cloud"})
    assert r.status_code == 201
    assert client.post("/api/admin/sites", headers=ADMIN, json={"key": new_key, "site": "x", "topic": "re-cloud"}).status_code == 409
    assert client.post("/api/admin/sites", headers=ADMIN, json={"key": "short", "site": "x", "topic": "re-cloud"}).status_code == 422
    assert client.get("/api/challenge", headers=hdr).json()["challenge"].startswith("re-cloud.")
    assert client.put(f"/api/admin/sites/{new_key}", headers=ADMIN, json={"topic": "odoo"}).status_code == 200
    assert client.get("/api/challenge", headers=hdr).json()["challenge"].startswith("odoo.")
    assert client.put(f"/api/admin/sites/{new_key}", headers=ADMIN, json={}).status_code == 400
    assert client.put("/api/admin/sites/nope", headers=ADMIN, json={"topic": "odoo"}).status_code == 404
    assert client.delete(f"/api/admin/sites/{new_key}", headers=ADMIN).status_code == 200
    assert client.get("/api/challenge", headers=hdr).status_code == 401
    assert client.delete(f"/api/admin/sites/{new_key}", headers=ADMIN).status_code == 404


def test_t_nl_042_admin_quiz_crud(client):
    q = {"topic": "simplify-erp", "id": "neu", "q": "Was ist 2+2?", "choices": ["4", "5"], "answer": 0}
    assert client.post("/api/admin/quiz", headers=ADMIN, json={**q, "answer": 2}).status_code == 400
    assert client.post("/api/admin/quiz", headers=ADMIN, json={**q, "topic": "a.b"}).status_code == 422  # '.' breaks tokens
    assert client.post("/api/admin/quiz", headers=ADMIN, json=q).status_code == 201
    assert client.post("/api/admin/quiz", headers=ADMIN, json=q).status_code == 409
    assert any(x["id"] == "neu" for x in mauxy_main.QUIZ_BANK["simplify-erp"])
    listed = client.get("/api/admin/quiz?topic=simplify-erp", headers=ADMIN).json()["questions"]
    assert {"topic": "simplify-erp", "qid": "neu", "question": "Was ist 2+2?", "choices": ["4", "5"], "answer": 0} in listed
    assert client.put("/api/admin/quiz/simplify-erp/neu", headers=ADMIN, json={**q, "answer": 1}).status_code == 200
    assert [x["answer"] for x in mauxy_main.QUIZ_BANK["simplify-erp"] if x["id"] == "neu"] == [1]
    assert client.delete("/api/admin/quiz/simplify-erp/neu", headers=ADMIN).status_code == 200
    assert client.delete("/api/admin/quiz/simplify-erp/neu", headers=ADMIN).status_code == 404


def test_t_nl_043_admin_mautic_instance(client, monkeypatch):
    monkeypatch.setattr(mauxy_main, "_sink_health", {"checked_at": -1e9, "detail": {}})
    assert client.get("/api/admin/mautic-instance", headers=ADMIN).json()["mautic_instance"]["source"] == "unset"
    assert client.put("/api/admin/mautic-instance", headers=ADMIN, json={"version": "5.2.1"}).status_code == 200
    assert mauxy_main.get_mautic_version() == "5.2.1"
    assert client.get("/health/detail").json()["mautic_version"] == "5.2.1"


def test_t_nl_044_admin_endpoints_never_leak_without_key(client):
    for method, path in (("GET", "/api/admin/quiz"), ("GET", "/api/admin/mautic-instance"),
                         ("DELETE", "/api/admin/sites/x"), ("GET", "/api/actions")):
        r = client.request(method, path)
        assert r.status_code == 401 and SITE_KEY not in r.text


def test_t_nl_050_in_memory_db_has_all_patches(client):
    rows = client.portal.call(_all, client, "SELECT version FROM schema_migrations ORDER BY version")
    assert [r[0] for r in rows] == [v for v, _, _ in db_migrate.list_patches()]
    tables = {r[0] for r in client.portal.call(_all, client, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sites", "quiz_questions", "mautic_instance", "action_log", "newsletter_pending", "selfcheck_submission",
            "selfcheck_counter", "lead_submission", "lead_sync", "auth_state", "auth_code"} <= tables


def test_t_nl_051_file_db_must_be_migrated_first(tmp_path, monkeypatch):
    path = tmp_path / "actions.db"
    monkeypatch.setattr(mauxy_main, "ACTION_LOG_DB", str(path))
    with pytest.raises(RuntimeError, match="sqlite_db.py --migrate"):
        with TestClient(mauxy_main.app):
            pass
    assert db_migrate.apply_pending(path) == [v for v, _, _ in db_migrate.list_patches()]
    assert db_migrate.apply_pending(path) == []  # idempotent
    with TestClient(mauxy_main.app) as c:
        assert c.get("/ready").status_code == 200
        assert set(mauxy_main._SITES_BY_KEY) == {SITE_KEY, WEB_KEY, LUDO_KEY, RECLOUD_KEY}


def test_t_nl_052_upgrade_from_upstream_0002_applies_ours(tmp_path):
    path = tmp_path / "prod.db"
    conn = sqlite3.connect(path)
    db_migrate.ensure_migrations_table(conn)
    for v, n, p in db_migrate.list_patches()[:2]:  # what upstream main deployed
        conn.executescript(p.read_text())
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?, 'x')", (v, n))
    for key, site in (("k-ludo", "ludo"), ("k-simplify", "simplify-erp"), ("k-recloud", "re-cloud")):
        conn.execute("INSERT INTO sites (key, site, segment, topic) VALUES (?, ?, ?, ?)", (key, site, site, site))
    conn.commit()
    conn.close()
    later = [(v, n) for v, n, _ in db_migrate.list_patches()[2:]]
    assert later[:3] == [(3, "website_consent"), (4, "site_policy"), (5, "mail_outbox")]
    assert db_migrate.check(path)["pending"] == later
    assert db_migrate.apply_pending(path) == [v for v, _ in later]
    conn = sqlite3.connect(path)
    policy = {r[0]: r[1:] for r in conn.execute(
        "SELECT key, subscribe_quiz, subscriber_sink, doi_owner FROM sites")}  # data kept, policy set
    assert policy == {"k-ludo": (1, "mautic", "sink"), "k-recloud": (1, "mautic", "sink"),
                      "k-simplify": (0, "odoo", "mauxy")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(newsletter_pending)")}
    assert {"site", "segment", "token_hash", "used_at"} <= cols
    conn.close()


def test_t_nl_053_seed_only_into_an_empty_sites_table(tmp_path, monkeypatch):
    path = tmp_path / "actions.db"
    db_migrate.apply_pending(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sites (key, site, segment, topic) VALUES ('admin-made-key-0000000', 'x', '', 'odoo')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mauxy_main, "ACTION_LOG_DB", str(path))
    with TestClient(mauxy_main.app):
        assert set(mauxy_main._SITES_BY_KEY) == {"admin-made-key-0000000"}  # DB is authoritative


def test_t_nl_054_seed_skips_invalid_policy(monkeypatch, caplog):
    monkeypatch.setenv("MAUXY_SITES", json.dumps([
        {"key": "k1-0000000000000000", "site": "a", "topic": "odoo", "doi_owner": "sink", "subscriber_sink": "odoo"},
        {"key": "k2-0000000000000000", "site": "b", "topic": "odoo", "subscriber_sink": "ftp"},
        {"key": "k3-0000000000000000", "site": "c", "topic": "odoo", "subscribe_quiz": False}]))
    seeded = mauxy_main._parse_seed_sites()
    assert [(s["site"], s["subscribe_quiz"], s["subscriber_sink"], s["doi_owner"]) for s in seeded] == [("c", 0, None, "mauxy")]
    assert "SECRET" not in caplog.text and "k1-0000" not in caplog.text  # keys never logged


def test_t_nl_055_policy_error_rules(monkeypatch):
    pe = mauxy_main.policy_error
    assert pe(None, "mauxy") is None and pe("odoo", "mauxy") is None and pe("mautic", "sink") is None
    assert pe("odoo", "sink") and pe("none", "sink") and pe("x", "mauxy") and pe(None, "x")
    monkeypatch.setenv("SUBSCRIBER_SINK", "mautic")
    assert pe(None, "sink") is None  # global fallback counts
    monkeypatch.setenv("SUBSCRIBER_SINK", "odoo")
    assert pe(None, "sink")


def test_t_nl_045_admin_site_policy(client):
    key = "policy-site-key-abcdefgh"
    bad = {"key": key, "site": "p", "topic": "odoo", "doi_owner": "sink", "subscriber_sink": "odoo"}
    assert client.post("/api/admin/sites", headers=ADMIN, json=bad).status_code == 400
    assert client.post("/api/admin/sites", headers=ADMIN, json={**bad, "subscriber_sink": "ftp"}).status_code == 422
    ok = {**bad, "subscriber_sink": "mautic", "subscribe_quiz": False}
    assert client.post("/api/admin/sites", headers=ADMIN, json=ok).status_code == 201
    assert mauxy_main._SITES_BY_KEY[key] == {"site": "p", "segment": "", "topic": "odoo", "subscribe_quiz": False,
                                             "sink": "mautic", "doi_owner": "sink"}
    # switching the sink to odoo while the DOI stays with the sink would write unconfirmed addresses
    assert client.put(f"/api/admin/sites/{key}", headers=ADMIN, json={"subscriber_sink": "odoo"}).status_code == 400
    r = client.put(f"/api/admin/sites/{key}", headers=ADMIN, json={"subscriber_sink": "odoo", "doi_owner": "mauxy"})
    assert r.status_code == 200 and mauxy_main._SITES_BY_KEY[key]["sink"] == "odoo"
    assert client.put(f"/api/admin/sites/{key}", headers=ADMIN, json={"subscriber_sink": "", "subscribe_quiz": True}).status_code == 200
    assert mauxy_main._SITES_BY_KEY[key]["sink"] is None and mauxy_main._SITES_BY_KEY[key]["subscribe_quiz"] is True
    listed = {r["key"]: r for r in client.get("/api/admin/sites", headers=ADMIN).json()["sites"]}
    assert listed[WEB_KEY]["subscribe_quiz"] == 0 and listed[LUDO_KEY]["doi_owner"] == "sink"


def test_t_nl_046_no_global_unauthenticated_path(client):
    """No site key -> 401 on every newsletter write, whatever the per-site switches say."""
    for path, body in (("/api/subscribe", {**PAGE, "email": "x@example.com"}), ("/api/unsubscribe", {"email": "x@example.com"})):
        assert client.request_raw("POST", path, json=body).status_code == 401


# ---------------------------------------------------------------------------------- ludo (doi_owner sink)

LUDO = {"Authorization": f"Bearer {LUDO_KEY}"}


def _ludo_mautic(respx_mock, *, search=200, create=201, segs=200, add=200):
    m = {}
    m["search"] = respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(search, json={"contacts": {}}))
    m["create"] = respx_mock.post("https://mautic.test/api/contacts/new").mock(
        return_value=httpx.Response(create, json={"contact": {"id": 11}}))
    m["segs"] = respx_mock.get("https://mautic.test/api/segments").mock(
        return_value=httpx.Response(segs, json={"lists": {"4": {"id": 4, "alias": "ludo"}}}))
    m["add"] = respx_mock.post("https://mautic.test/api/segments/4/contact/11/add").mock(return_value=httpx.Response(add))
    return m


@respx.mock
def test_t_nl_060_ludo_upstream_flow_unchanged(respx_mock, client, outbox, odoo):
    """Upstream body (email + quiz only), contact into the ludo segment AT ONCE, Mautic runs the
    opt-in: no mauxy DOI mail, no pending row -- even with the global sink on Odoo."""
    m = _ludo_mautic(respx_mock)
    body = {"email": "Lu@Example.com", **solve_challenge(client, LUDO_KEY, send=client.request_raw)}
    r = client.request_raw("POST", "/api/subscribe", json=body, headers=LUDO)
    assert r.status_code == 200 and r.json() == {"status": "pending_confirmation"}
    assert m["create"].called and m["add"].called
    assert json.loads(m["create"].calls.last.request.read()) == {"email": "lu@example.com"}
    assert outbox == [] and odoo.calls == []
    assert client.portal.call(_all, client, "SELECT * FROM newsletter_pending") == []


@pytest.mark.parametrize("kw,status,body", [
    ({"search": 500}, 503, {"status": "service_unavailable"}),
    ({"create": 500}, 502, {"status": "error"}),
    ({"segs": 500}, 502, {"status": "error"}),
    ({"add": 500}, 502, {"status": "error"}),
], ids=["T-NL-061-search", "T-NL-062-create", "T-NL-063-segments", "T-NL-064-add"])
def test_t_nl_061_064_ludo_upstream_error_codes(client, kw, status, body):
    with respx.mock(assert_all_called=False) as respx_mock:
        _ludo_mautic(respx_mock, **kw)
        r = client.request_raw("POST", "/api/subscribe", json={"email": "lu@example.com", **solve_challenge(
            client, LUDO_KEY, send=client.request_raw)}, headers=LUDO)
    assert r.status_code == status and r.json() == body


@respx.mock(assert_all_called=False)
def test_t_nl_065_ludo_keeps_quiz_and_lenient_body(respx_mock, client):
    m = _ludo_mautic(respx_mock)
    r = client.request_raw("POST", "/api/subscribe", json={"email": "lu@example.com"}, headers=LUDO)
    assert r.status_code == 409 and not m["search"].called  # quiz mandatory for doi_owner sink
    r = client.request_raw("POST", "/api/subscribe", json={"email": "lu@example.com", "answer": 0, "challenge": "",
                                                            "company_website": "spam"}, headers=LUDO)
    assert r.json() == {"status": "pending_confirmation"} and not m["search"].called  # honeypot
    assert client.request_raw("POST", "/api/subscribe", json={"challenge": "x"}, headers=LUDO).status_code == 422
    assert client.request_raw("POST", "/api/subscribe", content=b"not json", headers=LUDO).status_code == 422


@respx.mock
def test_t_nl_066_ludo_unsubscribe_goes_to_mautic_dnc_although_global_is_odoo(respx_mock, client, odoo):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {"11": {
        "id": 11, "fields": {"core": {"email": {"value": "lu@example.com"}}}, "tags": [], "doNotContact": []}}}))
    dnc = respx_mock.post("https://mautic.test/api/contacts/11/dnc/email/add").mock(return_value=httpx.Response(200))
    r = client.request_raw("POST", "/api/unsubscribe", json={"email": "lu@example.com", **solve_challenge(
        client, LUDO_KEY, send=client.request_raw)}, headers=LUDO)
    assert r.json() == {"status": "ok"} and dnc.called and odoo.calls == []


def test_t_nl_070_deployment_strategy_recreate():
    text = (HERE.parent / "k8s" / "deployment.yaml").read_text()
    spec = text.split("template:")[0]
    assert re.search(r"strategy:\s*\n\s*type: Recreate", spec)
    assert "rollingUpdate" not in spec and "replicas: 1" in spec
