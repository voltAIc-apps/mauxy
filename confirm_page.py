"""
Two-step DOI confirmation for every mail link (security review 2026-11-09, finding 1: DOI defeated
by link prefetch). Mail security scanners (Microsoft Safe Links, Proofpoint, Mimecast, ...) GET
every link in a mail, some even run JS. So a GET on a confirm link must never burn a token or write
anything:

  GET  <confirm path>?token=   validates the token read-only and renders a minimal page with ONE
                               form (method POST, no JS, no auto-submit) and a "Bestätigen" /
                               "Confirm" button carrying the token. Invalid / expired / used:
                               the neutral page below (uniform for every reason).
  POST <confirm path>          (form field `token`) burns the token single-use, does the sink /
                               lead write, then 303 to the website's existing confirmed page.

Used by the newsletter (main.py /api/confirm), CTA leads (leads_api.py /api/lead/confirm) and the
Selbst-Check (selfcheck_api.py /api/selfcheck/confirm).

CSRF: the token itself is the capability (an attacker without it cannot confirm anything). On
top, SameSite-independent: the POST must carry the token the page rendered (same value), and
when the browser sends Origin (or, without Origin, Referer) its host must be mauxy's own
(PUBLIC_BASE_URL host or the request Host). Origin "null" is refused.

Pages: no external resources, every element has a unique id, strict CSP (default-src 'none',
inline style by hash, form-action 'self' + the redirect targets' origins, which Chrome also checks
on the redirect after the POST), Cache-Control no-store, noindex, no framing,
Referrer-Policy same-origin (no-referrer would make browsers send Origin: null on the POST).

Per site (brand.py, owner requirement 2026-11-09): the page carries the site's wordmark / display
name, optional logo (data: URI, PNG / WebP only; CSP img-src data: only then), colours (validated
#RRGGBB inside the hashed stylesheet), legal entity and imprint / privacy links (validated https,
escaped). Every brand value is html-escaped. Without a profile (brand=None) the page is
byte-identical to the one before 0006 (T-BR-030, tests/cases/brand_golden.json).
Neutral page: the brand comes from the token's unsigned `b` claim (site name; only a KNOWN site
is used): it depends on the token text only, never on its validity, so the page stays uniform for
every reason (bad signature, expired, used, unknown) -- no oracle.
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
from typing import Iterable, Literal, Optional, Union
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MAX_FORM_BYTES = 2048   # a token is < 200 chars; anything larger is not our form
MAX_TOKEN_LEN = 1000    # same limit as the GET query parameter

Flow = Literal["newsletter", "lead", "selfcheck"]

# One constant stylesheet; its hash goes into the CSP (no 'unsafe-inline').
STYLE = (
    "body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#f5f7fa;color:#1f2937}"
    "main{max-width:32rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;"
    "box-shadow:0 1px 4px rgba(0,0,0,.08)}"
    "h1{font-size:1.35rem;margin:0 0 1rem}p{line-height:1.5;margin:0 0 1rem}"
    "button{font:inherit;font-weight:600;padding:.7rem 1.6rem;border:0;border-radius:8px;"
    "background:#1d4ed8;color:#fff;cursor:pointer}button:focus{outline:3px solid #93c5fd}"
    ".note{font-size:.875rem;color:#6b7280;margin-top:1.5rem}.brand{font-size:.8rem;color:#9ca3af;margin:0 0 1.5rem}"
)
STYLE_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode() + "'"

TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "newsletter": {
        "de": ("Newsletter-Anmeldung bestätigen",
               "Bitte bestätigen Sie Ihre Anmeldung zum Newsletter mit einem Klick auf „Bestätigen“."),
        "en": ("Confirm your newsletter subscription",
               "Please confirm your newsletter subscription by clicking “Confirm”."),
    },
    "lead": {
        "de": ("Anfrage bestätigen",
               "Bitte bestätigen Sie mit einem Klick auf „Bestätigen“, dass die Anfrage von Ihnen stammt."),
        "en": ("Confirm your request",
               "Please confirm that the request came from you by clicking “Confirm”."),
    },
    "selfcheck": {
        "de": ("Selbst-Check bestätigen",
               "Bitte bestätigen Sie Ihren Selbst-Check mit einem Klick auf „Bestätigen“."),
        "en": ("Confirm your self-check",
               "Please confirm your self-check by clicking “Confirm”."),
    },
}
BUTTON = {"de": "Bestätigen", "en": "Confirm"}
NOTE = {
    "de": "Erst der Klick bestätigt. Wenn Sie das nicht angefordert haben, schließen Sie diese Seite einfach; "
          "ohne Bestätigung speichern wir nichts weiter.",
    "en": "Only the click confirms. If you did not request this, simply close this page; "
          "without confirmation we store nothing further.",
}
BRAND = "wapsol GmbH"  # generic brand line of sites WITHOUT a profile (company only)
LEGAL_LINKS = {"de": ("Impressum", "Datenschutz"), "en": ("Imprint", "Privacy")}


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""


def _style(brand) -> str:
    """Stylesheet of a page: the constant one without a profile, else with the site's colours
    (validated #RRGGBB only; the result is hashed into the CSP, never 'unsafe-inline')."""
    if brand is None:
        return STYLE
    primary = brand.color_primary or "#1d4ed8"
    accent = brand.color_accent or "#93c5fd"
    return (STYLE.replace("background:#1d4ed8", f"background:{primary}")
            .replace("outline:3px solid #93c5fd", f"outline:3px solid {accent}")
            + f".brand{{color:{primary};font-weight:600;font-size:.95rem}}"
            ".logo{display:block;max-height:40px;max-width:200px;margin:0 0 .5rem}"
            ".legal{font-size:.75rem;color:#6b7280;margin:1.5rem 0 0}.legal a{color:inherit}")


def _hash(style: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'"


def _headers(form_action: str, style: str = STYLE, img_data: bool = False) -> dict:
    style_hash = STYLE_HASH if style == STYLE else _hash(style)
    img = "; img-src data:" if img_data else ""
    csp = (f"default-src 'none'; style-src {style_hash}{img}; form-action {form_action}; "
           "base-uri 'none'; frame-ancestors 'none'")
    return {
        "Content-Security-Policy": csp,
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _brand_head(brand) -> str:
    """Brand line: generic company name without a profile, else logo + wordmark (escaped)."""
    if brand is None:
        return f'<p id="mx-brand" class="brand">{html.escape(BRAND)}</p>'
    logo = brand.logo_data_uri()
    img = (f'<img id="mx-logo" class="logo" src="{html.escape(logo)}" alt="{html.escape(brand.display_name)}">'
           if logo else "")
    return f'<p id="mx-brand" class="brand">{img}{html.escape(brand.wordmark or brand.display_name)}</p>'


def _brand_foot(brand, lang: str) -> str:
    """Legal line (legal entity, imprint, privacy) of a profile; '' without one."""
    if brand is None:
        return ""
    parts = [f'<span id="mx-legal-entity">{html.escape(brand.legal_entity)}</span>'] if brand.legal_entity else []
    imprint, privacy = LEGAL_LINKS.get(lang, LEGAL_LINKS["de"])
    for key, url, text in (("imprint", brand.imprint_url, imprint), ("privacy", brand.privacy_url, privacy)):
        if url:
            href = html.escape(url.replace("{lang}", lang))
            parts.append(f'<a id="mx-{key}" href="{href}" rel="noopener noreferrer">{html.escape(text)}</a>')
    return f'<p id="mx-legal" class="legal">{" · ".join(parts)}</p>' if parts else ""


def _page(lang: str, title: str, inner: str, brand=None, style: str = STYLE) -> str:
    return (
        f'<!doctype html><html id="mx-html" lang="{lang}"><head id="mx-head">'
        '<meta id="mx-charset" charset="utf-8">'
        '<meta id="mx-viewport" name="viewport" content="width=device-width, initial-scale=1">'
        '<meta id="mx-robots" name="robots" content="noindex, nofollow">'
        f'<title id="mx-title">{html.escape(title)}</title>'
        f'<style id="mx-style">{style}</style></head>'
        f'<body id="mx-body"><main id="mx-main">'
        f'{_brand_head(brand)}{inner}{_brand_foot(brand, lang)}</main></body></html>'
    )


def render(flow: Flow, lang: str, action: str, token: str, redirect_base: Union[str, Iterable[str]],
           brand=None) -> HTMLResponse:
    """Confirmation page for a VALID token (read-only; nothing is written). `action` = the POST
    path (same path as the GET), `redirect_base` = the website URL(s) the POST may redirect to
    (confirmed + error target; CSP form-action), `brand` = the site's profile (brand.Brand) or None."""
    if lang not in ("de", "en"):
        lang = brand.default_locale if brand is not None else "de"
    title, text = TEXTS[flow][lang]
    inner = (
        f'<h1 id="mx-heading">{html.escape(title)}</h1>'
        f'<p id="mx-text">{html.escape(text)}</p>'
        f'<form id="mx-form" method="post" action="{html.escape(action)}">'
        f'<input id="mx-token" type="hidden" name="token" value="{html.escape(token)}">'
        f'<button id="mx-confirm" type="submit">{html.escape(BUTTON[lang])}</button>'
        '</form>'
        f'<p id="mx-note" class="note">{html.escape(NOTE[lang])}</p>'
    )
    bases = [redirect_base] if isinstance(redirect_base, str) else list(redirect_base)
    targets = " ".join(sorted(o for o in {_origin_of(b) for b in bases} if o))
    style = _style(brand)
    img = brand is not None and brand.logo_data_uri() is not None
    return HTMLResponse(_page(lang, title, inner, brand, style), status_code=200,
                        headers=_headers(f"'self' {targets}".strip(), style, img))


# Identical bytes for every invalid case (bad signature, expired, unknown, used, replay, foreign
# salt): no hint which one it was, no locale from the token.
NEUTRAL_TITLE = "Link ungültig / Link invalid"
NEUTRAL_INNER = (
    '<h1 id="mx-heading">Link ungültig oder abgelaufen</h1>'
    '<p id="mx-text">Dieser Bestätigungslink ist ungültig, abgelaufen oder wurde bereits verwendet. '
    'Es wurde nichts geändert.</p>'
    '<h2 id="mx-heading-en" lang="en">Link invalid or expired</h2>'
    '<p id="mx-text-en" lang="en">This confirmation link is invalid, expired or has already been used. '
    'Nothing was changed.</p>'
)
NEUTRAL_HTML = _page("de", NEUTRAL_TITLE, NEUTRAL_INNER)


def neutral(status_code: int = 200, brand=None) -> HTMLResponse:
    """The uniform page for every invalid case; `brand` from token_site() (module doc)."""
    if brand is None:
        return HTMLResponse(NEUTRAL_HTML, status_code=status_code, headers=_headers("'none'"))
    style = _style(brand)
    return HTMLResponse(_page("de", NEUTRAL_TITLE, NEUTRAL_INNER, brand, style), status_code=status_code,
                        headers=_headers("'none'", style, brand.logo_data_uri() is not None))


def token_site(signer, token: Optional[str]) -> Optional[str]:
    """The site claim `b` of a DOI token WITHOUT checking signature or age: used only to pick the
    brand of the neutral page (cosmetic, same for every validity state of the same token text).
    Compressed payloads are skipped (no decompression of untrusted input)."""
    if signer is None or not token or len(token) > MAX_TOKEN_LEN or token.startswith("."):
        return None
    try:
        _ok, payload = signer.loads_unsafe(token)
    except Exception:
        return None
    site = payload.get("b") if isinstance(payload, dict) else None
    return site if isinstance(site, str) and 0 < len(site) <= 80 else None


def same_origin(request: Request) -> bool:
    """CSRF check of the confirm POST: Origin (else Referer), when present, must be mauxy itself."""
    own = {request.headers.get("host", "").lower()}
    if PUBLIC_BASE_URL:
        own.add(urlsplit(PUBLIC_BASE_URL).netloc.lower())
    own.discard("")
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin.strip()).netloc.lower() in own  # "null" -> "" -> refused
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer.strip()).netloc.lower() in own
    return True  # neither header (privacy tools strip them): the token is the capability


async def form_token(request: Request) -> Optional[str]:
    """The `token` field of the confirm form (application/x-www-form-urlencoded, size-capped;
    no python-multipart needed). None = not our form."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    try:
        fields = parse_qs(body.decode("ascii"), max_num_fields=4, strict_parsing=False)
    except (UnicodeDecodeError, ValueError):
        return None
    values = fields.get("token", [])
    if len(values) != 1 or not 0 < len(values[0]) <= MAX_TOKEN_LEN:
        return None
    return values[0]
