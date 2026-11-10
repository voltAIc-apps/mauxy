"""
Salutation (owner request 2026-11-09, db-patches/0012): T-SAL-001 .. T-SAL-079.

The person STATES the salutation on the website (Anrede: Frau / Herr / Keine Angabe -> ms / mr / none);
mauxy never infers it from the name.

  T-SAL-001..043  greet(): de / en x ms / mr / none, academic titles, particles, single token, literal
                  plain text (cases/salutation_cases.json "greet", "greet_unchanged")
  T-SAL-050..054  crm.lead.title mapping via the xml ids (cases "title")
  T-SAL-055..059  title lookup: linked partner, read-only + cached, missing xml id, allowlist
  T-SAL-060..069  end to end: result mail (plain literal, HTML escaped), DOI without name, CRM title +
                  "Salutation:" lines, rollout flag SELFCHECK_SALUTATION_REQUIRED, /api/lead alias + CTA kinds,
                  admin listing, db-patches/0012
Validation of the field itself: T-SC-200..213 (cases/selfcheck_api_cases.json, test_selfcheck.py).
"""
import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import respx

import odoo_client
import selfcheck_api
import sinks
from tests.conftest import click_confirm, drain

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "salutation_cases.json").read_text(encoding="utf-8"))
SC_BASE = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
LEAD_BASE = json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))["base"]
ORIGIN = {"Origin": "https://site.test"}
ADMIN = {"Authorization": "Bearer admin-test-key"}
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
COMPLETE = {"system": ["ecc"], "why": ["eol"], "s4": "no", "when": "12"}
LEAD = {"ref": "mauxy-sal123", "kind": "contact", "campaign": "contact", "title": "Website: Contact request – X",
        "email": "anna@example.com", "contact_name": "Anna Muster", "partner_name": None, "phone": None,
        "lang": "de", "description": "x"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def body(**overrides):
    b = copy.deepcopy(SC_BASE)
    b.update(overrides)
    return b


def token_of(mail, path):
    return re.search(re.escape(path) + r"\?token=(\S+)", mail["body"]).group(1)


def submit_confirm(client, outbox, payload, path="/api/selfcheck"):
    r = client.post(path, headers=ORIGIN, json=payload)
    assert r.json() == {"status": "pending_confirmation"}, r.text
    confirm = "/api/lead/confirm" if payload.get("kind") not in (None, "selfcheck") else "/api/selfcheck/confirm"
    click_confirm(client, f"{confirm}?token={token_of(outbox[0], confirm)}")
    drain(client)


async def _rows(app, sql, params=()):
    async with app.state.db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


def rows(client, sql, params=()):
    return client.portal.call(_rows, client.app, sql, params)


# ------------------------------------------------------------------ T-SAL-001..043 greet()


@pytest.mark.parametrize("case", CASES["greet"], ids=[c["id"] for c in CASES["greet"]])
def test_greet(case):
    plain = selfcheck_api.GREETING[case["lang"]]["plain"]
    got = selfcheck_api.greet(plain + "\n\nREST", case["person"], case["lang"], case["salutation"])
    assert got == case["expect"] + "\n\nREST"


@pytest.mark.parametrize("case", CASES["greet_unchanged"], ids=[c["id"] for c in CASES["greet_unchanged"]])
def test_greet_unchanged(case):
    assert selfcheck_api.greet(case["text"], case["person"], case["lang"], case["salutation"]) == case["expect_text"]


def test_greet_default_salutation_is_none():
    """Old callers (no salutation argument) keep the greeting of before 0012."""
    assert selfcheck_api.greet("Guten Tag,\nx", "Anna Muster", "de") == "Guten Tag Anna Muster,\nx"
    assert selfcheck_api.salutation_label("ms") == "Ms" and selfcheck_api.salutation_label("mr") == "Mr"
    assert selfcheck_api.salutation_label("none") == "not given" and selfcheck_api.salutation_label(None) == "not given"


# ------------------------------------------------------------------ T-SAL-050..059 crm.lead.title


@pytest.mark.parametrize("case", CASES["title"], ids=[c["id"] for c in CASES["title"]])
def test_title_mapping(case, odoo):
    vals = run(sinks.lead_sink().build_vals({**LEAD, "salutation": case["salutation"]}))
    assert vals.get("title") == case["expect_title"]
    assert odoo.writes_to("res.partner.title") == [] and odoo.writes_to("ir.model.data") == []


def test_t_sal_055_title_with_linked_partner_partner_untouched(odoo):
    pid = odoo.add("res.partner", name="Anna Alt", email="anna@example.com")
    run(sinks.lead_sink().create_lead({**LEAD, "salutation": "ms"}, maybe_exists=False))
    (lead,) = odoo.leads()
    assert lead["partner_id"] == pid and lead["title"] == 3  # lead field only
    assert odoo.writes_to("res.partner") == [] and "title" not in odoo.db["res.partner"][pid]


def test_t_sal_056_lookup_read_only_and_cached(odoo):
    sink = sinks.lead_sink()
    run(sink.build_vals({**LEAD, "salutation": "none"}))
    assert not [c for c in odoo.calls if c[0] == "ir.model.data"]  # no lookup when nothing to map
    for sal in ("ms", "mr", "ms"):
        run(sink.build_vals({**LEAD, "salutation": sal}))
    lookups = [c for c in odoo.calls if c[0] == "ir.model.data"]
    assert len(lookups) == 1 and lookups[0][1] == "search_read"  # one read for both titles, then cached


def test_t_sal_057_missing_xmlid_no_title_warning(odoo, caplog):
    for k, v in list(odoo.db["ir.model.data"].items()):
        if v["name"] == "res_partner_title_mister":
            del odoo.db["ir.model.data"][k]
    sink = sinks.lead_sink()
    with caplog.at_level(logging.WARNING, logger="mauxy.sinks"):
        assert "title" not in run(sink.build_vals({**LEAD, "salutation": "mr"}))
    assert "ODOO_TITLE_MISSING xmlid=base.res_partner_title_mister" in caplog.text
    assert run(sink.build_vals({**LEAD, "salutation": "ms"}))["title"] == 3
    assert odoo.writes_to("res.partner.title") == []  # never created


def test_t_sal_058_allowlist_read_only():
    assert ("ir.model.data", "search_read") in odoo_client.ALLOWED
    for model, method in (("ir.model.data", "create"), ("ir.model.data", "write"), ("res.partner.title", "create"),
                          ("res.partner.title", "write"), ("res.partner.title", "unlink")):
        assert (model, method) not in odoo_client.ALLOWED
    c = odoo_client.OdooClient("https://odoo.test", "db", "u", "k")
    with pytest.raises(odoo_client.OdooRefused):
        run(c.execute("res.partner.title", "create", [{"name": "Frau"}]))


def test_t_sal_059_smoke_script_checks_title_rights():
    src = (HERE.parent / "scripts" / "odoo_smoke.py").read_text(encoding="utf-8")
    assert '("ir.model.data", "read")' in src and '("res.partner.title", "read")' in src


# ------------------------------------------------------------------ T-SAL-060..069 end to end


@pytest.mark.parametrize("lang,sal,name,first,neutral", [
    ("de", "ms", "Dr. Anna Muster", "Sehr geehrte Frau Dr. Muster,", "Guten Tag,"),
    ("de", "mr", "Prof. Dr. Max Mustermann", "Sehr geehrter Herr Prof. Dr. Mustermann,", "Guten Tag,"),
    ("de", "none", "Anna Muster", "Guten Tag Anna Muster,", "Guten Tag,"),
    ("en", "ms", "Anna Muster", "Dear Ms Muster,", "Hello,"),
    ("en", "mr", "Max Mustermann", "Dear Mr Mustermann,", "Hello,"),
    ("en", "none", "Anna Muster", "Hello Anna Muster,", "Hello,"),
])
@respx.mock(assert_all_called=False)
def test_t_sal_060_result_mail_greeting(respx_mock, client, outbox, lang, sal, name, first, neutral):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], lang=lang, salutation=sal, name=name))
    doi = outbox[0]
    assert doi["body"].startswith(neutral + "\n")  # DOI: server text only, no name, no salutation
    assert name.split()[-1] not in doi["body"] and "Frau" not in doi["body"] and "Herr" not in doi["body"]
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(doi, '/api/selfcheck/confirm')}")
    drain(client)
    result = outbox[1]
    assert result["body"].split("\n", 1)[0] == first  # plain text part
    assert f">{first}<" in result["html"]  # HTML part: same greeting (no markup in these names)
    assert result["body"].split("\n")[1] == ""  # the vendored intro follows unchanged


