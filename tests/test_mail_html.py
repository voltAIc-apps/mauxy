"""
Branded HTML part of the Selbst-Check mails (mail_html.py, owner request 2026-11-09): T-HTML-001 .. T-HTML-040.

  T-HTML-001..004  result mail: multipart/alternative (text first, HTML second), plain text unchanged,
                   structure (headline, 5 areas, risks, waves, quote + precision, button), partial answers
  T-HTML-005..006  de / en (cases in tests/cases/mail_html_cases.json "lang")
  T-HTML-007       ids on every element and unique; no script / form / external resource
  T-HTML-008       only the brand's website / imprint / privacy URLs (+ cid:) in the HTML
  T-HTML-009..011  brand values from the profile: a second brand renders its own name / colours / logo
                   (inline Content-ID), WebP logo -> wordmark, no profile -> no HTML part (as before 0006)
  T-HTML-012       DOI mail in the same frame (button = the confirm link, no name, no attachments)
  T-HTML-013       colour helpers keep text readable for light brand colours
  T-HTML-014..015  db-patches/0011 (simplify accent = website CTA red, owner value kept); Dockerfile ships mail_html.py
  T-HTML-020..024  XSS in the name (cases "xss"): escaped in HTML, literal in the text part
  T-HTML-050..057  dark mode (owner request 2026-11-09): color-scheme metas + :root, prefers-color-scheme
                   block, Outlook [data-ogsc] / [data-ogsb] duplicates, every coloured element carries its
                   dark class, dark contrast pairs (4.5:1 text + links, 3:1 button / bars) for several
                   brands, light pairs survive Gmail's forced inversion, traffic lights distinguishable,
                   logo on a white badge in every mode
"""
import base64
import colorsys
import copy
import email
import email.policy
import json
import re
import sqlite3
import struct
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest

import brand
import db_migrate
import mail_html
import mailer
import selfcheck_api
from tests.conftest import click_confirm, drain

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases" / "mail_html_cases.json").read_text(encoding="utf-8"))
SC_BASE = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
ORIGIN = {"Origin": "https://site.test"}
ADMIN = {"Authorization": "Bearer admin-test-key"}
COMPLETE = {
    "system": ["ecc", "excel"], "why": ["eol", "duty"], "s4": "no", "when": "12", "users": "150",
    "areas": ["fibu", "einkauf", "lager", "verkauf", "produktion"], "entities": "3", "custom": "many",
    "ifaces": ["datev", "bank", "edi", "lohn"], "data": "partly", "history": "2y", "team": "one", "hosting": "cloud",
}
SIMPLIFY = "https://site.test"  # website_url of simplify-erp in the tests (conftest client fixture)
IMPRINT = "https://simplify-erp.de/"  # seeded imprint_url (db-patches/0006)


def png(width=4, height=2) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


WEBP = b"RIFF" + struct.pack("<I", 26) + b"WEBPVP8 " + b"\x00" * 18
RECLOUD = {"display_name": "Re-Cloud", "legal_entity": "Re-Cloud Holding", "color_primary": "#112233",
           "color_accent": "#445566", "wordmark": "re-cloud", "website_url": "https://re-cloud.test",
           "imprint_url": "https://re-cloud.test/{lang}/impressum", "privacy_url": "https://re-cloud.test/{lang}/datenschutz",
           "default_locale": "de", "mail_footer": {"de": "Re-Cloud Team\nRe-Cloud Holding", "en": "Re-Cloud team\nRe-Cloud Holding"},
           "origins": ["https://re-cloud.test"], "logo": {"mime": "image/png", "data": base64.b64encode(png()).decode()}}


