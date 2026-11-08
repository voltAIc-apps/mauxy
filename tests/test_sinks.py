"""
Odoo sinks (sinks.py, SPEC-021): T-SINK-001 .. T-SINK-016. Fake Odoo only.

  001-003 partner matching: exactly one -> partner_id (no email_from / phone), else contact fields
  004-005 lead vals: type lead, salesperson, UTM mapped to existing records, team / tags, escaping
  006     follower: Odoo auto-follow kept, else message_subscribe(Ashant)
  007     retry safety: an earlier attempt's lead is found by its marker, no second create
  008-012 subscriber: new contact, existing contact, already, re-subscribe after opt-out, blacklist
  013     unsubscribe = opt_out on the configured lists only
  014     never a write to res.partner, never write / unlink on crm.lead
  015     missing config -> permanent
  016     factory: LEAD_SINK / SUBSCRIBER_SINK selection, Mautic deprecated
"""
import asyncio

import pytest

import sinks
from sinks import OdooLeadSink, OdooSubscriberSink, SinkPermanent

LEAD = {"ref": "mauxy-abc123", "kind": "contact", "campaign": "contact", "title": "Website: Kontaktanfrage – Muster GmbH",
        "email": "Bob@Example.com", "contact_name": "Bob Muster", "partner_name": "Muster GmbH", "phone": "+49 30 1234",
        "lang": "de", "description": "Zeile 1 <b>fett</b>\nZeile 2 & mehr"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def lead_sink():
    return sinks.lead_sink()


def created_lead(odoo):
    (lead,) = odoo.leads()
    return lead


def test_t_sink_001_exactly_one_partner_links(odoo):
    pid = odoo.add("res.partner", name="Bob Muster", email="bob@example.com", phone="+49 89 999")
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    lead = created_lead(odoo)
    assert lead["partner_id"] == pid and lead["contact_name"] == "Bob Muster"
    assert "email_from" not in lead and "phone" not in lead  # would be written back onto the partner
    assert odoo.db["res.partner"][pid]["phone"] == "+49 89 999"


def test_t_sink_002_no_partner_contact_fields(odoo):
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    lead = created_lead(odoo)
    assert "partner_id" not in lead
    assert (lead["email_from"], lead["contact_name"], lead["partner_name"], lead["phone"]) == (
        "Bob@Example.com", "Bob Muster", "Muster GmbH", "+49 30 1234")


def test_t_sink_003_two_partners_no_link(odoo):
    odoo.add("res.partner", name="Muster GmbH", email="bob@example.com")
    odoo.add("res.partner", name="Bob", email="BOB@example.com")
    odoo.add("res.partner", name="Archived", email="bob@example.com", active=False)  # archived ignored
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    assert "partner_id" not in created_lead(odoo)


def test_t_sink_004_lead_vals(odoo, monkeypatch):
    monkeypatch.setenv("ODOO_LEAD_TEAM_ID", "5")
    monkeypatch.setenv("ODOO_LEAD_TAG_IDS", "3, 4")
    sinks.reset()
    lead_id = run(lead_sink().create_lead(LEAD, maybe_exists=False))
    lead = created_lead(odoo)
    assert lead["id"] == lead_id
    assert lead["type"] == "lead" and lead["user_id"] == 7 and lead["team_id"] == 5 and lead["tag_ids"] == [[6, 0, [3, 4]]]
    assert (lead["source_id"], lead["medium_id"], lead["campaign_id"]) == (11, 12, 21)
    assert lead["name"] == "Website: Kontaktanfrage – Muster GmbH"
    assert lead["description"] == "<p>Zeile 1 &lt;b&gt;fett&lt;/b&gt;<br>Zeile 2 &amp; mehr</p><p>Referenz: mauxy-abc123</p>"


def test_t_sink_005_missing_utm_is_omitted_never_created(odoo):
    odoo.db["utm.campaign"].clear()
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    lead = created_lead(odoo)
    assert "campaign_id" not in lead and lead["source_id"] == 11
    assert not [c for c in odoo.calls if c[0].startswith("utm.") and c[1] != "search_read"]


def test_t_sink_006_follower(odoo):
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    assert created_lead(odoo)["message_partner_ids"] == [70]
    assert not odoo.writes_to("crm.lead")[1:]  # only the create, no extra subscribe
    odoo.db["crm.lead"].clear()
    odoo.auto_follow = False
    run(lead_sink().create_lead({**LEAD, "ref": "mauxy-def"}, maybe_exists=False))
    assert created_lead(odoo)["message_partner_ids"] == [70]
    assert [c[1] for c in odoo.writes_to("crm.lead")][-1] == "message_subscribe"


def test_t_sink_007_retry_finds_earlier_lead(odoo):
    first = run(lead_sink().create_lead(LEAD, maybe_exists=False))
    again = run(lead_sink().create_lead(LEAD, maybe_exists=True))
    assert first == again and len(odoo.leads()) == 1


def sub(odoo, email="nina@example.com", key="newsletter", **kw):
    return run(sinks.subscriber_sink().subscribe(email, key, lang="de", source="footer", page_url="https://site.test/de/", **kw))


def test_t_sink_008_new_contact(odoo):
    assert sub(odoo, "Nina@Example.com", name="Nina", company="Nina AG") == "ok"
    (contact,) = odoo.db["mailing.contact"].values()
    assert (contact["email"], contact["name"], contact["company_name"]) == ("nina@example.com", "Nina", "Nina AG")
    (s,) = odoo.db["mailing.subscription"].values()
    assert (s["contact_id"], s["list_id"], s["opt_out"]) == (contact["id"], 1, False)


def test_t_sink_009_existing_contact_gets_subscription_only(odoo):
    cid = odoo.add("mailing.contact", name="Nina Alt", email="nina@example.com")
    odoo.add("mailing.subscription", contact_id=cid, list_id=1, opt_out=False)
    assert sub(odoo, key="radar") == "ok"
    assert [c for c in odoo.writes_to("mailing.contact")] == []  # contact untouched
    assert {(s["list_id"], s["opt_out"]) for s in odoo.db["mailing.subscription"].values()} == {(1, False), (2, False)}


def test_t_sink_010_already_subscribed(odoo):
    cid = odoo.add("mailing.contact", name="Nina", email="nina@example.com")
    odoo.add("mailing.subscription", contact_id=cid, list_id=1, opt_out=False)
    assert sub(odoo) == "already"
    assert run(sinks.subscriber_sink().status("nina@example.com", "newsletter")) == "subscribed"
    assert not odoo.writes_to("mailing.subscription") and not odoo.writes_to("mailing.contact")


def test_t_sink_011_resubscribe_after_opt_out(odoo):
    cid = odoo.add("mailing.contact", name="Nina", email="nina@example.com")
    sid = odoo.add("mailing.subscription", contact_id=cid, list_id=1, opt_out=True)
    assert run(sinks.subscriber_sink().status("nina@example.com", "newsletter")) == "none"
    assert sub(odoo) == "ok"
    assert odoo.db["mailing.subscription"][sid]["opt_out"] is False
    assert odoo.writes_to("mailing.subscription") == [("mailing.subscription", "write", [[sid], {"opt_out": False}], {})]


def test_t_sink_012_blacklist_blocks(odoo):
    odoo.add("mail.blacklist", email="nina@example.com")
    assert run(sinks.subscriber_sink().status("Nina@example.com", "newsletter")) == "blocked"
    assert sub(odoo) == "blocked"
    assert not odoo.writes_to("mailing.contact") and not odoo.writes_to("mailing.subscription")


def test_t_sink_013_unsubscribe_configured_lists_only(odoo):
    cid = odoo.add("mailing.contact", name="Nina", email="nina@example.com")
    a = odoo.add("mailing.subscription", contact_id=cid, list_id=1, opt_out=False)
    b = odoo.add("mailing.subscription", contact_id=cid, list_id=2, opt_out=False)
    other = odoo.add("mailing.subscription", contact_id=cid, list_id=9, opt_out=False)  # not ours
    result, ref = run(sinks.subscriber_sink().unsubscribe("NINA@example.com"))
    assert result == "ok" and ref == f"{a},{b}"
    assert [odoo.db["mailing.subscription"][i]["opt_out"] for i in (a, b, other)] == [True, True, False]
    assert run(sinks.subscriber_sink().unsubscribe("nobody@example.com")) == ("not_found", None)


def test_t_sink_014_no_partner_or_lead_modifications(odoo):
    odoo.add("res.partner", name="Bob", email="bob@example.com")
    run(lead_sink().create_lead(LEAD, maybe_exists=False))
    run(lead_sink().create_lead(LEAD, maybe_exists=True))
    sub(odoo, "bob@example.com")
    assert odoo.writes_to("res.partner") == []
    assert {c[1] for c in odoo.writes_to("crm.lead")} <= {"create", "message_subscribe"}


def test_t_sink_015_missing_config_is_permanent(odoo, monkeypatch):
    monkeypatch.setenv("ODOO_LIST_RADAR_ID", "")
    monkeypatch.setenv("ODOO_LEAD_USER_ID", "")
    sinks.reset()
    with pytest.raises(SinkPermanent):
        sub(odoo, key="radar")
    with pytest.raises(SinkPermanent):
        run(lead_sink().create_lead(LEAD, maybe_exists=False))
    monkeypatch.setenv("ODOO_API_KEY", "")
    sinks.reset()
    with pytest.raises(SinkPermanent):
        sinks.lead_sink()


def test_t_sink_016_factory(monkeypatch, odoo):
    assert isinstance(sinks.lead_sink(), OdooLeadSink) and isinstance(sinks.subscriber_sink(), OdooSubscriberSink)
    monkeypatch.setenv("LEAD_SINK", "none")
    monkeypatch.setenv("SUBSCRIBER_SINK", "mautic")
    sinks.reset()
    assert sinks.lead_sink().name == "none" and sinks.subscriber_sink().name == "mautic"
    monkeypatch.setenv("SUBSCRIBER_SINK", "smtp-carrier-pigeon")
    sinks.reset()
    with pytest.raises(SinkPermanent):
        sinks.subscriber_sink()
    health = run(sinks.health())
    assert health["lead_sink"] == "none: disabled" and health["subscriber_sink"].startswith("error")