@respx.mock(assert_all_called=False)
def test_t_sal_061_greeting_escaped_in_html_literal_in_text(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], salutation="mr", name="Max <b>O'Muster&</b>"))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0], '/api/selfcheck/confirm')}")
    drain(client)
    result = outbox[1]
    assert result["body"].split("\n", 1)[0] == "Sehr geehrter Herr <b>O'Muster&</b>,"
    assert "Sehr geehrter Herr &lt;b&gt;O&#x27;Muster&amp;&lt;/b&gt;," in result["html"]
    assert "<b>O'Muster" not in result["html"]


def test_t_sal_062_crm_title_and_salutation_lines(client, outbox, odoo):
    submit_confirm(client, outbox, body(picks=["mail", "call"], answers=COMPLETE, salutation="ms", name="Dr. Anna Muster"))
    (lead,) = odoo.leads()
    assert lead["title"] == 3 and lead["contact_name"] == "Dr. Anna Muster"
    assert "Salutation: Ms" in lead["description"]
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Salutation: Ms\nName: Dr. Anna Muster" in note["body"]
    assert rows(client, "SELECT salutation FROM selfcheck_submission") == [{"salutation": "ms"}]


def test_t_sal_063_none_no_title_not_given(client, outbox, odoo):
    submit_confirm(client, outbox, body(picks=["call"], answers=COMPLETE, salutation="none"))
    (lead,) = odoo.leads()
    assert "title" not in lead and "Salutation: not given" in lead["description"]
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert "Salutation: not given" in note["body"]