def submit_and_confirm(client, outbox, *, origin=ORIGIN, **overrides):
    b = {**copy.deepcopy(SC_BASE), "picks": ["mail"], "answers": COMPLETE, **overrides}
    r = client.post("/api/selfcheck", headers=origin, json=b)
    assert r.status_code == 200, r.text
    doi = [m for m in outbox if m["to"] == b["email"]][-1]
    token = re.search(r"/api/selfcheck/confirm\?token=(\S+)", doi["body"]).group(1)
    click_confirm(client, f"/api/selfcheck/confirm?token={token}")
    drain(client)
    result = [m for m in outbox if m["to"] == b["email"] and m is not doi][-1]
    return doi, result


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def tags(doc: str) -> list[tuple[str, dict]]:
    p = Tags()
    p.feed(doc)
    return p.tags


def parts(mime: bytes):
    msg = email.message_from_bytes(mime, policy=email.policy.SMTP)
    return msg, [(p.get_content_type(), p) for p in msg.walk()]


# ------------------------------------------------------------------ T-HTML-001..004 structure


def test_t_html_001_result_mail_multipart_alternative(client, outbox):
    _doi, result = submit_and_confirm(client, outbox)
    assert result["html"] and result["body"].startswith("Guten Tag Bob Beispiel,\n\ndanke")
    msg, ps = parts(result["mime"])
    types = [t for t, _ in ps]
    assert types[:3] == ["multipart/alternative", "text/plain", "text/html"]  # text first = fallback
    assert result["attachments"] == []  # seeded simplify-erp has no logo: wordmark header, nothing inline
    html_part = dict(ps)["text/html"].get_content()
    assert html_part.replace("\r\n", "\n").rstrip("\n") == result["html"] and "Simplify-ERP" in html_part


def test_t_html_002_plain_text_unchanged(client, outbox):
    """The text part is exactly what result_mail produced before the HTML part existed."""
    _doi, result = submit_and_confirm(client, outbox)
    row = {"lang": "de", "site": "simplify-erp", "answers": json.dumps(COMPLETE), "stay": None, "name": "Bob Beispiel"}
    m = selfcheck_api.RULES["mail"]["result"]
    url = selfcheck_api._site_url("de", "simplify-erp")
    head = selfcheck_api.greet(m["intro"]["de"].format(answered=13, total=13, url=url), "Bob Beispiel", "de")
    old = "\n\n".join([head, selfcheck_api.engine.result_text(selfcheck_api.RULES, COMPLETE, "de"), m["outro"]["de"].format(url=url)])
    assert result["body"] == brand.replace_signature(old, "simplify-erp", "de", selfcheck_api.SIGNATURE["de"])
    assert row["name"] in result["body"]


def test_t_html_003_structure_mirrors_result_page(client, outbox):
    _doi, result = submit_and_confirm(client, outbox)
    doc = result["html"]
    ev = selfcheck_api.engine.evaluate(selfcheck_api.RULES, COMPLETE, "de")
    assert f'id="mx-headline"' in doc and ev["headline"] in doc
    for d in ev["dims"]:  # the five areas with dot + label in the mail's language
        assert f'id="mx-dim-{d["id"]}"' in doc and d["name"] in doc and d["label"] in doc and d["color"] in doc
    assert len(ev["dims"]) == 5
    for r in ev["risks"]:
        assert f'id="mx-risk-{r["id"]}"' in doc and r["title"] in doc
        if r["article"]:
            assert f'href="{SIMPLIFY}/de/wissen/{r["article"]}"' in doc
    for w in ev["waves"]:
        assert f'id="mx-wave-{w["id"]}"' in doc and w["title"] in doc
    assert "Genauigkeit: präzise · aus 13 von 13 Antworten" in doc and 'id="mx-quote"' in doc
    assert f'href="{SIMPLIFY}/de/selbst-check"' in doc and "Ergebnis online ansehen" in doc
    assert 'id="mx-preheader"' in doc and doc.index(ev["headline"]) < doc.index('id="mx-wrap"')  # preheader = verdict
    assert "Ashant Chalasani" in doc and f'href="{IMPRINT}de/impressum"' in doc
    # order as on the page: verdict, areas, risks, waves, quote, button, answers, signature
    order = ["mx-headline", "mx-lights", "mx-risks", "mx-waves", "mx-quote", "mx-button", "mx-answers", "mx-signature"]
    pos = [doc.index(f'id="{i}"') for i in order]
    assert pos == sorted(pos)


