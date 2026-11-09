"""
DOI mail links vs. link prefetch (security review 2026-11-09, finding 1). Mail security scanners
(Safe Links, Proofpoint, Mimecast) GET every link; so GET only validates and renders a page with a
"Bestätigen" / "Confirm" form, and only the POST (the human click) burns the token and writes to a
sink / creates the crm.lead. Same for all three flows: newsletter, CTA lead, Selbst-Check.

  T-SEC-020..035  data-driven matrix (tests/cases/confirm_click_cases.json): read-only GET / HEAD,
                  click, replay, CSRF (Origin / Referer), malformed POST bodies, tampered token, locale
  T-SEC-036       page hardening: CSP (hash, form-action incl. the redirect target), no-store,
                  noindex, no framing, Referrer-Policy, every element has a unique id, no external
                  resources, no script
  T-SEC-037       uniform neutral page: identical bytes for bad signature, expired, used, unknown,
                  foreign salt -- across all flows, GET and POST (per brand since db-patches/0006: the
                  brand comes from the token text, never from its validity)
  T-SEC-038       N parallel POSTs of one token -> exactly one confirmation
"""
import asyncio
import copy
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

import brand
import confirm_page
import leads_api
import main as mauxy_main
import selfcheck_api
from tests.conftest import MAUXY_ORIGIN, WEB_KEY, click_confirm, confirm_form, drain, is_neutral

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "confirm_click_cases.json").read_text(encoding="utf-8"))
SC_BASE = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
LEAD_BASE = json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))["base"]["contact"]
SITE_ORIGIN = {"Origin": "https://site.test"}
FLOWS = ("newsletter", "lead", "selfcheck")
PATH = {"newsletter": "/api/confirm", "lead": "/api/lead/confirm", "selfcheck": "/api/selfcheck/confirm"}
BUTTON = {"de": "Bestätigen", "en": "Confirm"}
# side effects of ONE confirmation: newsletter -> Odoo list contact; lead -> crm.lead via the outbox;
# selfcheck (picks mail + call + radar) -> crm.lead + Radar list contact via the outbox
ONCE = {"newsletter": {"contacts": 1, "leads": 0, "sync": 0},
        "lead": {"contacts": 0, "leads": 1, "sync": 1},
        "selfcheck": {"contacts": 1, "leads": 1, "sync": 1}}
TABLES = ("newsletter_pending", "lead_submission", "selfcheck_submission", "lead_sync", "action_log", "mail_outbox")


def ok_target(flow, lang):
    return {"newsletter": f"https://site.test/{lang}/newsletter/confirmed",
            "lead": f"https://site.test/{lang}/anfrage/bestaetigt?art=contact",
            "selfcheck": f"https://site.test/{lang}/selbst-check?bestaetigt=1"}[flow]


def submit(client, outbox, flow, lang="de", email="carla@example.com") -> str:
    """One submission -> one DOI mail -> its token."""
    if flow == "newsletter":
        r = client.post("/api/subscribe", headers={"Authorization": f"Bearer {WEB_KEY}"}, json={
            "email": email, "locale": lang, "source": "footer", "consent": True, "pageUrl": f"https://site.test/{lang}/"})
    elif flow == "lead":
        r = client.post("/api/lead", headers=SITE_ORIGIN, json={**copy.deepcopy(LEAD_BASE), "email": email, "lang": lang})
    else:
        r = client.post("/api/selfcheck", headers=SITE_ORIGIN, json={**copy.deepcopy(SC_BASE), "email": email, "lang": lang})
    assert r.status_code == 200, r.text
    (mail,) = [m for m in outbox if m["to"] == email]
    return re.search(re.escape(PATH[flow]) + r"\?token=(\S+)", mail["body"]).group(1)


async def _dump(app):
    out = {}
    for t in TABLES:
        async with app.state.db.execute(f"SELECT * FROM {t}") as cur:  # fixed table names
            out[t] = [tuple(r) for r in await cur.fetchall()]
    return out


def dump(client):
    return client.portal.call(_dump, client.app)