def test_t_sal_064_flag_off_missing_is_guessed(client, outbox, odoo, monkeypatch):
    # owner 2026-11-10: no stated salutation -> guessed from the first name (Bob -> mr)
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_SALUTATION_REQUIRED", False)
    b = body(picks=["mail", "call"], answers=COMPLETE)
    b.pop("salutation")
    submit_confirm(client, outbox, b)
    assert outbox[1]["body"].startswith("Sehr geehrter Herr Beispiel,\n")
    (lead,) = odoo.leads()
    assert "Salutation: Mr" in lead["description"]
    assert rows(client, "SELECT salutation FROM selfcheck_submission") == [{"salutation": "mr"}]


def test_t_sal_064b_flag_off_undecidable_is_none(client, outbox, odoo, monkeypatch):
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_SALUTATION_REQUIRED", False)
    b = body(picks=["mail", "call"], answers=COMPLETE, name="Xqzt Beispiel")
    b.pop("salutation")
    submit_confirm(client, outbox, b)
    assert outbox[1]["body"].startswith("Guten Tag Xqzt Beispiel,\n")
    (lead,) = odoo.leads()
    assert "title" not in lead and "Salutation: not given" in lead["description"]
    assert rows(client, "SELECT salutation FROM selfcheck_submission") == [{"salutation": "none"}]