def test_t_html_004_partial_answers_no_verdict(client, outbox):
    _doi, result = submit_and_confirm(client, outbox, answers=SC_BASE["answers"], stay={"licence": 120000, "support": 30000})
    doc = result["html"]
    assert 'id="mx-headline"' not in doc and 'id="mx-lights"' not in doc
    assert "Genauigkeit: grob" in doc and 'id="mx-stay-title"' in doc and "750.000 €" in doc
    assert f'<a id="mx-intro0-url" class="mx-link" href="{SIMPLIFY}/de/selbst-check"' in doc  # partial intro link


# ------------------------------------------------------------------ T-HTML-005..006 language


@pytest.mark.parametrize("lang", ["de", "en"], ids=["T-HTML-005-de", "T-HTML-006-en"])
def test_t_html_005_006_language(lang, client, outbox):
    case = CASES["lang"][lang]
    _doi, result = submit_and_confirm(client, outbox, lang=lang)
    assert result["subject"] == case["subject"]
    for word in case["present"]:
        assert word in result["html"], word
    for word in case["absent"]:
        assert word not in result["html"], word
    assert f'href="{SIMPLIFY}/{lang}/selbst-check"' in result["html"]


# ------------------------------------------------------------------ T-HTML-007..008 ids, resources, URLs


def test_t_html_007_ids_unique_no_active_content(client, outbox):
    doi, result = submit_and_confirm(client, outbox, stay={"licence": 1000, "support": 10})
    for doc in (result["html"], doi["html"]):
        found = tags(doc)
        ids = [a.get("id") for _, a in found]
        assert all(ids), [t for t, a in found if not a.get("id")]
        assert len(ids) == len(set(ids))
        names = {t for t, _ in found}
        assert not names & {"script", "form", "input", "button", "iframe", "link", "object", "embed", "video", "audio"}
        assert not re.search(r"\son\w+\s*=", doc)  # no event handlers
        assert "@import" not in doc and "url(" not in doc
        for t, a in found:
            if t == "img":
                assert a["src"].startswith("cid:") and a.get("alt")


def test_t_html_008_only_brand_urls(client, outbox):
    _doi, result = submit_and_confirm(client, outbox)
    doc = result["html"]
    allowed = (f"{SIMPLIFY}/", IMPRINT)
    refs = [a.get("href") or a.get("src") for _, a in tags(doc) if a.get("href") or a.get("src")]
    assert refs and all(r.startswith(allowed) or r.startswith("cid:") for r in refs), refs
    for url in re.findall(r"https?://[^\s\"'<>&]+", doc):
        assert url.startswith(allowed), url


# ------------------------------------------------------------------ T-HTML-009..011 brand profile


def test_t_html_009_second_brand_own_values_and_inline_logo(client, outbox, monkeypatch):
    assert client.request("PUT", "/api/admin/brands/re-cloud", headers=ADMIN, json=RECLOUD).status_code == 201
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_ALLOWED_ORIGINS", ["https://site.test", "https://re-cloud.test"])
    _doi, result = submit_and_confirm(client, outbox, origin={"Origin": "https://re-cloud.test"}, email="sam@example.com")
    doc = result["html"]
    assert "#112233" in doc and "#445566" in doc and "Re-Cloud Holding" in doc and "Re-Cloud Team" in doc
    assert 'href="https://re-cloud.test/de/impressum"' in doc and 'href="https://re-cloud.test/de/datenschutz"' in doc
    assert 'href="https://re-cloud.test/de/selbst-check"' in doc
    for foreign in ("Simplify", "simplify-erp", "#0074b2", "#ea5562", "Ashant"):
        assert foreign not in doc, foreign
    (img,) = [a for t, a in tags(doc) if t == "img"]
    assert img["src"] == "cid:brand-logo" and img["alt"] == "Re-Cloud" and img["width"] == "80" and img["height"] == "40"
    assert result["attachments"] == [("logo.png", "image/png", "brand-logo")]
    _msg, ps = parts(result["mime"])
    types = [t for t, _ in ps]
    assert types[:5] == ["multipart/alternative", "text/plain", "multipart/related", "text/html", "image/png"]
    logo = ps[4][1]
    assert logo["Content-ID"] == "<brand-logo>" and logo.get_content() == png()
    for url in re.findall(r"https?://[^\s\"'<>&]+", doc):
        assert url.startswith("https://re-cloud.test/"), url