def effects(client, odoo, outbox):
    d = dump(client)
    return {"odoo_writes": len([c for c in odoo.calls if c[1] in ("create", "write", "unlink", "message_subscribe")]),
            "leads": len(odoo.leads()), "contacts": len(odoo.db["mailing.contact"]),
            "sync": len(d["lead_sync"]), "mails_after_doi": len(outbox) - 1}


def post(client, flow, token, *, body="form", origin="mauxy", referer="absent"):
    headers = {}
    if origin != "absent":
        headers["Origin"] = {"mauxy": "https://mauxy.test", "foreign": "https://evil.test", "null": "null"}[origin]
    if referer != "absent":
        headers["Referer"] = {"mauxy": f"https://mauxy.test{PATH[flow]}?token={token}", "foreign": "https://evil.test/x"}[referer]
    url, form = PATH[flow], "application/x-www-form-urlencoded"
    if body == "json":
        return client.post(url, json={"token": token}, headers=headers, follow_redirects=False)
    content = {"form": urlencode({"token": token}), "empty": "", "dup": urlencode([("token", token), ("token", token)]),
               "oversized": urlencode({"token": token, "pad": "x" * 3000}), "query": ""}[body]
    if body == "query":
        url = f"{url}?{urlencode({'token': token})}"
    return client.post(url, content=content.encode(), headers={**headers, "Content-Type": form}, follow_redirects=False)


def check(resp, expect, flow, lang):
    if expect == "page":
        assert resp.status_code == 200 and confirm_form(resp) is not None, resp.text[:300]
        assert f'lang="{lang}"' in resp.text and BUTTON[lang] in resp.text
        assert confirm_form(resp)[0] == PATH[flow]
    elif expect == "neutral":
        assert is_neutral(resp), (resp.status_code, resp.text[:300])
    elif expect == "neutral403":
        assert is_neutral(resp, 403), (resp.status_code, resp.text[:300])
    elif expect == "ok":
        assert resp.status_code == 303 and resp.headers["location"] == ok_target(flow, lang), (resp.status_code, resp.headers)


MATRIX = [(c, f) for c in CASES["cases"] for f in c["flows"]]


@pytest.mark.parametrize("case,flow", MATRIX, ids=[f"{c['id']}-{f}" for c, f in MATRIX])
def test_t_sec_020_035_confirm_matrix(case, flow, client, outbox, odoo):
    lang = case.get("lang", "de")
    token = submit(client, outbox, flow, lang)
    before = dump(client)
    for step in case["steps"]:
        t = token[:-3] + "abc" if step.get("token") == "tampered" else token
        for _ in range(step.get("repeat", 1)):
            if step["method"] in ("GET", "HEAD"):
                resp = client.request(step["method"], f"{PATH[flow]}?token={t}", follow_redirects=False)
            else:
                resp = post(client, flow, t, body=step.get("body", "form"), origin=step.get("origin", "mauxy"),
                            referer=step.get("referer", "absent"))
            check(resp, step["expect"], flow, lang)
    drain(client)
    fx = effects(client, odoo, outbox)
    if case["expect_writes"] == "none":
        assert fx == {"odoo_writes": 0, "leads": 0, "contacts": 0, "sync": 0, "mails_after_doi": 0}, fx
    else:  # exactly one confirmation, whatever was replayed
        assert {k: fx[k] for k in ONCE[flow]} == ONCE[flow], fx
    if case.get("db_unchanged"):
        assert dump(client) == before  # not even an action_log line
    if case["then_click_works"]:
        assert click_confirm(client, f"{PATH[flow]}?token={token}").headers["location"] == ok_target(flow, lang)


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def _hardened(resp, form_action, allowed_hrefs=()):
    h = resp.headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    assert f"form-action {form_action}" in csp and "unsafe-inline" not in csp and "script-src" not in csp
    style = re.search(r"<style id=\"mx-style\">(.*?)</style>", resp.text, re.S).group(1)
    import base64
    import hashlib
    assert "'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'" in csp
    assert h["cache-control"] == "no-store" and h["x-robots-tag"] == "noindex, nofollow"
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"  # no-referrer would turn the POST's Origin into "null"
    assert h["content-type"].startswith("text/html")
    p = _Tags()
    p.feed(resp.text)
    ids = [a.get("id") for _, a in p.tags]
    assert all(ids) and len(ids) == len(set(ids)), ids  # every element has a unique id
    assert not [t for t, _ in p.tags if t in ("script", "img", "link", "iframe", "object", "embed")]
    text = resp.text
    for href in allowed_hrefs:  # brand legal links (validated https, navigation only, not a resource)
        assert f'href="{href}" rel="noopener noreferrer"' in text
        text = text.replace(f'href="{href}"', "")
    assert not re.search(r"\b(src|href)=|url\(|@import", text)  # no external resources


