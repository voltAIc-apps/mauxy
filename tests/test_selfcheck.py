"""
Self-check endpoints (flywheel SPEC-020): T-SC-101 .. T-SC-150.

  T-SC-101..117  submit validation (tests/cases/selfcheck_api_cases.json)
  T-SC-120..128  double opt-in flow (one DOI for all picks, result mail, Radar, notification)
  T-SC-130..133  anonymous counter: no IP, no identifier
  T-SC-140       admin endpoints
  T-SC-150       vendored rules engine unchanged (sha256 vs SOURCE.json)
  T-SC-160..183  name / phone validation (cases in selfcheck_api_cases.json, owner request 2026-11-09)
  T-SC-190..199  name / phone delivery: crm.lead fields, result mail greeting (plain text, literal), operator mail,
                 rollout flag SELFCHECK_NAME_REQUIRED, /api/lead alias, admin, db-patches/0010
"""
import copy
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

import main as mauxy_main
import selfcheck_api
from tests.conftest import click_confirm, drain, is_neutral

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))
ORIGIN = {"Origin": "https://site.test"}
CONTACTS_URL = re.compile(r"https://mautic\.test/api/contacts(\?.*)?$")
MAUTIC_ANY = re.compile(r"https://mautic\.test/.*")
ADMIN = {"Authorization": "Bearer admin-test-key"}

COMPLETE = {
    "system": ["ecc", "excel"], "why": ["eol", "duty"], "s4": "no", "when": "12", "users": "150",
    "areas": ["fibu", "einkauf", "lager", "verkauf", "produktion"], "entities": "3", "custom": "many",
    "ifaces": ["datev", "bank", "edi", "lohn"], "data": "partly", "history": "2y", "team": "one", "hosting": "cloud",
}


def _expand(v):
    """'{x500}' -> 'x' * 500 (keeps the case file readable)."""
    m = re.fullmatch(r"\{x(\d+)\}", v) if isinstance(v, str) else None
    return "x" * int(m.group(1)) if m else v


def body(**overrides):
    b = copy.deepcopy(CASES["base"])
    b.update({k: _expand(v) for k, v in overrides.items()})
    return b


async def _rows(app, sql, params=()):
    async with app.state.db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


def rows(client, sql, params=()):
    return client.portal.call(_rows, client.app, sql, params)


def token_of(mail):
    return re.search(r"/api/selfcheck/confirm\?token=(\S+)", mail["body"]).group(1)


# ------------------------------------------------------------------ T-SC-101..117 validation


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["id"] for c in CASES["cases"]])
@respx.mock(assert_all_called=False)
def test_submit_validation(case, respx_mock, client, outbox, monkeypatch):
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_NAME_REQUIRED", case.get("flag", True))
    mautic = respx_mock.route(url__regex=MAUTIC_ANY.pattern).mock(return_value=httpx.Response(500))
    b = body(**case.get("set", {}))
    for k in case.get("delete", []):
        b.pop(k, None)
    origin = case.get("origin", "https://site.test")
    headers = {"Origin": origin} if origin else {}
    resp = client.post("/api/selfcheck", json=b, headers=headers)
    assert resp.status_code == case["expect_status"], resp.text
    if resp.status_code == 200:
        assert resp.json() == {"status": "pending_confirmation"}
    assert (len(outbox) == 1) == case["expect_mail"]
    assert bool(rows(client, "SELECT id FROM selfcheck_submission")) == case["expect_row"]
    assert not mautic.called  # never any Mautic call before confirmation
    if "expect_stored" in case:
        cols = ", ".join(case["expect_stored"])
        assert rows(client, f"SELECT {cols} FROM selfcheck_submission")[0] == case["expect_stored"]


# ------------------------------------------------------------------ T-SC-120..128 DOI flow