def test_t_html_010_webp_logo_falls_back_to_wordmark(client, outbox):
    body = {"logo": {"mime": "image/webp", "data": base64.b64encode(WEBP).decode()}, "wordmark": "Simplify Mark"}
    assert client.request("PUT", "/api/admin/brands/simplify-erp", headers=ADMIN, json=body).status_code == 200
    _doi, result = submit_and_confirm(client, outbox)
    assert 'id="mx-wordmark"' in result["html"] and "Simplify Mark" in result["html"] and "<img" not in result["html"]
    assert result["attachments"] == []


def test_t_html_011_no_profile_plain_text_only(client, outbox):
    """Sites without a profile behave as before 0006: plain text only (DOI + result)."""
    assert client.request("DELETE", "/api/admin/brands/simplify-erp", headers=ADMIN).status_code == 200
    doi, result = submit_and_confirm(client, outbox)
    assert doi["html"] is None and result["html"] is None and result["attachments"] == []
    assert result["body"].endswith(selfcheck_api.SIGNATURE["de"])
    _msg, ps = parts(result["mime"])
    assert [t for t, _ in ps] == ["text/plain"]


# ------------------------------------------------------------------ T-HTML-012 DOI


def test_t_html_012_doi_mail_same_frame(client, outbox):
    doi, _result = submit_and_confirm(client, outbox, name="<b>Eve</b>")
    link = re.search(r"(https://mauxy\.test/api/selfcheck/confirm\?token=\S+)", doi["body"]).group(1)
    doc = doi["html"]
    assert doi["attachments"] == [] and 'id="mx-wordmark"' in doc and 'id="mx-legal-imprint"' in doc
    btn = [a for t, a in tags(doc) if a.get("id") == "mx-button-link"][0]
    assert btn["href"] == link and "Bestätigen" in doc
    assert "Eve" not in doc and "Eve" not in doi["body"]  # DOI: server text only
    _msg, ps = parts(doi["mime"])
    assert [t for t, _ in ps][:3] == ["multipart/alternative", "text/plain", "text/html"]
    subject, plain, html_en, atts_en = selfcheck_api.doi_mail("simplify-erp", "en", ["mail"], "https://mauxy.test/x?token=t")
    assert atts_en == []  # seeded simplify-erp has no logo
    assert subject == "Please confirm: your self-check" and 'lang="en"' in html_en and ">Confirm</a>" in html_en
    assert "Imprint" in html_en and plain.endswith(selfcheck_api.SIGNATURE["en"])


# ------------------------------------------------------------------ T-HTML-013 colours


def test_t_html_013_colours_readable_for_any_brand():
    light = brand.Brand(site="x", display_name="X", color_primary="#80d2ff", color_accent="#ffe066")
    pal = mail_html.Palette(light)
    assert mail_html.contrast(pal.link, "#ffffff") >= 4.5          # links darkened
    assert pal.on_button == "#111111" and pal.on_primary == "#111111"  # dark text on light colours
    dark = mail_html.Palette(brand.Brand(site="y", display_name="Y", color_primary="#112233"))
    assert dark.on_primary == "#ffffff" and dark.button == "#112233" and dark.link == "#112233"
    assert mail_html.Palette(brand.Brand(site="z", display_name="Z")).primary == mail_html.FALLBACK_PRIMARY
    cta = mail_html.Palette(brand.Brand(site="s", display_name="S", color_primary="#0074b2", color_accent="#ea5562"))
    assert cta.on_button == "#ffffff"  # website: white bold text on the red CTA (large text, 3:1)
    with pytest.raises(ValueError):
        ids = mail_html.Ids("t")
        ids("a"), ids("a")


