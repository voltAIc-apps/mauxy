"""
Branded HTML part of person-facing mails (owner request 2026-11-09: "the Selbst-Check result mail has to
be brand beautified"). The plain-text part stays the primary content and is sent as the
multipart/alternative fallback; this module only renders the HTML twin of the same texts.

Used by selfcheck_api.py: the Selbst-Check result mail (full layout, mirrors the website's result page
SelfCheckResult.tsx) and the Selbst-Check DOI mail (same frame, letter body). Sites WITHOUT a brand profile
get no HTML part at all (plain text exactly as before 0006, LUDO regression contract).

Brand values come from the profile (brand.Brand) only: display name, wordmark, logo, colours, legal entity,
website / imprint / privacy URLs, mail footer. Nothing site-specific is hard-coded here; the neutral greys
below are the frame, not a brand.

E-mail client rules (Outlook / Gmail / Apple Mail / mobile):
  - table layout, inline CSS on every element, width 600 / max-width 600px, one <style> block in <head>
    only for the mobile and dark-mode media queries (progressive enhancement; the mail works without it)
  - no scripts, no forms, no external resources: no web fonts, no remote images, no tracking pixel. The
    logo goes inline via Content-ID (multipart/related) and only as PNG (WebP is not shown by Outlook);
    without a PNG logo the header is the styled wordmark text.
  - colours: the brand's primary / accent; text colour on a coloured surface is picked by WCAG contrast,
    link colour darkened until it reaches 4.5:1 on white, so a light brand colour stays readable.
  - dark mode: explicit light colours inline (clients that invert get a predictable start), a
    prefers-color-scheme block for clients that honour it; a logo always sits on its own white header
    panel (no dark-on-transparent logo on a dark background).
  - lang attribute, preheader text, alt text, role="presentation" on layout tables, every element with
    a unique id (owner rule).

Security: every value is html.escape()d (person's name, brand values, rule texts); URLs come from the
validated brand profile / mauxy itself and are escaped as attributes; links are only created for URLs the
caller passes explicitly (no free-text linkification).
"""
from __future__ import annotations

import html
import struct
from typing import Iterable, Optional

import mailer

LOGO_CID = mailer.BRAND_LOGO_CID
LOGO_HEIGHT = 40          # px in the header
LOGO_MAX_WIDTH = 220

# neutral frame (not brand): page, card, text, lines -- the greys of the website's result page
PAGE_BG = "#f4f6f8"
CARD_BG = "#ffffff"
SOFT_BG = "#f8f9fa"
TEXT = "#22272b"
MUTED = "#495057"
FAINT = "#5c636a"
LINE = "#dee2e6"
CHIP_BG = "#e9ecef"
FALLBACK_PRIMARY = "#1d4ed8"   # same default as confirm_page.py for a profile without colours

SANS = "'Source Sans Pro','Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',Times,serif"

# Frame wording only (the content itself comes from the rules engine / the plain-text mail).
TEXTS = {
    "de": {"button": "Ergebnis online ansehen", "quote": "Richtangebot",
           "precision": "Genauigkeit: {word} · aus {answered} von {total} Antworten",
           "imprint": "Impressum", "privacy": "Datenschutz"},
    "en": {"button": "View your result online", "quote": "Indicative quote",
           "precision": "Precision: {word} · from {answered} of {total} answers",
           "imprint": "Imprint", "privacy": "Privacy"},
}


