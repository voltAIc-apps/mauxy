"""
Language of every text (owner decision 2026-11-09): T-LANG-001 .. T-LANG-020.

  T-LANG-001..006  external = the contact's language: DOI mail (subject, body, per-locale brand
                   footer), confirmation page, redirect, Self-Check result mail; lead / newsletter /
                   Self-Check x de / en (cases/language_cases.json "external")
  T-LANG-010       crm.lead of a GERMAN submission is English, every lead kind
  T-LANG-011       LEAD_SINK=none operator mail (lead by mail) is English
  T-LANG-012       failed CRM delivery mail (lead_sync) is English
  T-LANG-013       Self-Check de: operator mail + crm.lead English (engine "en" labels), result mail German
  T-LANG-014       failed routed delivery mail (routing) is English
  T-LANG-015       unknown locale -> the site's brand default_locale (redirect, sinks), unbranded -> de
"""
import copy
import json
import re
from pathlib import Path

import pytest

import brand
import leads_api
import mailer
import routing
import selfcheck_api
from tests.conftest import click_confirm, drain
from tests.test_leads import FUTURE, base

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "language_cases.json").read_text(encoding="utf-8"))
FORBIDDEN = CASES["internal_forbidden"]
ORIGIN = {"Origin": "https://site.test"}
ADMIN = {"Authorization": "Bearer admin-test-key"}
CONFIRM_PATH = {"lead": "/api/lead/confirm", "newsletter": "/api/confirm", "selfcheck": "/api/selfcheck/confirm"}
SC_COMPLETE = {"system": ["ecc", "excel"], "why": ["eol"], "s4": "no", "when": "12"}


def _german(text: str) -> list[str]:
    return [w for w in FORBIDDEN if w in text]


def _selfcheck(lang: str, **extra) -> dict:
    sc = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
    return {**copy.deepcopy(sc), "lang": lang, **extra}


def _submit(client, flow: str, lang: str):
    if flow == "lead":
        return client.post("/api/lead", headers=ORIGIN, json=base("contact", lang=lang))
    if flow == "newsletter":
        return client.post("/api/subscribe", json={"locale": lang, "source": "footer", "consent": True,
                                                   "pageUrl": f"https://site.test/{lang}/", "email": "nina@example.com"})
    return client.post("/api/selfcheck", headers=ORIGIN, json=_selfcheck(lang, picks=["mail", "radar"]))


# ------------------------------------------------------------------ T-LANG-001..006 external


@pytest.mark.parametrize("case", CASES["external"], ids=[c["id"] for c in CASES["external"]])
def test_t_lang_001_external_texts_in_contact_language(case, client, outbox, odoo):
    flow, lang = case["flow"], case["lang"]
    assert _submit(client, flow, lang).json() == {"status": "pending_confirmation"}
    (doi,) = outbox
    assert doi["subject"] == case["subject"]
    for s in case["body"]:
        assert s in doi["body"], s
    other = "en" if lang == "de" else "de"
    assert f"https://simplify-erp.de/{other}/impressum" not in doi["body"]  # footer of the contact's locale only
    path = CONFIRM_PATH[flow]
    token = re.search(re.escape(path) + r"\?token=(\S+)", doi["body"]).group(1)
    page = client.get(f"{path}?token={token}")
    for s in case["page"]:
        assert s in page.text, s
    r = click_confirm(client, f"{path}?token={token}")
    drain(client)
    assert r.status_code == 303 and r.headers["location"] == case["redirect"]
    if "result_subject" in case:
        result = next(m for m in outbox[1:] if m["to"] == "bob@example.com")
        assert result["subject"] == case["result_subject"]
        for s in case["result_body"]:
            assert s in result["body"], s


# ------------------------------------------------------------------ T-LANG-010..014 internal


@pytest.mark.parametrize("kind,title", [
    ("contact", "Website: Contact request – Muster GmbH"),
    ("inquiry", "Website: Inquiry Odoo Datalake – Muster GmbH"),
    ("webinar", "Website: Webinar KI in Odoo {d} – Muster GmbH"),
    ("checkout", "Website: Order Invoice OCR – Muster GmbH"),
])
def test_t_lang_010_crm_lead_english_for_german_submission(kind, title, client, outbox, odoo):
    payload = base(kind, lang="de", company="Muster GmbH")
    client.post("/api/lead", headers=ORIGIN, json=payload)
    (doi,) = outbox
    assert doi["subject"].startswith("Bitte bestätigen")  # the contact still gets German
    token = re.search(r"/api/lead/confirm\?token=(\S+)", doi["body"]).group(1)
    click_confirm(client, f"/api/lead/confirm?token={token}")
    drain(client)
    (lead,) = odoo.leads()
    assert lead["name"] == title.format(d=leads_api._fmt_date(FUTURE)[:10])
    desc = lead["description"]
    assert "Confirmed by double opt-in on" in desc and "(language de)" in desc
    assert "Source: site.test / Website / campaign " + kind in desc and "Reference: mauxy-" in desc
    assert _german(desc) == [] and _german(lead["name"]) == []