# ------------------------------------------------------------------ T-HTML-014..015 seed, image


def test_t_html_014_patch_0011_accent(client, tmp_path):
    assert brand.get("simplify-erp").color_accent == "#ea5562"
    path = tmp_path / "a.db"
    conn = sqlite3.connect(path)
    patches = db_migrate.list_patches()
    for v, _n, p in patches:
        if v < 11:
            conn.executescript(p.read_text())
    conn.execute("UPDATE site_brand SET color_accent = '#123456' WHERE site = 'simplify-erp'")  # owner's own value
    conn.executescript((db_migrate.PATCHES_DIR / "0011_simplify_accent_cta.sql").read_text())
    assert conn.execute("SELECT color_accent FROM site_brand").fetchone() == ("#123456",)
    conn.close()


def test_t_html_015_dockerfile_ships_mail_html():
    docker = (HERE.parent / "Dockerfile").read_text()
    assert "mail_html.py" in docker


# ------------------------------------------------------------------ T-HTML-020..024 XSS


@pytest.mark.parametrize("case", CASES["xss"], ids=[c["id"] for c in CASES["xss"]])
def test_t_html_020_024_name_escaped(case, client, outbox):
    _doi, result = submit_and_confirm(client, outbox, name=case["name"])
    assert case["escaped"] in result["html"]
    for bad in case["forbidden"]:
        assert bad not in result["html"]
    assert result["body"].split("\n", 1)[0] == case["plain_first_line"]
    anchors = [a["href"] for t, a in tags(result["html"]) if t == "a"]
    assert anchors and all(h.startswith((f"{SIMPLIFY}/", IMPRINT)) for h in anchors)  # no link from the name


def test_t_html_014_doi_mail_carries_only_the_brand_logo(client, outbox, monkeypatch):
    """A brand with a PNG logo: the DOI mail shows it inline (cid:brand-logo), the only attachment allowed."""
    assert client.request("PUT", "/api/admin/brands/re-cloud", headers=ADMIN, json=RECLOUD).status_code == 201
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_ALLOWED_ORIGINS", ["https://site.test", "https://re-cloud.test"])
    doi, _result = submit_and_confirm(client, outbox, origin={"Origin": "https://re-cloud.test"}, email="lia@example.com")
    assert doi["attachments"] == [("logo.png", "image/png", "brand-logo")]
    (img,) = [a for t, a in tags(doi["html"]) if t == "img"]
    assert img["src"] == "cid:brand-logo"


def test_t_html_015_doi_mail_refuses_any_other_attachment():
    good = mailer.Attachment(filename="logo.png", content=png(), mime_type="image/png", inline_cid=mailer.BRAND_LOGO_CID)
    mailer.build("x@example.com", "s", "b", html="<p>x</p>", attachments=[good], audience="doi")
    bad = [
        [mailer.Attachment(filename="r.pdf", content=b"%PDF-1.4 x", mime_type="application/pdf")],
        [mailer.Attachment(filename="logo.png", content=png(), mime_type="image/png", inline_cid="other")],
        [mailer.Attachment(filename="logo.png", content=png(), mime_type="image/png")],
        [good, good],
    ]
    for atts in bad:
        with pytest.raises(mailer.MailRejected):
            mailer.build("x@example.com", "s", "b", html="<p>x</p>", attachments=atts, audience="doi")


# ------------------------------------------------------------------ T-HTML-050..057 dark mode