@respx.mock
def test_t_sc_120_one_doi_then_result_radar_and_notification(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {}}))
    create = respx_mock.post("https://mautic.test/api/contacts/new").mock(
        return_value=httpx.Response(201, json={"contact": {"id": 77}}))

    resp = client.post("/api/selfcheck", headers=ORIGIN, json=body(
        answers=COMPLETE, stay={"licence": 120000, "support": 30000, "offer": 0, "users": 60}))
    assert resp.status_code == 200
    assert len(outbox) == 1 and outbox[0]["to"] == "bob@example.com"
    doi = outbox[0]["body"]
    assert "Ergebnis und Richtangebot" in doi and "30 Minuten Kennenlernen" in doi and "Migration Radar" in doi
    assert not create.called  # nothing in Mautic before the click

    r = click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    assert r.status_code == 303 and r.headers["location"] == "https://site.test/de/selbst-check?bestaetigt=1"

    result = next(m for m in outbox[1:] if m["to"] == "bob@example.com")
    assert "Der Wechsel ist machbar" in result["body"]
    assert "https://site.test/de/wissen/" not in result["body"]  # article links use the rules website
    assert "https://simplify-erp.de/de/wissen/ist-prozesse-aufnehmen-ohne-sie-im-neuen-system-nachzubauen" in result["body"]
    assert "750.000 €" in result["body"]
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Kennenlernen (Meetly): ja" in note["body"] and "Richtangebot: ja" in note["body"]
    assert "Migration Radar: ok" in note["body"] and note["reply_to"] == "bob@example.com"
    sent = create.calls.last.request.read().decode()
    assert "migration_radar_confirmed" in sent and '"newsletter_source": "selfcheck"' in sent.replace('":"', '": "')
    row = rows(client, "SELECT status, token_hash, mail_sent, notify_sent, radar_result FROM selfcheck_submission")[0]
    assert row == {"status": "confirmed", "token_hash": None, "mail_sent": 1, "notify_sent": 1, "radar_result": "ok"}


@respx.mock(assert_all_called=False)
def test_t_sc_121_confirm_link_single_use(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"]))
    token = token_of(outbox[0])
    assert click_confirm(client, f"/api/selfcheck/confirm?token={token}").headers["location"].endswith("=1")
    n = len(outbox)
    again = click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    assert is_neutral(again)
    assert len(outbox) == n  # no second round of mails


@respx.mock
def test_t_sc_122_radar_skipped_for_dnc(respx_mock, client, outbox):
    respx_mock.get(CONTACTS_URL).mock(return_value=httpx.Response(200, json={"contacts": {"5": {
        "id": 5, "fields": {"core": {"email": {"value": "bob@example.com"}}},
        "doNotContact": [{"channel": "email", "reason": 1}], "tags": []}}}))
    edit = respx_mock.patch(re.compile(r".*/contacts/5/edit$")).mock(return_value=httpx.Response(200, json={"contact": {"id": 5}}))
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["radar"]))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    assert not edit.called
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Migration Radar: dnc_skip" in note["body"]
    assert not any(m["to"] == "bob@example.com" and "Ergebnis" in m["subject"] for m in outbox[1:])


@respx.mock(assert_all_called=False)
def test_t_sc_123_call_only_no_result_mail_no_mautic(respx_mock, client, outbox):
    mautic = respx_mock.route(url__regex=MAUTIC_ANY.pattern).mock(return_value=httpx.Response(500))
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["call"], answers=COMPLETE))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    assert [m["to"] for m in outbox] == ["bob@example.com", "owner@site.test"]  # DOI + notification
    assert "Kennenlernen (Meetly): ja" in outbox[1]["body"] and "Richtangebot: nein" in outbox[1]["body"]
    assert not mautic.called


@respx.mock(assert_all_called=False)
def test_t_sc_124_rough_quote_from_exit_screen(respx_mock, client, outbox):
    partial = {"system": ["ecc"], "why": ["eol", "duty"], "s4": "no", "when": "12", "users": "150"}
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], answers=partial, lang="en"))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    result = outbox[1]
    assert result["subject"] == "Your self-check: result"
    assert "You answered 5 of 13 questions" in result["body"] and "Traffic lights" not in result["body"]
    note = outbox[2]
    assert "5 von 13 Antworten, Genauigkeit grob" in note["body"]