def test_t_lang_011_no_crm_sink_operator_mail_english(client, outbox):
    """LEAD_SINK=none (conftest default): the lead goes to LEAD_NOTIFY_TO by mail, in English."""
    client.post("/api/lead", headers=ORIGIN, json=base("checkout", lang="de"))
    token = re.search(r"/api/lead/confirm\?token=(\S+)", outbox[0]["body"]).group(1)
    click_confirm(client, f"/api/lead/confirm?token={token}")
    drain(client)
    note = outbox[-1]
    assert note["to"] == "owner@site.test" and note["subject"].startswith("Website lead (not in CRM): Website: Order ")
    assert "Net total per website: EUR 1,490.00" in note["body"] and "Company: " in note["body"]
    assert _german(note["subject"] + note["body"]) == []


def test_t_lang_012_failed_crm_delivery_mail_english(client, outbox, odoo):
    odoo.fail[("crm.lead", "create")] = ["AccessError"]
    client.post("/api/lead", headers=ORIGIN, json=base("contact", lang="de"))
    token = re.search(r"/api/lead/confirm\?token=(\S+)", outbox[0]["body"]).group(1)
    click_confirm(client, f"/api/lead/confirm?token={token}")
    drain(client)
    fail = outbox[-1]
    assert fail["to"] == "owner@site.test" and fail["subject"].startswith("mauxy: website lead NOT in CRM (lead #")
    assert "could not be delivered to erp.wapsol" in fail["body"] and "Attempts: 1" in fail["body"]
    assert "(admin key)" in fail["body"]
    assert _german(fail["subject"] + fail["body"].replace("Bitte um Rückruf", "")) == []  # message = contact's own text


def test_t_lang_013_selfcheck_operator_english_result_mail_german(client, outbox, odoo):
    client.post("/api/selfcheck", headers=ORIGIN, json=_selfcheck("de", picks=["mail", "call", "radar"],
                                                                  answers=SC_COMPLETE, company="Muster GmbH",
                                                                  phone="+49 30 1234567"))
    token = re.search(r"/api/selfcheck/confirm\?token=(\S+)", outbox[0]["body"]).group(1)
    click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    drain(client)
    result = next(m for m in outbox if m["to"] == "bob@example.com" and m is not outbox[0])
    assert result["subject"] == "Ihr Selbst-Check: Ergebnis" and "Ihre Antworten" in result["body"]
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    rules = selfcheck_api.RULES
    assert note["subject"].startswith("Self-check confirmed: Result and indicative quote, 30-minute introduction, Migration Radar (")
    assert note["subject"] == rules["mail"]["notify"]["subject"]["en"].format(
        picks_short="Result and indicative quote, 30-minute introduction, Migration Radar",
        precision=note["subject"].rsplit("(", 1)[1].rstrip(")"))
    body = note["body"]
    for s in ("Self-Check #", "(language de)", "Phone: +49 30 1234567", "Company: Muster GmbH",
              "Indicative quote: yes", "Introduction call (Meetly): yes", "Result mail to the person (de): sent",
              "Your answers", "CRM lead (erp.wapsol): #", "Retention 90 days"):
        assert s in body, s
    assert _german(note["subject"] + body) == []
    (lead,) = odoo.leads()
    assert lead["name"] == "Self-Check: SAP ECC / R/3 +1 – Muster GmbH"
    assert "Systems: SAP ECC / R/3 + Excel lists and DATEV" in lead["description"]
    assert "Requested: result and indicative quote by mail; 30-minute introduction (Meetly)" in lead["description"]
    assert _german(lead["description"]) == []
    assert selfcheck_api.OPERATOR_LANG == "en"


@pytest.mark.parametrize("action,legal", [("subscribe", False), ("unsubscribe", True)])
def test_t_lang_014_failed_routed_delivery_mail_english(action, legal, client, outbox):
    row = {"id": 9, "site": "simplify-web", "action": action, "sink": "webhook", "attempts": 3,
           "last_error": "HTTP 404", "params": json.dumps({"url": "https://flywheel.test/hook"}),
           "payload": json.dumps({"email": "fritz@example.com"})}
    assert client.portal.call(routing.mail_failure, row) is True
    (fail,) = outbox
    assert fail["to"] == "owner@site.test"
    assert fail["subject"] == f"mauxy: delivery FAILED (simplify-web / {action} -> webhook)"
    body = fail["body"]
    assert "failed permanently" in body and "Target: webhook https://flywheel.test/hook" in body
    assert "Attempts: 3" in body and "/api/admin/deliveries/9/retry (admin key)" in body
    assert ("(unsubscribe: legally required)" in body) is legal
    assert _german(fail["subject"] + body) == []


# ------------------------------------------------------------------ T-LANG-015 fallback


def test_t_lang_015_unknown_locale_uses_brand_default(client):
    r = client.request("PUT", "/api/admin/brands/re-cloud", headers=ADMIN,
                       json={"display_name": "Re-Cloud", "default_locale": "en", "website_url": "https://re-cloud.test"})
    assert r.status_code == 201, r.text
    assert brand.locale_for("re-cloud", None) == "en" and brand.locale_for("re-cloud", "xx") == "en"
    assert brand.locale_for("re-cloud", "de") == "de"  # the submission's own locale wins
    assert brand.locale_for("simplify-erp", None) == "de"  # seeded default_locale de
    assert brand.locale_for("no-profile", None) == "de"  # unbranded: as before 0006
    assert leads_api._target("re-cloud", "", True, "contact") == "https://re-cloud.test/en/anfrage/bestaetigt?art=contact"
    assert selfcheck_api._target("re-cloud", "", False) == "https://re-cloud.test/en/selbst-check?bestaetigt=fehler"
    assert mailer.OUTBOX == []