BRANDS = {  # simplify (seed + 0011), very dark, very light, no colours (frame default)
    "simplify": ("#0074b2", "#ea5562"), "dark": ("#112233", "#445566"), "light": ("#80d2ff", "#ffe066"),
    "none": (None, None),
}
LEVELS = ("#1E7B4F", "#A35F00", "#C0392B")  # gut / prüfen / handeln (vendor/selfcheck/selfcheck.json)


def palette(key):
    primary, accent = BRANDS[key]
    pal = mail_html.Palette(brand.Brand(site=key, display_name=key, color_primary=primary, color_accent=accent))
    for c in LEVELS:
        pal.level(c)
    return pal


def mails(client, outbox, monkeypatch, *, logo: bool):
    """(doi, result) for the seeded simplify-erp (wordmark header) or re-cloud (PNG logo)."""
    if not logo:
        return submit_and_confirm(client, outbox, stay={"licence": 1000, "support": 10})
    assert client.request("PUT", "/api/admin/brands/re-cloud", headers=ADMIN, json=RECLOUD).status_code == 201
    monkeypatch.setattr(selfcheck_api, "SELFCHECK_ALLOWED_ORIGINS", ["https://site.test", "https://re-cloud.test"])
    return submit_and_confirm(client, outbox, origin={"Origin": "https://re-cloud.test"}, email="dm@example.com",
                              stay={"licence": 1000, "support": 10})


def style_of(doc):
    return re.search(r"<style[^>]*>(.*?)</style>", doc, re.S).group(1)


def media_dark(css):
    """Rules inside @media (prefers-color-scheme:dark){...} as {class: {prop: value}}, + the css after it."""
    head = "@media (prefers-color-scheme:dark){"
    start = css.index(head) + len(head)
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return rules_of(css[start:i - 1], ""), css[i:]


def rules_of(css, prefix):
    out: dict[str, dict[str, str]] = {}
    for sel, body in re.findall(re.escape(prefix) + r"\.(mx-[\w-]+)\{([^}]*)\}", css):
        for decl in body.split(";"):
            prop, value = decl.split(":", 1)
            assert value.endswith(" !important"), decl
            out.setdefault(sel, {})[prop] = value[: -len(" !important")]
    return out


@pytest.mark.parametrize("logo", [False, True], ids=["T-HTML-050-wordmark", "T-HTML-050-logo"])
def test_t_html_050_scheme_metas_media_query_outlook_blocks(logo, client, outbox, monkeypatch):
    for m in mails(client, outbox, monkeypatch, logo=logo):
        doc = m["html"]
        metas = {a.get("name"): a.get("content") for t, a in tags(doc) if t == "meta"}
        assert metas["color-scheme"] == "light dark" and metas["supported-color-schemes"] == "light dark"
        css = style_of(doc)
        assert css.startswith(":root{color-scheme:light dark;")
        dark, rest = media_dark(css)
        ogsc, ogsb = rules_of(rest, "[data-ogsc] "), rules_of(rest, "[data-ogsb] ")
        assert ogsc and ogsb
        # Outlook duplicates: every media rule once more, text / border -> ogsc, backgrounds -> ogsb
        for cls, props in dark.items():
            for prop, value in props.items():
                target = ogsb if prop.startswith("background") else ogsc
                assert target[cls][prop] == value, (cls, prop)
        assert sum(map(len, dark.values())) == sum(map(len, ogsc.values())) + sum(map(len, ogsb.values()))
        for must in ("mx-bg", "mx-card", "mx-head", "mx-soft", "mx-stripe", "mx-text", "mx-muted", "mx-faint",
                     "mx-line", "mx-link", "mx-btn", "mx-btn-text", "mx-pre"):
            assert must in dark, must


TEXT_COLOR = re.compile(r"(?:^|;)\s*color\s*:")