@respx.mock(assert_all_called=False)
def test_t_sc_125_throttle_per_address_is_silent(respx_mock, client, outbox):
    for _ in range(4):
        assert client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"])).status_code == 200
    assert len(outbox) == 3


@respx.mock(assert_all_called=False)
def test_t_sc_126_foreign_tokens_refused(respx_mock, client, outbox, token_signer):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"]))
    sid = rows(client, "SELECT id FROM selfcheck_submission")[0]["id"]
    newsletter_token = token_signer.dumps({"s": sid})  # right payload, newsletter salt
    for t in (newsletter_token, token_of(outbox[0])[:-3] + "abc"):
        assert is_neutral(click_confirm(client, f"/api/selfcheck/confirm?token={t}"))
    assert len(outbox) == 1


def test_t_sc_127_retention_purge(client):
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    very_old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(timespec="seconds")
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def seed(app):
        db = app.state.db
        ins = ("INSERT INTO selfcheck_submission (created_at, email, lang, picks, answers, rules_version, complete, status, confirmed_at) "
               "VALUES (?, 'x@example.com', 'de', '[]', '{}', 'v', 0, ?, ?)")
        await db.execute(ins, (old, "pending", None))
        await db.execute(ins, (recent, "pending", None))
        await db.execute(ins, (very_old, "confirmed", very_old))
        await db.execute(ins, (old, "confirmed", old))
        await db.commit()
        await selfcheck_api.purge(db)

    client.portal.call(seed, client.app)
    left = rows(client, "SELECT status, created_at FROM selfcheck_submission ORDER BY id")
    assert left == [{"status": "pending", "created_at": recent}, {"status": "confirmed", "created_at": old}]


@respx.mock(assert_all_called=False)
def test_t_sc_128_hidden_answers_not_stored(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(answers={"system": ["b1"], "s4": "yes"}))
    stored = json.loads(rows(client, "SELECT answers FROM selfcheck_submission")[0]["answers"])
    assert stored == {"system": ["b1"]}


# ------------------------------------------------------------------ T-SC-130..133 counters


def test_t_sc_130_event_counts_without_identifiers(client):
    for ev in ("started", "started", "completed"):
        r = client.post("/api/selfcheck/event", headers=ORIGIN, json={"event": ev, "lang": "de"})
        assert r.status_code == 204
    counters = rows(client, "SELECT * FROM selfcheck_counter ORDER BY event")
    assert [(c["event"], c["count"]) for c in counters] == [("completed", 1), ("started", 2)]
    assert set(counters[0]) == {"day", "event", "lang", "count"}  # schema has no room for an IP
    assert "set-cookie" not in {k.lower() for k in r.headers}
    # nothing about the request landed in the action log either
    assert rows(client, "SELECT * FROM action_log") == []


def test_t_sc_131_event_rejects_foreign_origin_and_unknown_event(client):
    assert client.post("/api/selfcheck/event", headers={"Origin": "https://evil.test"}, json={"event": "started"}).status_code == 403
    assert client.post("/api/selfcheck/event", headers=ORIGIN, json={"event": "clicked"}).status_code == 422
    assert client.post("/api/selfcheck/event", headers=ORIGIN, json={"event": "started", "uid": "abc"}).status_code == 422


def test_t_sc_132_access_log_drops_event_requests():
    log = logging.getLogger("uvicorn.access")
    rec = lambda path: logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("203.0.113.9:5", "POST", path, "1.1", 204), None)  # noqa: E731
    assert all(f.filter(rec("/api/selfcheck/event")) is False for f in log.filters)
    assert all(f.filter(rec("/api/selfcheck")) is True for f in log.filters)


def test_t_sc_133_event_rate_limit_key_is_global():
    assert selfcheck_api._global_key() == "selfcheck-event"
    assert selfcheck_api._global_key(object()) == "selfcheck-event"


