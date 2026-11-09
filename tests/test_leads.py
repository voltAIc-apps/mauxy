"""
CTA leads end to end through the API (leads_api.py + lead_sync.py, SPEC-021). Fake Odoo only.

  T-LEAD-001..029  POST /api/lead validation per kind (tests/cases/lead_api_cases.json)
  T-LEAD-030       nothing reaches Odoo before the confirmation; uniform immediate answer
  T-LEAD-031       contact: confirm -> ONE crm.lead (fields), newsletter subscription, redirect
  T-LEAD-032       confirmation link single use: no second lead
  T-LEAD-033       every kind: title, campaign, description
  T-LEAD-034/035   Selbst-Check: lead (mail / call) + Radar list + operator mail; Radar alone = no lead
  T-LEAD-036/037   transient failures -> retries -> exactly one lead (also when a response is lost)
  T-LEAD-038/039   permanent failure / retries exhausted -> failed + mail to Ashant; admin re-queue
  T-LEAD-040       queued delivery resumed after a restart
  T-LEAD-041       foreign tokens refused (newsletter / self-check salts, tampered)
  T-LEAD-042       retention + data minimisation (delivered rows hold no lead text)
  T-LEAD-043       LEAD_SINK=none: lead goes by mail, no Odoo call
  T-LEAD-044       newsletter DOI with the Odoo subscriber sink (+ blacklist, unsubscribe)
  T-LEAD-045       DOI mail carries no browser-supplied text
  T-LEAD-046       per-address throttle is silent
  T-LEAD-047       admin endpoints
  T-LEAD-048       timing: identical, immediate answers
  T-LEAD-049       health: sink status, no secrets
  T-LEAD-050       the Odoo smoke script only reads
"""
import asyncio
import copy
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

import jobs
import lead_sync
import leads_api
import mailer
import selfcheck_api
from tests.conftest import drain, click_confirm, is_neutral

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))
ORIGIN = {"Origin": "https://site.test"}
ADMIN = {"Authorization": "Bearer admin-test-key"}
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def base(kind, /, **overrides):
    b = copy.deepcopy(CASES["base"][kind])
    b.update(overrides)
    for k, v in list(b.items()):
        if v == "{future}":
            b[k] = FUTURE
        elif v == "{x4001}":
            b[k] = "x" * 4001
    return b


async def _rows(app, sql, params=()):
    async with app.state.db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


def rows(client, sql, params=()):
    return client.portal.call(_rows, client.app, sql, params)


def token_of(mail, path="/api/lead/confirm"):
    return re.search(re.escape(path) + r"\?token=(\S+)", mail["body"]).group(1)


def submit_and_confirm(client, outbox, payload):
    assert client.post("/api/lead", headers=ORIGIN, json=payload).json() == {"status": "pending_confirmation"}
    r = click_confirm(client, f"/api/lead/confirm?token={token_of(outbox[-1])}")
    drain(client)
    return r


# ------------------------------------------------------------------ T-LEAD-001..029


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["id"] for c in CASES["cases"]])
def test_lead_validation(case, client, outbox, odoo):
    b = base(case["kind"], **case.get("set", {}))
    for k in case.get("delete", []):
        b.pop(k, None)
    origin = case.get("origin", "https://site.test")
    resp = client.post("/api/lead", json=b, headers={"Origin": origin} if origin else {})
    assert resp.status_code == case["expect_status"], resp.text
    if resp.status_code == 200:
        assert resp.json() == {"status": "pending_confirmation"}
    assert (len(outbox) == 1) == case["expect_mail"]
    assert bool(rows(client, "SELECT id FROM lead_submission")) == case["expect_row"]
    assert odoo.calls == [] and odoo.authenticates == 0  # nothing before the confirmation


# ------------------------------------------------------------------ flow


def test_t_lead_030_nothing_before_confirmation(client, outbox, odoo):
    for kind in ("contact", "inquiry", "webinar", "checkout"):
        assert client.post("/api/lead", headers=ORIGIN, json=base(kind, newsletter=True)).json() == {"status": "pending_confirmation"}
    assert len(outbox) == 3  # MAX_PENDING_PER_EMAIL: the 4th is throttled silently
    assert odoo.calls == [] and odoo.authenticates == 0
    assert rows(client, "SELECT COUNT(*) AS n FROM lead_sync")[0]["n"] == 0