@pytest.mark.parametrize("logo", [False, True], ids=["T-HTML-051-wordmark", "T-HTML-051-logo"])
def test_t_html_051_every_coloured_element_has_its_dark_class(logo, client, outbox, monkeypatch):
    for m in mails(client, outbox, monkeypatch, logo=logo):
        doc = m["html"]
        dark, _ = media_dark(style_of(doc))
        found = tags(doc)
        ids = [a.get("id") for _, a in found]
        assert all(ids) and len(ids) == len(set(ids))  # ids stay unique
        checked = 0
        for t, a in found:
            style = a.get("style") or ""
            props = set()
            for cls in (a.get("class") or "").split():
                props |= set(dark.get(cls, {}))
            if TEXT_COLOR.search(style):
                assert "color" in props, (t, a.get("id"), a.get("class"))
                checked += 1
            if a.get("bgcolor") or re.search(r"background(-color)?\s*:", style):
                assert "background-color" in props, (t, a.get("id"), a.get("class"))
                checked += 1
            if re.search(r"border(-\w+)?\s*:[^;]*#", style):
                assert "border-color" in props, (t, a.get("id"), a.get("class"))
                checked += 1
        assert checked >= 15


def test_t_html_051_result_mail_dark_classes_in_place(client, outbox):
    _doi, result = submit_and_confirm(client, outbox)
    found = {a["id"]: a.get("class", "") for _t, a in tags(result["html"])}
    ev = selfcheck_api.engine.evaluate(selfcheck_api.RULES, COMPLETE, "de")
    for d in ev["dims"]:  # dot + label: the level's own dark colour
        lvl = "mx-lvl-" + d["color"].lower()[1:]
        assert lvl in found[f"mx-dim-{d['id']}-dot"].split() and lvl in found[f"mx-dim-{d['id']}-level"].split()
    assert found[f"mx-risk-{ev['risks'][0]['id']}-n"] == "mx-num"
    assert found[f"mx-wave-{ev['waves'][0]['id']}-n"] == "mx-num-solid"
    assert found["mx-quote"] == "mx-quote" and found["mx-bar-fill"] == "mx-bar-fill"
    assert found["mx-button-cell"] == "mx-btn" and found["mx-button-link"] == "mx-btn-text"
    assert "mx-chip" in found["mx-chip0"] and found["mx-headline"] == "mx-text mx-h1"


@pytest.mark.parametrize("key", list(BRANDS), ids=[f"T-HTML-052-{k}" for k in BRANDS])
def test_t_html_052_dark_contrast_pairs(key):
    pal = palette(key)
    C = mail_html.contrast
    card, soft = mail_html.DARK_CARD_BG, mail_html.DARK_SOFT_BG
    # body text, muted text, footer, chips, links: 4.5:1 on every surface they sit on
    for fg, bg in [(mail_html.DARK_TEXT, card), (mail_html.DARK_TEXT, soft), (mail_html.DARK_MUTED, card),
                   (mail_html.DARK_FAINT, soft), (mail_html.DARK_FAINT, card), (mail_html.DARK_TEXT, mail_html.DARK_CHIP_BG),
                   (mail_html.DARK_TEXT, pal.dark_tint), (mail_html.DARK_MUTED, pal.dark_tint),
                   (pal.dark_link, card), (pal.dark_tint_link, pal.dark_tint)]:
        assert C(fg, bg) >= 4.5, (key, fg, bg)
    for c, d in pal.levels.items():  # traffic-light labels (small bold text)
        assert C(d, card) >= 4.5, (c, d)
    # large bold button text 3:1, the button 3:1 against the dark card, quote bar 3:1 on its box
    assert C(pal.dark_on_button, pal.dark_button) >= 3.0
    assert C(pal.dark_button, card) >= 3.0
    assert C(pal.dark_bar, pal.dark_tint) >= 3.0
    assert C(pal.on_primary, pal.primary) >= 3.0  # wave number box keeps the brand colour in dark mode


def test_t_html_053_simplify_cta_stays_brand_red_with_white_text():
    pal = palette("simplify")
    assert (pal.button, pal.on_button) == ("#ea5562", "#ffffff")
    assert (pal.dark_button, pal.dark_on_button) == ("#ea5562", "#ffffff")