# ------------------------------------------------------------------ T-SC-140 admin


@respx.mock(assert_all_called=False)
def test_t_sc_140_admin_endpoints(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], answers=COMPLETE))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    client.post("/api/selfcheck/event", headers=ORIGIN, json={"event": "completed", "lang": "de"})
    assert client.get("/api/selfcheck/submissions").status_code == 401
    assert client.get("/api/selfcheck/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    subs = client.get("/api/selfcheck/submissions", headers=ADMIN).json()
    assert subs["count"] == 1 and subs["submissions"][0]["answers"] == COMPLETE and subs["submissions"][0]["picks"] == ["mail"]
    assert "token_hash" not in subs["submissions"][0]
    assert client.get("/api/selfcheck/stats", headers=ADMIN).json()["totals"] == {"completed": 1}


# ------------------------------------------------------------------ T-SC-150 vendored engine


def test_t_sc_150_vendored_files_untouched():
    vendor = HERE.parent / "vendor" / "selfcheck"
    src = json.loads((vendor / "SOURCE.json").read_text())
    for name, digest in src["sha256"].items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, f"{name} edited by hand -- sync from flywheel"
    assert selfcheck_api.RULES["version"] == src["rules_version"]
    assert mauxy_main.app is not None


# ------------------------------------------------------------------ T-SC-190..199 name / phone delivery

PHONE = "+49 (0)30 / 123-45"


def _confirm(client, outbox):
    r = click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    drain(client)
    return r


def _mail_to(outbox, to, subject_part):
    return next(m for m in outbox if m["to"] == to and subject_part in m["subject"])


def test_t_sc_190_crm_lead_gets_contact_name_and_phone(client, outbox, odoo):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail", "call"], answers=COMPLETE,
                                                            company="Muster GmbH", phone=PHONE))
    assert odoo.calls == []  # nothing in Odoo before the click
    _confirm(client, outbox)
    (lead,) = odoo.leads()
    assert lead["contact_name"] == "Bob Beispiel" and lead["phone"] == PHONE
    assert lead["partner_name"] == "Muster GmbH" and lead["email_from"] == "bob@example.com"
    assert lead["name"].endswith("– Muster GmbH")  # company still names the lead
    assert "Name: Bob Beispiel" in lead["description"] and f"Telefon: {PHONE}" in lead["description"]


def test_t_sc_191_without_company_the_name_titles_the_lead(client, outbox, odoo):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["call"], answers=COMPLETE))
    _confirm(client, outbox)
    (lead,) = odoo.leads()
    assert lead["name"].endswith("– Bob Beispiel") and lead["contact_name"] == "Bob Beispiel"
    assert "phone" not in lead and "partner_name" not in lead  # nothing invented


def test_t_sc_192_known_partner_name_but_no_phone_write(client, outbox, odoo):
    """Linked partner (exactly one with this e-mail): contact_name set; phone stays off the lead (crm.lead's
    inverse would write it onto the existing partner, sinks.py), it is in the description."""
    odoo.add("res.partner", id=81, name="Bob Alt", email="bob@example.com")
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["call"], answers=COMPLETE, phone=PHONE))
    _confirm(client, outbox)
    (lead,) = odoo.leads()
    assert lead["partner_id"] == 81 and lead["contact_name"] == "Bob Beispiel"
    assert "phone" not in lead and f"Telefon: {PHONE}" in lead["description"]


@pytest.mark.parametrize("lang,greeting,neutral", [("de", "Guten Tag Bob Beispiel,\n\ndanke", "Guten Tag,"),
                                                    ("en", "Hello Bob Beispiel,\n\nthank you", "Hello,")])
@respx.mock(assert_all_called=False)
def test_t_sc_193_result_mail_greets_by_name(respx_mock, client, outbox, lang, greeting, neutral):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], lang=lang, phone=PHONE))
    doi = outbox[0]["body"]
    assert doi.startswith(neutral + "\n") and "Bob" not in doi and PHONE not in doi  # DOI: server text only
    _confirm(client, outbox)
    result = outbox[1]
    assert result["to"] == "bob@example.com" and result["body"].startswith(greeting)
    assert PHONE not in result["body"]