# T-SAL-070..: guess_salutation (gender_guesser, German data first, worldwide fallback)
@pytest.mark.parametrize("name,expected", [
    ("Anna Muster", "ms"), ("Max Muster", "mr"), ("Andrea Rossi", "ms"), ("Jürgen Klein", "mr"),
    ("Ulrike von Muster", "ms"), ("Prof. Dr. Wolfgang Muster", "mr"), ("Dr.-Ing. Birgit Muster", "ms"),
    ("Hans-Peter Muster", "mr"), ("Marie-Luise Muster", "ms"), ("Mehmet Yilmaz", "mr"), ("Priya Sharma", "ms"),
    ("Frau Kim Muster", "ms"), ("Herr Kim Muster", "mr"), ("Mr. Ashant Chalasani", "mr"),
    ("Kim Muster", "none"), ("Ashant Chalasani", "none"), ("Muster", "none"), ("", "none"), (None, "none"),
    ("A. Muster", "none"),
])
def test_t_sal_070_guess(name, expected):
    assert selfcheck_api.guess_salutation(name) == expected


def test_t_sal_071_stated_wins():
    assert selfcheck_api.resolve_salutation("none", "Max Muster") == "none"
    assert selfcheck_api.resolve_salutation("ms", "Max Muster") == "ms"
    assert selfcheck_api.resolve_salutation(None, "Max Muster") == "mr"


def test_t_sal_065_lead_alias_follows_the_flag(client, outbox, monkeypatch):
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_SALUTATION_REQUIRED", True)
    b = body(kind="selfcheck")
    b.pop("salutation")
    assert client.post("/api/lead", headers=ORIGIN, json=b).status_code == 422
    assert client.post("/api/lead", headers=ORIGIN, json={**b, "salutation": "mrs"}).status_code == 422
    assert outbox == []
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_SALUTATION_REQUIRED", False)
    assert client.post("/api/lead", headers=ORIGIN, json=b).json() == {"status": "pending_confirmation"}
    assert client.post("/api/lead", headers=ORIGIN, json={**b, "salutation": "mrs"}).status_code == 422
    assert len(outbox) == 1


@pytest.mark.parametrize("kind", ["contact", "inquiry", "webinar", "checkout"])
def test_t_sal_066_cta_kinds_optional_salutation(kind, client, outbox, odoo):
    payload = copy.deepcopy(LEAD_BASE[kind])
    if payload.get("date") == "{future}":
        payload["date"] = FUTURE
    payload.setdefault("name", "Max Mustermann")
    assert client.post("/api/lead", headers=ORIGIN, json={**payload, "salutation": "Herr"}).status_code == 422
    submit_confirm(client, outbox, {**payload, "salutation": "mr"}, path="/api/lead")
    (lead,) = odoo.leads()
    assert lead["title"] == 4 and "Salutation: Mr" in lead["description"]


def test_t_sal_067_cta_without_salutation_guessed(client, outbox, odoo):
    # owner 2026-11-10: no stated salutation -> guessed from the name (Bob Muster -> Mister)
    submit_confirm(client, outbox, copy.deepcopy(LEAD_BASE["contact"]), path="/api/lead")
    (lead,) = odoo.leads()
    assert lead.get("title") and "Salutation: Mr" in lead["description"]


@respx.mock(assert_all_called=False)
def test_t_sal_068_admin_lists_salutation(respx_mock, client, outbox):
    client.post("/api/selfcheck", headers=ORIGIN, json=body(picks=["mail"], salutation="mr"))
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0], '/api/selfcheck/confirm')}")
    drain(client)
    (sub,) = client.get("/api/selfcheck/submissions", headers=ADMIN).json()["submissions"]
    assert sub["salutation"] == "mr"


def test_t_sal_069_patch_0012_and_flag_default(client):
    cols = {r["name"] for r in rows(client, "PRAGMA table_info(selfcheck_submission)")}
    assert "salutation" in cols
    assert (HERE.parent / "db-patches" / "0012_selfcheck_salutation.sql").is_file()
    assert selfcheck_api.SELFCHECK_SALUTATION_REQUIRED is False  # default when unset (2026-11-10: guessed)