@pytest.mark.parametrize("flow", FLOWS)
def test_t_sec_036_page_hardening(flow, client, outbox, odoo):
    token = submit(client, outbox, flow)
    page = client.get(f"{PATH[flow]}?token={token}")
    # newsletter: test site simplify-web (no profile); lead / selfcheck: DEFAULT_SITE simplify-erp (seeded profile)
    _hardened(page, "'self' https://site.test", () if flow == "newsletter" else ("https://simplify-erp.de/de/impressum",))
    assert '<meta id="mx-robots" name="robots" content="noindex, nofollow">' in page.text
    neutral = client.get(f"{PATH[flow]}?token=garbage")
    _hardened(neutral, "'none'")


def test_t_sec_037_neutral_page_uniform(client, outbox, odoo, monkeypatch):
    tokens = {f: submit(client, outbox, f, email=f"{f}@example.com") for f in FLOWS}
    foreign = {"newsletter": tokens["lead"], "lead": tokens["selfcheck"], "selfcheck": tokens["newsletter"]}
    bodies = set()
    for f in FLOWS:
        variants = [tokens[f][:-2] + "zz", "not-a-token", foreign[f]]
        if f == "newsletter":
            variants.append(mauxy_main._token_signer.dumps({"p": "does-not-exist"}))
        else:
            signer = leads_api._signer if f == "lead" else selfcheck_api._signer
            variants.append(signer.dumps({"l" if f == "lead" else "s": 999999}))
        for t in variants:
            for resp in (client.get(f"{PATH[f]}?token={t}"), post(client, f, t)):
                assert is_neutral(resp)
                bodies.add(resp.content)
        assert click_confirm(client, f"{PATH[f]}?token={tokens[f]}").status_code == 303
        for resp in (client.get(f"{PATH[f]}?token={tokens[f]}"), post(client, f, tokens[f])):  # used
            assert is_neutral(resp)
            bodies.add(resp.content)
    # expired (fresh submissions, max_age forced below zero in every module)
    fresh = {f: submit(client, outbox, f, email=f"x-{f}@example.com") for f in FLOWS}
    for mod in (mauxy_main, leads_api, selfcheck_api):
        monkeypatch.setattr(mod, "CONFIRM_TOKEN_MAX_AGE", -1)
    for f in FLOWS:
        for resp in (client.get(f"{PATH[f]}?token={fresh[f]}"), post(client, f, fresh[f])):
            assert is_neutral(resp)
            bodies.add(resp.content)
    # one page for every reason and every flow -- per brand: the newsletter test site has no profile,
    # lead / selfcheck tokens carry the simplify-erp claim (garbage / minted-without-claim -> generic)
    generic, branded = confirm_page.neutral().body, confirm_page.neutral(brand=brand.get("simplify-erp")).body
    assert bodies == {generic, branded}


async def _parallel_posts(app, path, token, n):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        return await asyncio.gather(*(ac.post(path, data={"token": token}, headers=MAUXY_ORIGIN) for _ in range(n)))


@pytest.mark.parametrize("flow", FLOWS)
def test_t_sec_038_parallel_posts_confirm_once(flow, client, outbox, odoo):
    token = submit(client, outbox, flow)
    resps = client.portal.call(_parallel_posts, client.app, PATH[flow], token, 6)
    assert sorted(r.status_code for r in resps) == [200] * 5 + [303]
    assert all(is_neutral(r) for r in resps if r.status_code == 200)
    drain(client)
    fx = effects(client, odoo, outbox)
    assert {k: fx[k] for k in ONCE[flow]} == ONCE[flow], fx