@respx.mock(assert_all_called=False)
def test_t_sc_194_greeting_plain_text_literal(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], name="<b>Bob</b> & Co"))
    _confirm(client, outbox)
    result = outbox[1]
    first = result["body"].split("\n", 1)[0]
    assert first == "Guten Tag <b>Bob</b> & Co,"  # plain text: shown literally, never rendered
    assert result["html"] is None  # plain-text mail, no HTML part
    assert selfcheck_api.greet("Hello,\nx", "O'Brien \"Jr\"", "en") == "Hello O'Brien \"Jr\",\nx"  # quotes stay
    assert selfcheck_api.greet("Hi there", "Bob", "en") == "Hi there"  # foreign opening: unchanged


@respx.mock(assert_all_called=False)
def test_t_sc_195_operator_mail_shows_name_and_phone(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["call"], phone=PHONE))
    _confirm(client, outbox)
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Name: Bob Beispiel" in note["body"] and f"Telefon: {PHONE}" in note["body"]
    outbox.clear()
    client.post("/api/selfcheck", headers=ORIGIN, json=body(email="eva@example.com", name="Eva Ohnetelefon", picks=["call"]))
    _confirm(client, outbox)
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Name: Eva Ohnetelefon" in note["body"] and "Telefon:" not in note["body"]


def test_t_sc_196_flag_off_missing_name_delivers_as_before(client, outbox, odoo, monkeypatch):
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_NAME_REQUIRED", False)
    b = body(picks=["mail", "call"], answers=COMPLETE)
    b.pop("name")
    assert client.post("/api/selfcheck", headers=ORIGIN, json=b).json() == {"status": "pending_confirmation"}
    _confirm(client, outbox)
    assert outbox[1]["body"].startswith("Guten Tag,\n")  # neutral greeting
    (lead,) = odoo.leads()
    assert "contact_name" not in lead and lead["name"].endswith("– bob@example.com")
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Name:" not in note["body"]


def test_t_sc_197_lead_alias_follows_the_flag(client, outbox, monkeypatch):
    b = body(kind="selfcheck")
    b.pop("name")
    assert client.post("/api/lead", headers=ORIGIN, json=b).status_code == 422
    assert client.post("/api/lead", headers=ORIGIN, json={**b, "name": " \t "}).status_code == 422
    assert outbox == []
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_NAME_REQUIRED", False)
    assert client.post("/api/lead", headers=ORIGIN, json=b).json() == {"status": "pending_confirmation"}
    assert client.post("/api/lead", headers=ORIGIN, json={**b, "name": "Bob Beispiel", "phone": "abc"}).status_code == 422
    assert len(outbox) == 1


@respx.mock(assert_all_called=False)
def test_t_sc_198_admin_lists_name_and_phone(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], phone=PHONE))
    _confirm(client, outbox)
    (sub,) = client.get("/api/selfcheck/submissions", headers=ADMIN).json()["submissions"]
    assert sub["name"] == "Bob Beispiel" and sub["phone"] == PHONE


def test_t_sc_199_patch_0010_and_flag_parsing(client):
    cols = {r["name"] for r in rows(client, "PRAGMA table_info(selfcheck_submission)")}
    assert {"name", "phone"} <= cols
    patch = HERE.parent / "db-patches" / "0010_selfcheck_name_phone.sql"
    assert patch.is_file()
    f = selfcheck_api.env_flag
    assert f(None) is True and f("") is True and f("true") is True and f("1") is True and f("yes") is True
    assert f("false") is False and f(" FALSE ") is False and f("0") is False and f("off") is False and f("no") is False
    assert selfcheck_api.SELFCHECK_NAME_REQUIRED is True  # default when unset (conftest does not set it)
