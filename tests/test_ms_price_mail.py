"""
Microsoft -> Odoo 5-year comparison in the Selbst-Check mails (flywheel SPEC-022, owner decision 2026-11-10 /
D-065): T-MSC-100 .. T-MSC-119 here, plus the shared flywheel cases T-MSC-080..093 (vendored
vendor/selfcheck/mscalc_cases.json) run through the real submit -> DOI click -> mails flow.

  T-MSC-080..083  customer-mail gate (shared cases): figures only with the owner switch on AND every rate confirmed
  T-MSC-090..093  shared mail cases: result mail (plain text + HTML, contact's language) and the English
                  operator line, gate closed (neutral sentence) and open (figures)
  T-MSC-100       shipped rules: gate closed while any rate is owner_to_confirm (no figure to the customer)
  T-MSC-101       crm.lead description carries the operator line ("UNCONFIRMED RATES")
  T-MSC-102       no Microsoft system -> no comparison block, no operator line
  T-MSC-103       Microsoft system without stay figures -> nothing computed
  T-MSC-104..111  rollout + validation: old payload valid, new fields optional, ranges from mscalc.json (422)
  T-MSC-112       vendored comparison files listed in SOURCE.json (sha256, T-SC-150 checks them)
"""
import json
from pathlib import Path

import pytest

import selfcheck_api
from tests.conftest import click_confirm, drain
from tests.test_selfcheck import ORIGIN, body, token_of
from vendor.selfcheck import mscalc

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "selfcheck"
CASES = json.loads((VENDOR / "mscalc_cases.json").read_text(encoding="utf-8"))
MS_ANSWERS = {"system": ["ms"], "why": ["cost"], "when": "12"}
STAY_002 = {"users": 40, "support": 30000, "offer": 80000, "customisations": 40, "history_years": 5, "office_seats": 40}


def patched(patch: dict) -> dict:
    """Rules as after the owner's release (same helper as flywheel tests/test_mscalc.py and the TS runner)."""
    r = json.loads(json.dumps(selfcheck_api.MSCALC))
    if "customer_mail_figures" in patch:
        r["customer_mail_figures"] = patch["customer_mail_figures"]
    if patch.get("confirm_all"):
        for a in r["assumptions"]:
            if a["status"] == "owner_to_confirm":
                a["status"] = "verified"
    return r


def run(client, outbox, **overrides) -> tuple[dict, dict]:
    """Submit, click the DOI link, deliver; (result mail to the contact, operator mail)."""
    b = body(**{"picks": ["mail"], "answers": MS_ANSWERS, **overrides})
    r = client.post("/api/selfcheck", headers=ORIGIN, json=b)
    assert r.status_code == 200, r.text
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0])}")
    drain(client)
    result = next(m for m in outbox if m["to"] == b["email"] and m is not outbox[0])
    notify = next(m for m in outbox if m["to"] == "owner@site.test")
    return result, notify


# ------------------------------------------------------------------ shared cases T-MSC-080..093


@pytest.mark.parametrize("case", CASES["gate"], ids=[c["id"] for c in CASES["gate"]])
def test_shared_gate_cases(case):
    assert mscalc.mail_figures_allowed(patched(case["patch"])) is case["expect"]


@pytest.mark.parametrize("case", CASES["mail"], ids=[c["id"] for c in CASES["mail"]])
def test_shared_mail_cases_through_the_real_mails(case, client, outbox, monkeypatch):
    rules = patched(case["patch"])
    monkeypatch.setattr(selfcheck_api, "MSCALC", rules)
    result, notify = run(client, outbox, lang=case["lang"], stay=case["stay"])
    # customer: title + exactly the case lines, in the plain text (one block) and in the HTML part
    assert "\n".join([case["title_expect"], *case["lines"]]) in result["body"]
    assert case["title_expect"] in result["html"]
    for line in case["lines"]:
        assert line in result["html"]
    # block sits before "Ihre Antworten" / "Your answers" (after the stay costs), like the HTML part
    answers_label = "Ihre Antworten" if case["lang"] == "de" else "Your answers"
    assert result["body"].index(case["title_expect"]) < result["body"].index(f"\n\n{answers_label}\n")
    # operator (English): the figures always, flagged while rates are unconfirmed
    assert case["operator"] in notify["body"]
    if not mscalc.mail_figures_allowed(rules):
        r = mscalc.compare_selfcheck(rules, {"system": ["ms"]}, case["stay"])
        for amount in (r["stay"]["total"], r["move"]["total"]["low"], r["move"]["one_off"]["high"]):
            shown = mscalc.format_eur(amount, case["lang"])
            assert shown not in result["body"] and shown not in result["html"], shown