# ------------------------------------------------------------------------------- colour helpers


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _hex(rgb: Iterable[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def luminance(hex_: str) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(hex_: str, other: str, f: float) -> str:
    """f = share of `other` (0..1)."""
    return _hex(a + (b - a) * f for a, b in zip(_rgb(hex_), _rgb(other)))


def on_color(bg: str, min_ratio: float = 4.5) -> str:
    """White text on `bg` when it reaches min_ratio, else near-black."""
    return "#ffffff" if contrast(bg, "#ffffff") >= min_ratio else "#111111"


def ink(hex_: str, bg: str = CARD_BG, min_ratio: float = 4.5) -> str:
    """`hex_` darkened (towards black) until it reads as text on `bg`."""
    out = hex_
    for step in range(1, 11):
        if contrast(out, bg) >= min_ratio:
            return out
        out = mix(hex_, "#000000", step / 10)
    return out


class Palette:
    """Colours of one brand (validated #RRGGBB from the profile, else the frame default)."""

    def __init__(self, brand):
        self.primary = (brand.color_primary if brand and brand.color_primary else FALLBACK_PRIMARY).lower()
        self.accent = (brand.color_accent if brand and brand.color_accent else self.primary).lower()
        self.link = ink(self.primary)                      # links, eyebrow, wordmark on white
        self.on_primary = on_color(self.primary)           # number boxes on primary
        self.tint = mix(self.primary, "#ffffff", 0.88)     # risk number circle, quote box
        self.button = self.accent
        self.on_button = on_color(self.button, 3.0)        # large bold text: WCAG 3:1
        self.dark_link = mix(self.primary, "#ffffff", 0.45)  # links on the dark-mode card


# ------------------------------------------------------------------------------- html helpers


class Ids:
    """Unique id per element (owner rule); raises on a duplicate (a template bug, caught by tests)."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.seen: set[str] = set()

    def __call__(self, name: str) -> str:
        value = f"{self.prefix}-{name}"
        if value in self.seen:
            raise ValueError(f"duplicate id {value}")
        self.seen.add(value)
        return html.escape(value)


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def png_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 24 or not mailer.MAGIC["image/png"](data) or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return (w, h) if w > 0 and h > 0 else None


def logo_attachment(brand) -> Optional[mailer.Attachment]:
    """The profile's logo as an inline image (PNG only), or None (wordmark header)."""
    if brand is None or not brand.logo or brand.logo_mime != "image/png" or png_size(brand.logo) is None:
        return None
    return mailer.Attachment(filename="logo.png", content=bytes(brand.logo), mime_type="image/png",
                             inline_cid=LOGO_CID)


def text_html(ids: Ids, name: str, text: str, links: dict[str, str], pal: Palette, *, style: str = "") -> str:
    """One paragraph of plain text: escaped, line breaks -> <br>, and exactly the URLs in `links`
    (url -> id suffix) become anchors. No other text is ever turned into a link."""
    parts = []
    for i, line in enumerate(text.split("\n")):
        if i:
            parts.append(f'<br id="{ids(f"{name}-br{i}")}">')
        out = esc(line)
        for url, key in links.items():
            e = esc(url)
            if e in out:
                out = out.replace(e, f'<a id="{ids(f"{name}-{key}")}" class="mx-link" href="{e}" '
                                     f'style="color:{pal.link};text-decoration:underline;word-break:normal;overflow-wrap:break-word">{e}</a>', 1)
        parts.append(out)
    return (f'<p id="{ids(name)}" class="mx-text" style="margin:0 0 16px;font-family:{SANS};font-size:16px;'
            f'line-height:1.55;color:{TEXT};{style}">{"".join(parts)}</p>')


def button(ids: Ids, name: str, label: str, url: str, pal: Palette) -> str:
    """Bulletproof button: a table cell with the colour (Outlook ignores padding on <a>)."""
    return (
        f'<table id="{ids(name)}" role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:8px 0 24px;border-collapse:separate">'
        f'<tr id="{ids(name + "-row")}"><td id="{ids(name + "-cell")}" bgcolor="{pal.button}" '
        f'style="background:{pal.button};border-radius:8px;mso-padding-alt:14px 26px">'
        f'<a id="{ids(name + "-link")}" href="{esc(url)}" '
        f'style="display:inline-block;padding:14px 26px;font-family:{SANS};font-size:19px;font-weight:700;'
        f'line-height:1.2;color:{pal.on_button};text-decoration:none;border-radius:8px">{esc(label)}</a>'
        f'</td></tr></table>')


def h2(ids: Ids, name: str, text: str) -> str:
    return (f'<h2 id="{ids(name)}" class="mx-text" style="margin:28px 0 12px;font-family:{SERIF};font-size:21px;'
            f'line-height:1.3;font-weight:700;color:{TEXT}">{esc(text)}</h2>')


def _style_block(pal: Palette, *, logo: bool) -> str:
    """Media queries only (mobile + dark). Everything else is inline."""
    head_dark = "" if logo else ".mx-head{background:#1a1f24 !important}.mx-wordmark{color:%s !important}" % pal.dark_link
    return (
        "@media only screen and (max-width:620px){"
        ".mx-outer{padding:12px 0 !important}.mx-pad{padding-left:20px !important;padding-right:20px !important}"
        ".mx-h1{font-size:23px !important}}"
        "@media (prefers-color-scheme:dark){"
        ".mx-bg{background:#0f1215 !important}.mx-card{background:#1a1f24 !important;border-color:#2e353c !important}"
        ".mx-soft{background:#151a1e !important}.mx-text{color:#e8eaed !important}.mx-muted{color:#aab2bb !important}"
        ".mx-line{border-color:#2e353c !important}.mx-chip{background:#2a3036 !important;color:#e8eaed !important}"
        f".mx-link{{color:{pal.dark_link} !important}}.mx-quote{{background:#16232d !important}}"
        f"{head_dark}}}"
    )


def frame(*, brand, lang: str, title: str, preheader: str, inner: str, ids: Ids, pal: Palette,
          footer_lines: list[str], logo: Optional[mailer.Attachment], legal_links: list[tuple[str, str]]) -> str:
    """The branded document around `inner`: header (logo or wordmark), content, signature, legal footer."""
    if logo is not None:
        w, h = png_size(logo.content)
        height = LOGO_HEIGHT
        width = max(1, round(w * height / h))
        if width > LOGO_MAX_WIDTH:
            width, height = LOGO_MAX_WIDTH, max(1, round(h * LOGO_MAX_WIDTH / w))
        mark = (f'<img id="{ids("logo")}" src="cid:{LOGO_CID}" width="{width}" height="{height}" '
                f'alt="{esc(brand.display_name)}" style="display:block;border:0;outline:none;height:{height}px;'
                f'width:{width}px;max-width:100%">')
    else:
        mark = (f'<span id="{ids("wordmark")}" class="mx-wordmark" style="font-family:{SANS};font-size:24px;'
                f'font-weight:700;letter-spacing:-0.01em;color:{pal.link}">{esc(brand.wordmark or brand.display_name)}</span>')

    sig = ""
    if footer_lines:
        sig = text_html(ids, "signature", "\n".join(footer_lines), {}, pal,
                        style=f"margin:28px 0 0;padding-top:16px;border-top:1px solid {LINE};color:{MUTED};font-size:15px")
        sig = sig.replace('class="mx-text"', 'class="mx-muted mx-line"', 1)

    legal = [f'<span id="{ids("legal-name")}">{esc(brand.display_name)}</span>']
    if brand.legal_entity and brand.legal_entity != brand.display_name:
        legal.append(f'<span id="{ids("legal-entity")}">{esc(brand.legal_entity)}</span>')
    for key, url, label in legal_links:
        legal.append(f'<a id="{ids("legal-" + key)}" class="mx-muted" href="{esc(url)}" '
                     f'style="color:{FAINT};text-decoration:underline">{esc(label)}</a>')
    sep = f'<span id="{{}}" style="color:{FAINT}"> · </span>'
    legal_html = ""
    for i, part in enumerate(legal):
        legal_html += (sep.format(ids(f"legal-sep{i}")) if i else "") + part

    pad = "͏ " * 60  # keeps the body text out of the inbox preview after the preheader
    return (
        f'<!doctype html><html id="{ids("html")}" lang="{esc(lang)}">'
        f'<head id="{ids("head")}"><meta id="{ids("charset")}" charset="utf-8">'
        f'<meta id="{ids("viewport")}" name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta id="{ids("format")}" name="x-apple-disable-message-reformatting" content="">'
        f'<meta id="{ids("scheme")}" name="color-scheme" content="light dark">'
        f'<meta id="{ids("schemes")}" name="supported-color-schemes" content="light dark">'
        f'<title id="{ids("title")}">{esc(title)}</title>'
        f'<style id="{ids("style")}">{_style_block(pal, logo=logo is not None)}</style></head>'
        f'<body id="{ids("body")}" class="mx-bg" style="margin:0;padding:0;background:{PAGE_BG};'
        f'-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%">'
        f'<div id="{ids("preheader")}" style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;'
        f'opacity:0;overflow:hidden;mso-hide:all;color:{PAGE_BG}">{esc(preheader)}{pad}</div>'
        f'<table id="{ids("wrap")}" class="mx-bg" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{PAGE_BG}" style="background:{PAGE_BG};width:100%">'
        f'<tr id="{ids("wrap-row")}"><td id="{ids("outer")}" class="mx-outer" align="center" style="padding:24px 12px">'
        f'<table id="{ids("card")}" class="mx-card" role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{CARD_BG}" style="width:100%;max-width:600px;background:{CARD_BG};'
        f'border:1px solid {LINE};border-radius:12px;border-collapse:separate;overflow:hidden">'
        f'<tr id="{ids("stripe-row")}"><td id="{ids("stripe")}" height="5" bgcolor="{pal.primary}" '
        f'style="height:5px;line-height:5px;font-size:0;background:{pal.primary};border-radius:12px 12px 0 0">&nbsp;</td></tr>'
        f'<tr id="{ids("head-row")}"><td id="{ids("header")}" class="mx-head mx-pad mx-line" bgcolor="#ffffff" '
        f'style="padding:20px 32px;background:#ffffff;border-bottom:1px solid {LINE}">{mark}</td></tr>'
        f'<tr id="{ids("content-row")}"><td id="{ids("content")}" class="mx-pad" '
        f'style="padding:28px 32px 32px;font-family:{SANS};color:{TEXT}">{inner}{sig}</td></tr>'
        f'<tr id="{ids("foot-row")}"><td id="{ids("footer")}" class="mx-soft mx-pad mx-line" bgcolor="{SOFT_BG}" '
        f'style="padding:18px 32px;background:{SOFT_BG};border-top:1px solid {LINE};border-radius:0 0 12px 12px;'
        f'font-family:{SANS};font-size:13px;line-height:1.6;color:{FAINT}">'
        f'<p id="{ids("legal")}" class="mx-muted" style="margin:0;color:{FAINT}">{legal_html}</p></td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def legal_links(brand, lang: str) -> list[tuple[str, str, str]]:
    t = TEXTS[lang]
    out = []
    for key, url in (("imprint", brand.imprint_url), ("privacy", brand.privacy_url)):
        if url:
            out.append((key, url.replace("{lang}", lang), t[key]))
    return out


# ------------------------------------------------------------------------------- Selbst-Check mails


def selfcheck_result(brand, lang: str, p: dict) -> tuple[Optional[str], list[mailer.Attachment]]:
    """HTML part + inline images of the result mail; (None, []) for a site without a brand profile.

    `p` (built by selfcheck_api.result_parts from the rules engine; the plain text uses the same values):
      subject, greeting, intro (paragraphs after the greeting), url, base, complete, answered, total,
      precision_word, ev (engine.evaluate or None), labels (rules mail labels for lang), stay_lines,
      answers (engine.answer_lines), outro, footer_lines
    """
    if brand is None:
        return None, []
    lang = lang if lang in TEXTS else "de"
    t = TEXTS[lang]
    pal = Palette(brand)
    ids = Ids("mx")
    logo = logo_attachment(brand)
    lab = p["labels"]
    url = p["url"]
    precision = t["precision"].format(word=p["precision_word"], answered=p["answered"], total=p["total"])
    out: list[str] = [
        f'<p id="{ids("eyebrow")}" class="mx-link" style="margin:0 0 18px;font-family:{SANS};font-size:13px;'
        f'font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:{pal.link}">{esc(p["subject"])}</p>',
        text_html(ids, "greeting", p["greeting"], {}, pal),
    ]
    for i, para in enumerate(p["intro"]):
        out.append(text_html(ids, f"intro{i}", para, {url: "url"}, pal))

    ev = p["ev"]
    if ev is not None:
        chips = "".join(
            f'<span id="{ids(f"chip{i}")}" class="mx-chip" style="display:inline-block;margin:0 6px 6px 0;'
            f'padding:4px 12px;border-radius:999px;background:{CHIP_BG};color:{TEXT};font-size:14px;'
            f'line-height:1.4">{esc(c)}</span>' for i, c in enumerate(ev["chips"]))
        out += [
            f'<h1 id="{ids("headline")}" class="mx-text mx-h1" style="margin:12px 0 14px;font-family:{SERIF};'
            f'font-size:27px;line-height:1.25;font-weight:700;color:{TEXT}">{esc(ev["headline"])}</h1>',
            f'<p id="{ids("chips")}" style="margin:0 0 8px;font-family:{SANS}">{chips}</p>',
            h2(ids, "lights-title", lab["lights"]),
        ]
        rows = []
        for d in ev["dims"]:
            k = f"dim-{d['id']}"
            last = d is ev["dims"][-1]
            border = "" if last else f"border-bottom:1px solid {LINE};"
            rows.append(
                f'<tr id="{ids(k)}">'
                f'<td id="{ids(k + "-dot")}" class="mx-line" width="22" valign="top" aria-hidden="true" '
                f'style="{border}padding:12px 0;width:22px;font-size:18px;line-height:22px;color:{esc(d["color"])}">&#9679;</td>'
                f'<td id="{ids(k + "-body")}" class="mx-line" valign="top" style="{border}padding:12px 8px 12px 4px">'
                f'<span id="{ids(k + "-name")}" class="mx-text" style="display:block;font-weight:700;font-size:16px;'
                f'line-height:22px;color:{TEXT}">{esc(d["name"])}</span>'
                f'<span id="{ids(k + "-note")}" class="mx-muted" style="display:block;font-size:15px;line-height:1.45;'
                f'color:{MUTED}">{esc(d["note"])}</span></td>'
                f'<td id="{ids(k + "-level")}" class="mx-line" valign="top" align="right" style="{border}padding:12px 0;'
                f'white-space:nowrap;font-size:13px;line-height:22px;font-weight:700;letter-spacing:0.05em;'
                f'text-transform:uppercase;color:{esc(d["color"])}">{esc(d["label"])}</td></tr>')
        out.append(f'<table id="{ids("lights")}" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                   f'border="0" style="width:100%;font-family:{SANS}">{"".join(rows)}</table>')

        out.append(h2(ids, "risks-title", lab["risks"]))
        rows = []
        for r in ev["risks"]:
            k = f"risk-{r['id']}"
            more = ""
            if r["article"]:
                href = f"{p['base']}/{lang}/wissen/{r['article']}"
                more = (f'<a id="{ids(k + "-link")}" class="mx-link" href="{esc(href)}" style="display:inline-block;'
                        f'margin-top:4px;font-size:15px;font-weight:600;color:{pal.link};text-decoration:underline">'
                        f'{esc(lab["read"])} &rarr;</a>')
            rows.append(
                f'<tr id="{ids(k)}"><td id="{ids(k + "-n-cell")}" width="40" valign="top" style="width:40px;padding:0 0 18px">'
                f'<div id="{ids(k + "-n")}" aria-hidden="true" style="width:28px;height:28px;border-radius:14px;'
                f'background:{pal.tint};color:{pal.link};font-weight:700;font-size:15px;line-height:28px;'
                f'text-align:center">{r["n"]}</div></td>'
                f'<td id="{ids(k + "-body")}" valign="top" style="padding:2px 0 18px">'
                f'<span id="{ids(k + "-title")}" class="mx-text" style="display:block;font-weight:700;font-size:16px;'
                f'line-height:1.4;color:{TEXT}">{esc(r["title"])}</span>'
                f'<span id="{ids(k + "-rule")}" class="mx-muted" style="display:block;font-size:15px;line-height:1.5;'
                f'color:{MUTED}">{esc(r["rule"])}</span>{more}</td></tr>')
        out.append(f'<table id="{ids("risks")}" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                   f'border="0" style="width:100%;font-family:{SANS}">{"".join(rows)}</table>')

        out.append(h2(ids, "waves-title", lab["waves"]))
        rows = []
        for w in ev["waves"]:
            k = f"wave-{w['id']}"
            rows.append(
                f'<tr id="{ids(k)}"><td id="{ids(k + "-n-cell")}" width="40" valign="top" style="width:40px;padding:0 0 14px">'
                f'<div id="{ids(k + "-n")}" aria-hidden="true" style="width:28px;height:28px;border-radius:6px;'
                f'background:{pal.primary};color:{pal.on_primary};font-weight:700;font-size:15px;line-height:28px;'
                f'text-align:center">{w["n"]}</div></td>'
                f'<td id="{ids(k + "-body")}" valign="top" style="padding:2px 0 14px">'
                f'<span id="{ids(k + "-title")}" class="mx-text" style="display:block;font-weight:700;font-size:16px;'
                f'line-height:1.4;color:{TEXT}">{esc(w["title"])}</span>'
                f'<span id="{ids(k + "-items")}" class="mx-muted" style="display:block;font-size:15px;line-height:1.5;'
                f'color:{MUTED}">{esc(w["items"])}</span></td></tr>')
        out.append(f'<table id="{ids("waves")}" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                   f'border="0" style="width:100%;font-family:{SANS}">{"".join(rows)}</table>')

    # Richtangebot: how it follows (intro text above) + precision of the answers (bar = answered share)
    share = max(0, min(100, round(100 * p["answered"] / p["total"]))) if p["total"] else 0
    bar_rest = (f'<td id="{ids("bar-rest")}" width="{100 - share}%" bgcolor="{LINE}" style="background:{LINE};'
                f'height:6px;line-height:6px;font-size:0">&nbsp;</td>') if share < 100 else ""
    bar_fill = (f'<td id="{ids("bar-fill")}" width="{share}%" bgcolor="{pal.primary}" style="background:{pal.primary};'
                f'height:6px;line-height:6px;font-size:0">&nbsp;</td>') if share > 0 else ""
    out.append(
        f'<table id="{ids("quote")}" class="mx-quote" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{pal.tint}" style="width:100%;margin:24px 0 8px;background:{pal.tint};'
        f'border-left:4px solid {pal.primary};border-radius:0 8px 8px 0">'
        f'<tr id="{ids("quote-row")}"><td id="{ids("quote-cell")}" style="padding:16px 18px;font-family:{SANS}">'
        f'<span id="{ids("quote-title")}" class="mx-text" style="display:block;font-weight:700;font-size:16px;'
        f'line-height:1.4;color:{TEXT}">{esc(t["quote"])}</span>'
        f'<span id="{ids("precision")}" class="mx-muted" style="display:block;margin:2px 0 10px;font-size:15px;'
        f'line-height:1.5;color:{MUTED}">{esc(precision)}</span>'
        f'<table id="{ids("bar")}" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-radius:3px;overflow:hidden"><tr id="{ids("bar-row")}">{bar_fill}{bar_rest}</tr></table>'
        f'</td></tr></table>')

    if p["stay_lines"]:
        out.append(h2(ids, "stay-title", lab["stay"]))
        for i, line in enumerate(p["stay_lines"]):
            out.append(text_html(ids, f"stay{i}", line, {}, pal))

    out.append(f'<div id="{ids("cta")}" style="margin:28px 0 0">{button(ids, "button", t["button"], url, pal)}</div>')

    items = "".join(
        f'<li id="{ids(f"answer{i}")}" class="mx-muted" style="margin:0 0 6px;font-size:14px;line-height:1.45;'
        f'color:{MUTED}">{esc(a)}</li>' for i, a in enumerate(p["answers"]))
    out += [
        h2(ids, "answers-title", lab["answers"]),
        f'<ul id="{ids("answers")}" style="margin:0 0 20px;padding:0 0 0 18px;font-family:{SANS}">{items}</ul>',
    ]
    for i, para in enumerate(p["outro"]):
        out.append(text_html(ids, f"outro{i}", para, {url: "url"}, pal))

    preheader = ev["headline"] if ev is not None else precision
    doc = frame(brand=brand, lang=lang, title=p["subject"], preheader=preheader, inner="".join(out), ids=ids,
                pal=pal, footer_lines=p["footer_lines"], logo=logo, legal_links=legal_links(brand, lang))
    return doc, [logo] if logo else []


def letter(brand, lang: str, *, subject: str, paragraphs: list[str], footer_lines: list[str],
           link: Optional[str] = None, link_label: Optional[str] = None) -> tuple[Optional[str], list]:
    """Simple branded letter (DOI mail): the plain-text paragraphs in the frame; the paragraph that is
    exactly `link` becomes a button (+ the URL as small fallback text). Returns (html, attachments): the
    brand's PNG logo inline (mailer allows exactly that one on a DOI mail), else the wordmark header."""
    if brand is None:
        return None, []
    lang = lang if lang in TEXTS else "de"
    pal = Palette(brand)
    ids = Ids("mx")
    out = []
    for i, para in enumerate(paragraphs):
        lines = para.split("\n")
        if link and link in lines:
            before = "\n".join(lines[: lines.index(link)])
            if before:
                out.append(text_html(ids, f"p{i}", before, {}, pal))
            out.append(button(ids, "button", link_label or link, link, pal))
            out.append(text_html(ids, f"p{i}-url", link, {link: "url"}, pal,
                                 style=f"font-size:13px;color:{FAINT}"))
            rest = "\n".join(lines[lines.index(link) + 1:])
            if rest:
                out.append(text_html(ids, f"p{i}-rest", rest, {}, pal))
        else:
            out.append(text_html(ids, f"p{i}", para, {}, pal))
    preheader = paragraphs[1] if len(paragraphs) > 1 else subject
    logo = logo_attachment(brand)
    doc = frame(brand=brand, lang=lang, title=subject, preheader=preheader.split("\n")[0], inner="".join(out),
                ids=ids, pal=pal, footer_lines=footer_lines, logo=logo, legal_links=legal_links(brand, lang))
    return doc, [logo] if logo else []