def test_t_html_054_traffic_lights_distinguishable_in_dark():
    pal = palette("simplify")
    hues = []
    for c in (x.lower() for x in LEVELS):
        h0 = colorsys.rgb_to_hsv(*(v / 255 for v in mail_html._rgb(c)))[0]
        h1, s1, _v = colorsys.rgb_to_hsv(*(v / 255 for v in mail_html._rgb(pal.levels[c])))
        assert abs(h0 - h1) < 0.02 and s1 > 0.25  # same hue as the light colour, still saturated
        hues.append(h1 * 360)
    for i in range(3):
        for j in range(i + 1, 3):
            d = abs(hues[i] - hues[j])
            assert min(d, 360 - d) >= 25, hues  # green / amber / red stay apart


@pytest.mark.parametrize("key", list(BRANDS), ids=[f"T-HTML-055-{k}" for k in BRANDS])
def test_t_html_055_light_pairs_survive_forced_inversion(key):
    """Gmail apps ignore the media query and invert the light design themselves: every light pair is
    readable to begin with (no light grey on white), so the inverted pairs stay readable too."""
    pal = palette(key)
    C = mail_html.contrast
    for fg, bg in [(mail_html.TEXT, mail_html.CARD_BG), (mail_html.MUTED, mail_html.CARD_BG),
                   (mail_html.FAINT, mail_html.SOFT_BG), (mail_html.TEXT, mail_html.CHIP_BG),
                   (mail_html.MUTED, pal.tint), (pal.link, mail_html.CARD_BG), (pal.tint_link, pal.tint)]:
        assert C(fg, bg) >= 4.5, (key, fg, bg)
    for c in LEVELS:
        assert C(c, mail_html.CARD_BG) >= 4.5
    assert C(pal.on_button, pal.button) >= 3.0


def test_t_html_056_logo_on_white_badge_in_every_mode(client, outbox, monkeypatch):
    doi, result = mails(client, outbox, monkeypatch, logo=True)
    for m in (doi, result):
        doc = m["html"]
        found = tags(doc)
        cell = [a for t, a in found if a.get("id") == "mx-badge-cell"][0]
        assert cell["class"] == "mx-badge" and cell["bgcolor"] == "#ffffff"
        assert "background-image:linear-gradient(#ffffff,#ffffff)" in cell["style"]  # Gmail does not invert it
        assert doc.index('id="mx-header"') < doc.index('id="mx-badge-cell"') < doc.index('id="mx-logo"')
        dark, rest = media_dark(style_of(doc))
        assert dark["mx-badge"] == {"background-color": "#ffffff"}
        assert rules_of(rest, "[data-ogsb] ")["mx-badge"] == {"background-color": "#ffffff"}
        assert dark["mx-head"]["background-color"] == mail_html.DARK_CARD_BG  # header goes dark, badge stays
        assert m["attachments"] == [("logo.png", "image/png", "brand-logo")]  # still exactly one inline image
        assert [a["src"] for t, a in found if t == "img"] == ["cid:brand-logo"]


def test_t_html_057_dark_classes_are_fixed_tokens(client, outbox):
    """Dark classes are fixed tokens or a validated #RRGGBB: a rules colour that is not #RRGGBB gets no
    class of its own (falls back to mx-text); the name stays escaped (cases T-HTML-020..024)."""
    pal = mail_html.Palette(None)
    assert pal.level("red;}</style><script>") == "mx-text" and pal.levels == {}
    assert pal.level("#1E7B4F") == "mx-lvl-1e7b4f"
    _doi, result = submit_and_confirm(client, outbox, name="<b>Eve</b>")
    assert "&lt;b&gt;Eve&lt;/b&gt;" in result["html"] and "<b>Eve" not in result["html"]
    assert re.fullmatch(r"[\w\s:;{}()#.,!@\-\[\]%]*", style_of(result["html"]))