def test_t_lead_031_contact_creates_one_lead(client, outbox, odoo):
    r = submit_and_confirm(client, outbox, base("contact", newsletter=True, page_url="https://site.test/de/contact"))
    assert r.status_code == 303 and r.headers["location"] == "https://site.test/de/anfrage/bestaetigt?art=contact"
    (lead,) = odoo.leads()
    assert lead["name"] == "Website: Kontaktanfrage – Muster GmbH"
    assert lead["type"] == "lead" and lead["user_id"] == 7 and lead["message_partner_ids"] == [70]
    assert (lead["source_id"], lead["medium_id"], lead["campaign_id"]) == (11, 12, 21)
    assert (lead["email_from"], lead["contact_name"], lead["partner_name"], lead["phone"]) == (
        "bob@example.com", "Bob Muster", "Muster GmbH", "+49 30 1234567")
    assert "Wir planen den Wechsel von SAP ECC.<br>Bitte um Rückruf." in lead["description"]
    assert "Newsletter: ja (bestätigt)" in lead["description"]
    (contact,) = odoo.db["mailing.contact"].values()
    assert contact["email"] == "bob@example.com" and contact["name"] == "Bob Muster"
    sync = rows(client, "SELECT * FROM lead_sync")[0]
    assert sync["state"] == "done" and sync["odoo_lead_id"] == lead["id"] and json.loads(sync["sub_results"]) == {"newsletter": "ok"}
    assert sync["lead"] is None and sync["subscribe"] is None  # minimised after delivery
    assert [m["to"] for m in outbox] == ["bob@example.com"]  # Odoo notifies Ashant itself


def test_t_lead_032_single_use(client, outbox, odoo):
    client.post("/api/lead", headers=ORIGIN, json=base("contact"))
    token = token_of(outbox[0])
    assert click_confirm(client, f"/api/lead/confirm?token={token}").headers["location"].endswith("art=contact")
    again = click_confirm(client, f"/api/lead/confirm?token={token}")
    assert is_neutral(again)
    drain(client)
    assert len(odoo.leads()) == 1


@pytest.mark.parametrize("kind,title,campaign,snippets", [
    ("inquiry", "Website: Anfrage Odoo Datalake – Muster GmbH", 22, ["Anfrage zu app „Odoo Datalake“ (odoo-datalake)"]),
    ("webinar", "Website: Webinar KI in Odoo {d} – Muster GmbH", 23, ["Webinar-Anmeldung: „KI in Odoo“ (odoo-ki), Termin {d}"]),
    ("checkout", "Website: Bestellung Invoice OCR – Muster GmbH", 24,
     ["Bestellreferenz ORD-ABC123", "Summe netto laut Website: 1.490,00 €", "Rechnungsadresse: Hauptstr. 1, 10115 Berlin, DE",
      "USt-IdNr.: DE123456789", "Zusatzleistungen: Installation", "Paket: Starter"]),
])
def test_t_lead_033_every_kind(kind, title, campaign, snippets, client, outbox, odoo):
    payload = base(kind, company="Muster GmbH") if kind == "inquiry" else base(kind)
    lang = payload["lang"]
    r = submit_and_confirm(client, outbox, payload)
    assert r.headers["location"] == f"https://site.test/{lang}/anfrage/bestaetigt?art={kind}"
    d = leads_api._fmt_date(FUTURE)[:10]
    (lead,) = odoo.leads()
    assert lead["name"] == title.format(d=d) and lead["campaign_id"] == campaign and lead["type"] == "lead"
    for s in snippets:
        assert s.format(d=leads_api._fmt_date(FUTURE)).replace("&", "&amp;") in lead["description"], s


COMPLETE = {"system": ["ecc"], "why": ["eol"], "s4": "no", "when": "12"}