# ------------------------------------------------------------------ T-MSC-100..103


def test_t_msc_100_shipped_rules_never_put_figures_in_the_customer_mail(client, outbox):
    """Owner rule: while ANY rate is owner_to_confirm, no figure to the customer; operator sees them flagged."""
    assert mscalc.unconfirmed(selfcheck_api.MSCALC), "release done: revisit this test"
    assert mscalc.mail_figures_allowed(selfcheck_api.MSCALC) is False
    result, notify = run(client, outbox, stay=STAY_002)
    pending = selfcheck_api.MSCALC["mail"]["pending"]["de"].format(years=5)
    assert pending in result["body"] and pending in result["html"]
    assert "410.888" not in result["body"] + result["html"] and "348.760" not in result["body"] + result["html"]
    assert "UNCONFIRMED RATES" in notify["body"] and "€410,888" in notify["body"]


def test_t_msc_101_crm_lead_carries_the_operator_line(client, outbox, odoo):
    run(client, outbox, picks=["mail", "call"], stay=STAY_002, company="Muster GmbH")
    (lead,) = odoo.leads()
    assert "Odoo 5y UNCONFIRMED RATES (13 owner_to_confirm)" in lead["description"]  # description is HTML (-> escaped)
    assert "stay €410,888" in lead["description"] and "customer mail: neutral sentence (gate closed)" in lead["description"]


def test_t_msc_102_other_systems_get_no_comparison(client, outbox):
    result, notify = run(client, outbox, answers={"system": ["ecc"], "when": "12"}, stay=STAY_002)
    title = selfcheck_api.MSCALC["mail"]["title"]["de"].format(years=5)
    assert title not in result["body"] and title not in (result["html"] or "")
    assert "Microsoft -> Odoo" not in notify["body"]


def test_t_msc_103_microsoft_without_figures_computes_nothing(client, outbox):
    result, notify = run(client, outbox)
    assert "Microsoft oder Odoo" not in result["body"] and "Microsoft -> Odoo" not in notify["body"]


# ------------------------------------------------------------------ T-MSC-104..111 rollout + validation


def test_t_msc_104_old_payload_without_the_new_fields_stays_valid(client):
    """Rollout: the website of before (stay licence / support / offer / users only) keeps working."""
    b = body(stay={"licence": 120000, "support": 30000, "offer": 0, "users": 40})
    assert client.post("/api/selfcheck", headers=ORIGIN, json=b).status_code == 200
    assert client.post("/api/selfcheck", headers=ORIGIN, json=body()).status_code == 200  # no stay at all


@pytest.mark.parametrize("tid,stay,status", [
    ("T-MSC-105", {"customisations": 500, "history_years": 40, "office_seats": 10000}, 200),  # the limits themselves
    ("T-MSC-106", {"customisations": 501}, 422),
    ("T-MSC-107", {"history_years": 41}, 422),
    ("T-MSC-108", {"office_seats": -1}, 422),
    ("T-MSC-109", {"customisations": 2.5}, 422),
    ("T-MSC-110", {"history_years": "3"}, 422),
    ("T-MSC-111", {"office_seats": 10001, "users": 10}, 422),
])
def test_new_fields_are_validated_server_side(tid, stay, status, client):
    r = client.post("/api/selfcheck", headers=ORIGIN, json=body(answers=MS_ANSWERS, stay=stay))
    assert r.status_code == status, (tid, r.text)


def test_t_msc_105b_limits_come_from_the_vendored_rules():
    limits = selfcheck_api.MSCALC["limits"]
    fields = selfcheck_api.StayInputs.model_fields
    for name in ("customisations", "history_years", "office_seats"):
        le = next(m.le for m in fields[name].metadata if hasattr(m, "le"))
        assert le == limits[name], name


# ------------------------------------------------------------------ T-MSC-112 vendoring


def test_t_msc_112_comparison_files_are_vendored_with_checksums():
    src = json.loads((VENDOR / "SOURCE.json").read_text())
    assert {"mscalc.py", "mscalc.json", "mscalc_cases.json"} <= set(src["sha256"])
    assert src["mscalc_version"] == selfcheck_api.MSCALC["version"] == CASES["rules_version"]