def test_t_lead_034_selfcheck_lead_radar_and_operator_mail(client, outbox, odoo):
    payload = {"kind": "selfcheck", "email": "bob@example.com", "lang": "de", "picks": ["mail", "call", "radar"],
               "answers": COMPLETE, "rules_version": "x", "consent": True, "company": "Muster GmbH",
               "name": "Bob Beispiel", "phone": "+49 (0)30 / 123-45"}
    assert client.post("/api/lead", headers=ORIGIN, json=payload).json() == {"status": "pending_confirmation"}
    assert odoo.calls == []
    r = click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0], '/api/selfcheck/confirm')}")
    drain(client)
    assert r.headers["location"] == "https://site.test/de/selbst-check?bestaetigt=1"
    (lead,) = odoo.leads()
    assert lead["name"] == "Website: Selbst-Check SAP ECC / R/3 – Muster GmbH"
    assert lead["campaign_id"] == 20 and lead["partner_name"] == "Muster GmbH"
    assert lead["contact_name"] == "Bob Beispiel" and lead["phone"] == "+49 (0)30 / 123-45"
    assert "Angefragt: Ergebnis und Richtangebot per Mail; 30 Minuten Kennenlernen (Meetly)" in lead["description"]
    assert "Ihre Antworten" in lead["description"]  # answers summary from the shared rules engine
    (sub,) = odoo.db["mailing.subscription"].values()
    assert sub["list_id"] == 2
    note = next(m for m in outbox if m["to"] == "owner@site.test")
    assert f"CRM-Lead (erp.wapsol): #{lead['id']}" in note["body"] and "Migration Radar: ok" in note["body"]
    row = rows(client, "SELECT radar_result, notify_sent, company FROM selfcheck_submission")[0]
    assert row == {"radar_result": "ok", "notify_sent": 1, "company": "Muster GmbH"}


def test_t_lead_035_radar_only_is_no_lead(client, outbox, odoo):
    client.post("/api/selfcheck", headers=ORIGIN, json={"email": "bob@example.com", "lang": "de", "picks": ["radar"],
                                                        "answers": {}, "rules_version": "x", "consent": True,
                                                        "name": "Bob Beispiel"})
    click_confirm(client, f"/api/selfcheck/confirm?token={token_of(outbox[0], '/api/selfcheck/confirm')}")
    drain(client)
    assert odoo.leads() == [] and len(odoo.db["mailing.subscription"]) == 1
    assert "CRM-Lead: keiner (nur Radar)" in outbox[-1]["body"]


def test_t_lead_036_transient_failures_are_retried(client, outbox, odoo):
    odoo.fail[("crm.lead", "create")] = ["http502", "timeout"]
    submit_and_confirm(client, outbox, base("contact"))
    assert len(odoo.leads()) == 1
    sync = rows(client, "SELECT state, attempts, last_error FROM lead_sync")[0]
    assert sync == {"state": "done", "attempts": 3, "last_error": None}


def test_t_lead_037_lost_response_no_duplicate(client, outbox, odoo):
    odoo.fail[("crm.lead", "create")] = ["lost"]  # Odoo created it, the answer never arrived
    submit_and_confirm(client, outbox, base("contact"))
    assert len(odoo.leads()) == 1
    assert [c[1] for c in odoo.calls if c[0] == "crm.lead"].count("create") == 1
    assert rows(client, "SELECT odoo_lead_id FROM lead_sync")[0]["odoo_lead_id"] == odoo.leads()[0]["id"]


def test_t_lead_038_permanent_failure_mails_ashant_and_can_be_requeued(client, outbox, odoo):
    odoo.fail[("crm.lead", "create")] = ["AccessError"]
    submit_and_confirm(client, outbox, base("contact"))
    assert odoo.leads() == []
    sync = rows(client, "SELECT * FROM lead_sync")[0]
    assert sync["state"] == "failed" and sync["failure_mailed"] == 1 and sync["attempts"] == 1
    mail = outbox[-1]
    assert mail["to"] == "owner@site.test" and "NICHT im CRM" in mail["subject"] and mail["reply_to"] == "bob@example.com"
    assert "Website: Kontaktanfrage – Muster GmbH" in mail["body"] and "Bitte um Rückruf" in mail["body"]
    assert "AccessError" in mail["body"]
    assert client.post(f"/api/lead/sync/{sync['id']}/retry").status_code == 401
    assert client.post(f"/api/lead/sync/{sync['id']}/retry", headers=ADMIN).json() == {"requeued": True}
    drain(client)
    assert len(odoo.leads()) == 1
    assert rows(client, "SELECT state FROM lead_sync")[0]["state"] == "done"
    assert client.post(f"/api/lead/sync/{sync['id']}/retry", headers=ADMIN).status_code == 409


def test_t_lead_039_retries_exhausted(client, outbox, odoo):
    odoo.fail[("crm.lead", "create")] = ["http502"] * 10
    submit_and_confirm(client, outbox, base("inquiry"))
    sync = rows(client, "SELECT state, attempts, last_error FROM lead_sync")[0]
    assert sync["state"] == "failed" and sync["attempts"] == lead_sync.MAX_ATTEMPTS and "retries exhausted" in sync["last_error"]
    assert "NICHT im CRM" in outbox[-1]["subject"]


def test_t_lead_040_resume_after_restart(client, outbox, odoo):
    client.post("/api/lead", headers=ORIGIN, json=base("contact"))
    async def confirm_without_job(app):
        # simulate: confirmed + queued, then the pod died before the job ran
        db = app.state.db
        row = (await _rows(app, "SELECT * FROM lead_submission"))[0]
        await db.execute("UPDATE lead_submission SET status='confirmed', confirmed_at=?, token_hash=NULL WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]))
        await db.commit()
        row = (await _rows(app, "SELECT * FROM lead_submission"))[0]
        lead = leads_api.lead_for(row, lead_sync.new_ref())
        await db.execute("INSERT INTO lead_sync (origin, origin_id, ref, created_at, lead) VALUES ('lead', ?, ?, ?, ?)",
                         (row["id"], lead["ref"], row["confirmed_at"], json.dumps(lead)))
        await db.commit()
        return await lead_sync.resume(db)
    assert client.portal.call(confirm_without_job, client.app) == 1
    drain(client)
    assert len(odoo.leads()) == 1


def test_t_lead_041_foreign_tokens_refused(client, outbox, odoo, token_signer):
    client.post("/api/lead", headers=ORIGIN, json=base("contact"))
    lid = rows(client, "SELECT id FROM lead_submission")[0]["id"]
    forged = [token_signer.dumps({"l": lid}), selfcheck_api._signer.dumps({"l": lid}), token_of(outbox[0])[:-3] + "abc",
              leads_api._signer.dumps({"l": "1"}), leads_api._signer.dumps({"l": lid + 99})]
    for t in forged:
        assert is_neutral(click_confirm(client, f"/api/lead/confirm?token={t}"))
    drain(client)
    assert odoo.calls == []


def test_t_lead_042_retention(client, outbox, odoo):
    submit_and_confirm(client, outbox, base("contact"))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    pending_old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")

    async def age(app):
        db = app.state.db
        await db.execute("UPDATE lead_submission SET confirmed_at = ?", (old,))
        await db.execute("UPDATE lead_sync SET done_at = ?", (old,))
        await db.execute("INSERT INTO lead_submission (kind, created_at, email, lang, data) VALUES ('contact', ?, 'x@example.com', 'de', '{}')", (pending_old,))
        await db.commit()
        await leads_api.purge(db)
        await lead_sync.purge(db)
    client.portal.call(age, client.app)
    assert rows(client, "SELECT * FROM lead_submission") == [] and rows(client, "SELECT * FROM lead_sync") == []


def test_t_lead_043_no_crm_sink_mails_the_lead(client, outbox, odoo, monkeypatch):
    monkeypatch.setenv("LEAD_SINK", "none")
    import sinks
    sinks.reset()
    submit_and_confirm(client, outbox, base("webinar"))
    assert odoo.leads() == [] and not [c for c in odoo.calls if c[0] == "crm.lead"]
    note = outbox[-1]
    assert note["to"] == "owner@site.test" and note["subject"].startswith("Website-Lead (nicht im CRM): Website: Webinar KI in Odoo")


def test_t_lead_044_newsletter_with_odoo_sink(client, outbox, odoo):
    page = {"locale": "de", "source": "footer", "consent": True, "pageUrl": "https://site.test/de/"}
    client.post("/api/subscribe", json={**page, "email": "nina@example.com"})
    assert odoo.writes_to("mailing.contact") == []  # read only before the click
    token = re.search(r"token=(\S+)", outbox[-1]["body"]).group(1)
    assert click_confirm(client, f"/api/confirm?token={token}").headers["location"] == "https://site.test/de/newsletter/confirmed"
    (contact,) = odoo.db["mailing.contact"].values()
    assert contact["email"] == "nina@example.com"
    n = len(outbox)
    client.post("/api/subscribe", json={**page, "email": "nina@example.com"})  # already subscribed: no mail
    odoo.add("mail.blacklist", email="bl@example.com")
    client.post("/api/subscribe", json={**page, "email": "bl@example.com"})  # blacklisted: no mail
    assert len(outbox) == n
    assert client.post("/api/unsubscribe", json={"email": "nina@example.com"}).json() == {"status": "ok"}
    assert all(s["opt_out"] for s in odoo.db["mailing.subscription"].values())
    odoo.fail[("mail.blacklist", "search_read")] = ["http502"]
    client.post("/api/subscribe", json={**page, "email": "new@example.com"})  # Odoo down: uniform 200, no DOI
    assert len(outbox) == n


def test_t_lead_045_doi_mail_has_no_browser_text(client, outbox, odoo):
    client.post("/api/lead", headers=ORIGIN, json=base("contact", name="Visit evil.example", company="Free money",
                                                       message="Click http://evil.example now"))
    body = outbox[0]["body"]
    for s in ("evil.example", "Free money", "Visit"):
        assert s not in body
    assert "eine Kontaktanfrage" in body and "https://mauxy.test/api/lead/confirm?token=" in body


def test_t_lead_046_throttle_silent(client, outbox, odoo):
    for _ in range(5):
        assert client.post("/api/lead", headers=ORIGIN, json=base("contact")).status_code == 200
    assert len(outbox) == 3


def test_t_lead_047_admin(client, outbox, odoo):
    submit_and_confirm(client, outbox, base("checkout", newsletter=True))
    assert client.get("/api/lead/submissions").status_code == 401
    data = client.get("/api/lead/submissions", headers=ADMIN).json()
    (s,) = data["submissions"]
    assert s["kind"] == "checkout" and s["crm_state"] == "done" and s["odoo_lead_id"] == odoo.leads()[0]["id"]
    assert s["data"]["order_ref"] == "ORD-ABC123" and s["sub_results"] == {"newsletter": "ok"} and "token_hash" not in s
    assert client.get("/api/lead/submissions?kind=contact", headers=ADMIN).json()["count"] == 0


def test_t_lead_048_timing_identical(client, outbox, odoo, monkeypatch):
    real = mailer.send

    async def slow(*a, **kw):
        await asyncio.sleep(0.6)
        return await real(*a, **kw)
    monkeypatch.setattr(mailer, "send", slow)
    t0 = time.perf_counter()
    a = client.request_nodrain("POST", "/api/lead", json=base("contact"), headers=ORIGIN)
    b = client.request_nodrain("POST", "/api/lead", json=base("contact", email="new@example.com"), headers=ORIGIN)
    assert time.perf_counter() - t0 < 0.6
    assert a.content == b.content == b'{"status":"pending_confirmation"}'
    client.portal.call(jobs.drain)
    assert len(outbox) == 2


@respx.mock(assert_all_called=False)
def test_t_lead_049_health(respx_mock, client, odoo):
    # sites pinned to Mautic (test ludo / re-cloud) are reported too: Mautic mocked reachable
    respx_mock.get("https://mautic.test/api/contacts?limit=1").mock(return_value=httpx.Response(200, json={}))
    assert client.get("/health").json() == {"status": "ok"}
    d = client.get("/health/detail").json()
    assert d["status"] == "ok" and d["lead_sink"].startswith("odoo: reachable") and d["subscriber_sink"].startswith("odoo: reachable")
    assert d["subscriber_sink_mautic"] == "mautic: reachable"
    assert "test-key-not-real" not in json.dumps(d)


def test_t_lead_050_smoke_script_never_writes():
    import ast
    src = (HERE.parent / "scripts" / "odoo_smoke.py").read_text()
    tree = ast.parse(src)
    guard = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "READ_METHODS")
    assert {e.value for e in guard.value.elts} == {"search_read", "read", "has_access", "check_access_rights"}
    assert '"create"' not in src.split("NEEDED")[0] and ".execute(" not in src  # only the guarded read() path
