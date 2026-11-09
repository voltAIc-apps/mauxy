"""
Per-site brand profile (owner requirement 2026-11-09: "the confirm page should be per brand").

One profile per site (= sites.site; db-patches/0006): display name, legal entity, colours, wordmark,
optional small raster logo, website / imprint / privacy URLs, redirect targets per flow + locale +
outcome, default locale, mail sender + reply-to, mail footer per locale, and the website origins
that identify the site on the endpoints without a site key (CTA leads, Selbst-Check).

Where a person sees the brand: the DOI confirmation page and the neutral page (confirm_page.py),
the DOI mails (newsletter, lead, Selbst-Check), the Selbst-Check result mail, the redirect after the
click. A site WITHOUT a profile looks and behaves exactly as before 0006 (generic "wapsol GmbH"
page, SMTP_FROM, env redirect URLs) -- that is the LUDO regression contract (T-BR-03x).

Security:
  - every value is validated on write (admin CRUD below) and escaped on render (confirm_page.py)
  - colours: exactly #RRGGBB (they go into the hashed inline stylesheet, never free CSS)
  - URLs: https only (http only for the dev loopback alias 10.0.99.1), no userinfo, no quotes /
    angle brackets / whitespace; {lang} is the only placeholder
  - logo: PNG or WebP only (magic bytes), <= BRAND_LOGO_MAX_BYTES (64 KiB), stored in SQLite and
    served inline as a data: URI (CSP img-src data: only on pages that carry one). No SVG (script
    vector), no remote URL (tracking / mixed content).
  - mail sender address: domain must be in MAIL_SENDER_DOMAINS (mailer.sender_allowed)

Redirect target of a DOI click (resolve order):
  1. site_redirect row (flow, locale, outcome)          explicit URL (+ the flow's query parameter)
  2. site_brand.website_url + the flow's standard path  e.g. /de/newsletter/confirmed
  3. env base (PUBLIC_WEBSITE_BASE_URL, LEAD_WEBSITE_URL, SELFCHECK_WEBSITE_URL) + standard path
     = the behaviour before 0006 (sites without a profile)
Each environment (prod / stag / dev) has its own SQLite DB: the seed holds the prod values of
simplify-erp; stag / dev set their website_url via PUT /api/admin/brands/simplify-erp.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional
from urllib.parse import urlencode, urlsplit

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import mailer

logger = logging.getLogger("mauxy.brand")

# Site of CTA leads / Selbst-Checks whose Origin is not in site_origin (today: the website).
DEFAULT_SITE = os.environ.get("DEFAULT_SITE", "simplify-erp").strip() or "simplify-erp"
LOGO_MAX_BYTES = int(os.environ.get("BRAND_LOGO_MAX_BYTES", "65536"))
DEV_LOOPBACK = "10.0.99.1"  # the one host allowed over http (local dev, owner convention)

FLOWS = ("newsletter", "lead", "selfcheck")
LOCALES = ("de", "en")
OUTCOMES = ("confirmed", "error")
LOGO_TYPES = ("image/png", "image/webp")

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_LINE = r"^[^\x00-\x1f\x7f<>]+$"                 # one line, no controls, no angle brackets
_URL_BAD = re.compile(r"[\x00-\x20\x7f\"'<>\\`{}|^]")  # after {lang} substitution


@dataclass(frozen=True)
class Brand:
    site: str
    display_name: str
    legal_entity: Optional[str] = None
    color_primary: Optional[str] = None
    color_accent: Optional[str] = None
    wordmark: Optional[str] = None
    logo: Optional[bytes] = None
    logo_mime: Optional[str] = None
    website_url: Optional[str] = None
    imprint_url: Optional[str] = None
    privacy_url: Optional[str] = None
    default_locale: str = "de"
    mail_from_name: Optional[str] = None
    mail_from_address: Optional[str] = None
    mail_reply_to: Optional[str] = None
    footers: dict = field(default_factory=dict)     # locale -> footer text
    redirects: dict = field(default_factory=dict)   # (flow, locale, outcome) -> url
    origins: tuple = ()

    def logo_data_uri(self) -> Optional[str]:
        if not self.logo or self.logo_mime not in LOGO_TYPES:
            return None
        return f"data:{self.logo_mime};base64,{base64.b64encode(self.logo).decode('ascii')}"


_BRANDS: dict[str, Brand] = {}
_ORIGINS: dict[str, str] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------- cache


async def load(db: aiosqlite.Connection) -> None:
    """Reload every profile (startup, after each admin write; main.reload_config)."""
    global _BRANDS, _ORIGINS
    footers: dict[str, dict] = {}
    async with db.execute("SELECT site, locale, footer FROM site_mail_footer") as cur:
        for site, locale, footer in await cur.fetchall():
            footers.setdefault(site, {})[locale] = footer
    redirects: dict[str, dict] = {}
    async with db.execute("SELECT site, flow, locale, outcome, url FROM site_redirect") as cur:
        for site, flow, locale, outcome, url in await cur.fetchall():
            redirects.setdefault(site, {})[(flow, locale, outcome)] = url
    origins: dict[str, str] = {}
    by_site: dict[str, list] = {}
    async with db.execute("SELECT origin, site FROM site_origin ORDER BY origin") as cur:
        for origin, site in await cur.fetchall():
            origins[origin] = site
            by_site.setdefault(site, []).append(origin)
    brands: dict[str, Brand] = {}
    async with db.execute(
            "SELECT site, display_name, legal_entity, color_primary, color_accent, wordmark, logo, logo_mime, "
            "website_url, imprint_url, privacy_url, default_locale, mail_from_name, mail_from_address, "
            "mail_reply_to FROM site_brand") as cur:
        for r in await cur.fetchall():
            site = r[0]
            brands[site] = Brand(
                site=site, display_name=r[1], legal_entity=r[2], color_primary=r[3], color_accent=r[4],
                wordmark=r[5], logo=bytes(r[6]) if r[6] is not None else None, logo_mime=r[7],
                website_url=r[8], imprint_url=r[9], privacy_url=r[10], default_locale=r[11] or "de",
                mail_from_name=r[12], mail_from_address=r[13], mail_reply_to=r[14],
                footers=footers.get(site, {}), redirects=redirects.get(site, {}),
                origins=tuple(by_site.get(site, ())))
    _BRANDS, _ORIGINS = brands, origins


def get(site: Optional[str]) -> Optional[Brand]:
    return _BRANDS.get(site) if site else None


def count() -> int:
    return len(_BRANDS)


def site_for_origin(origin: Optional[str]) -> str:
    """Site of a request without site key (CTA lead, Selbst-Check) from its Origin header."""
    norm = (origin or "").strip().rstrip("/").lower()
    return _ORIGINS.get(norm, DEFAULT_SITE)


def lead_site(row_site: Optional[str]) -> str:
    """Stored site of a lead / self-check row ('' = before 0006 or unmapped) -> effective site."""
    return row_site or DEFAULT_SITE


# ------------------------------------------------------------------------------- render helpers


def localized(url: Optional[str], lang: str) -> Optional[str]:
    return url.replace("{lang}", lang) if url else url


def with_query(url: str, params: Optional[dict]) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params)


def base_url(site: Optional[str], env_base: str) -> str:
    """Website base of a site: its profile's website_url, else the env URL (before 0006)."""
    b = get(site)
    return ((b.website_url if b and b.website_url else env_base) or "").rstrip("/")


def redirect_url(site: Optional[str], flow: str, lang: str, outcome: str, *, env_base: str, path: str,
                 params: Optional[dict] = None) -> str:
    """Where the DOI click ends (module doc: explicit row > website_url + path > env + path).
    `path` = the flow's standard path incl. the locale (e.g. '/de/newsletter/confirmed')."""
    b = get(site)
    explicit = b.redirects.get((flow, lang, outcome)) if b else None
    if explicit:
        return with_query(localized(explicit, lang), params)
    return with_query(base_url(site, env_base) + path, params)


def origin_of(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""


def label(site: Optional[str], env_base: str) -> str:
    """Short website name for mail texts ('simplify-erp.de'): host of the site's website."""
    host = urlsplit(base_url(site, env_base)).hostname or ""
    return host[4:] if host.startswith("www.") else host


def sender(site: Optional[str]) -> Optional[mailer.Sender]:
    """Per-site sender of person-facing mails; None = SMTP_FROM (unbranded, as before)."""
    b = get(site)
    if not b or not (b.mail_from_name or b.mail_from_address or b.mail_reply_to):
        return None
    return mailer.Sender(name=b.mail_from_name, address=b.mail_from_address, reply_to=b.mail_reply_to)


def footer(site: Optional[str], lang: str) -> Optional[str]:
    b = get(site)
    return (b.footers.get(lang) or b.footers.get(b.default_locale)) if b else None


def with_footer(body: str, site: Optional[str], lang: str) -> str:
    """Append the site's mail footer (unbranded: body unchanged)."""
    f = footer(site, lang)
    return f"{body.rstrip()}\n\n{f}\n" if f else body


def replace_signature(body: str, site: Optional[str], lang: str, signature: str) -> str:
    """Texts that already end with a signature (vendored Selbst-Check rules): swap it for the site's
    footer. Unbranded or no footer -> unchanged; simplify-erp's seeded footer == that signature."""
    f = footer(site, lang)
    if not f or not body.rstrip().endswith(signature):
        return body
    return body.rstrip()[: -len(signature)] + f


# ------------------------------------------------------------------------------- validation


class BrandInvalid(ValueError):
    """Admin input refused (400 with the message; never contains stored secrets)."""


def check_url(value: str, *, allow_lang: bool = False, what: str = "url") -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 500:
        raise BrandInvalid(f"{what}: 1..500 characters")
    probe = value.replace("{lang}", "de") if allow_lang else value
    if _URL_BAD.search(probe):
        raise BrandInvalid(f"{what}: forbidden character" + ("" if allow_lang else " ({lang} not allowed here)"))
    try:
        parts = urlsplit(probe)
        _ = parts.port  # ValueError for a malformed port
    except ValueError:
        raise BrandInvalid(f"{what}: not a URL") from None
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BrandInvalid(f"{what}: host required, no user info")
    if parts.scheme == "https" or (parts.scheme == "http" and host == DEV_LOOPBACK):
        return value
    raise BrandInvalid(f"{what}: https only (http only on {DEV_LOOPBACK})")


def check_origin(value: str) -> str:
    norm = value.strip().rstrip("/").lower()
    check_url(norm, what="origin")
    parts = urlsplit(norm)
    if parts.path or parts.query or parts.fragment:
        raise BrandInvalid("origin: scheme://host[:port] only")
    return norm


def check_logo(mime: str, data_b64: str) -> bytes:
    if mime not in LOGO_TYPES:
        raise BrandInvalid("logo: image/png or image/webp only (no SVG)")
    if len(data_b64) > (LOGO_MAX_BYTES * 4) // 3 + 8:
        raise BrandInvalid(f"logo: larger than {LOGO_MAX_BYTES} bytes")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise BrandInvalid("logo: invalid base64") from None
    if not raw or len(raw) > LOGO_MAX_BYTES:
        raise BrandInvalid(f"logo: 1..{LOGO_MAX_BYTES} bytes")
    if not mailer.MAGIC[mime](raw):
        raise BrandInvalid("logo: content does not match its type")
    if mime == "image/png" and raw[12:16] != b"IHDR":
        raise BrandInvalid("logo: not a PNG image")
    return raw


def check_sender(addr: str, what: str) -> str:
    try:
        norm = mailer.normalize_address(addr)
    except mailer.MailRejected:
        raise BrandInvalid(f"{what}: invalid address") from None
    if what == "mail_from_address" and not mailer.sender_allowed(norm):
        raise BrandInvalid(f"{what}: domain not in MAIL_SENDER_DOMAINS")
    return norm


def check_footer(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if not 0 < len(text) <= 1000 or re.search(r"[\x00-\x09\x0b-\x1f\x7f]", text):
        raise BrandInvalid("mail_footer: 1..1000 characters, no control characters except line breaks")
    return text


# ------------------------------------------------------------------------------- admin models

Line = lambda n: Field(default=None, min_length=1, max_length=n, pattern=_LINE)  # noqa: E731


class LogoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mime: Literal["image/png", "image/webp"]
    data: str = Field(min_length=4, max_length=LOGO_MAX_BYTES * 2)


class RedirectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow: Literal["newsletter", "lead", "selfcheck"]
    locale: Literal["de", "en"]
    outcome: Literal["confirmed", "error"]
    url: str = Field(min_length=1, max_length=500)


class BrandIn(BaseModel):
    """PUT /api/admin/brands/{site}: only the fields sent change; null clears an optional field.
    mail_footer / redirects / origins / logo: the given value REPLACES the stored set."""
    model_config = ConfigDict(extra="forbid")
    display_name: Optional[str] = Line(80)
    legal_entity: Optional[str] = Line(120)
    color_primary: Optional[str] = Field(default=None, pattern=HEX.pattern)
    color_accent: Optional[str] = Field(default=None, pattern=HEX.pattern)
    wordmark: Optional[str] = Line(60)
    website_url: Optional[str] = Field(default=None, max_length=500)
    imprint_url: Optional[str] = Field(default=None, max_length=500)
    privacy_url: Optional[str] = Field(default=None, max_length=500)
    default_locale: Optional[Literal["de", "en"]] = None
    mail_from_name: Optional[str] = Line(80)
    mail_from_address: Optional[str] = Field(default=None, max_length=254)
    mail_reply_to: Optional[str] = Field(default=None, max_length=254)
    mail_footer: Optional[dict[Literal["de", "en"], str]] = None
    redirects: Optional[list[RedirectIn]] = Field(default=None, max_length=12)
    origins: Optional[list[str]] = Field(default=None, max_length=10)
    logo: Optional[LogoIn] = None


COLUMNS = ("display_name", "legal_entity", "color_primary", "color_accent", "wordmark", "website_url",
           "imprint_url", "privacy_url", "default_locale", "mail_from_name", "mail_from_address", "mail_reply_to")
REQUIRED = ("display_name", "default_locale")


def normalize(payload: BrandIn) -> dict:
    """Validated column values + sub-sets of the fields that were SENT (model_fields_set)."""
    sent = payload.model_fields_set
    out: dict = {}
    for col in COLUMNS:
        if col not in sent:
            continue
        val = getattr(payload, col)
        if val is None and col in REQUIRED:
            raise BrandInvalid(f"{col} cannot be cleared")
        if val is not None:
            if col == "website_url":
                val = check_url(val, what=col).rstrip("/")
            elif col in ("imprint_url", "privacy_url"):
                val = check_url(val, allow_lang=True, what=col)
            elif col in ("mail_from_address", "mail_reply_to"):
                val = check_sender(val, col)
        out[col] = val
    if "mail_footer" in sent:
        out["_footers"] = {k: check_footer(v) for k, v in (payload.mail_footer or {}).items()}
    if "redirects" in sent:
        seen = {}
        for r in payload.redirects or []:
            key = (r.flow, r.locale, r.outcome)
            if key in seen:
                raise BrandInvalid(f"redirects: duplicate {key}")
            seen[key] = check_url(r.url, allow_lang=True, what=f"redirects.{r.flow}.{r.locale}.{r.outcome}")
        out["_redirects"] = seen
    if "origins" in sent:
        out["_origins"] = sorted({check_origin(o) for o in payload.origins or []})
    if "logo" in sent:
        out["_logo"] = (payload.logo.mime, check_logo(payload.logo.mime, payload.logo.data)) if payload.logo else None
    return out


def public_view(b: Brand) -> dict:
    """Admin view: everything except the logo bytes (mime, size, sha256 only)."""
    return {
        "site": b.site, **{c: getattr(b, c) for c in COLUMNS},
        "logo": {"mime": b.logo_mime, "bytes": len(b.logo), "sha256": hashlib.sha256(b.logo).hexdigest()} if b.logo else None,
        "mail_footer": dict(b.footers),
        "redirects": [{"flow": f, "locale": lc, "outcome": o, "url": u} for (f, lc, o), u in sorted(b.redirects.items())],
        "origins": list(b.origins),
    }


async def save(db: aiosqlite.Connection, site: str, vals: dict) -> bool:
    """Upsert one profile + its sub-sets in ONE transaction. True = created. Raises BrandInvalid."""
    async with db.execute("SELECT 1 FROM site_brand WHERE site = ?", (site,)) as cur:
        exists = await cur.fetchone() is not None
    cols = {k: v for k, v in vals.items() if not k.startswith("_")}
    if not exists and not cols.get("display_name"):
        raise BrandInvalid("display_name is required for a new profile")
    if "_origins" in vals:
        for origin in vals["_origins"]:
            async with db.execute("SELECT site FROM site_origin WHERE origin = ?", (origin,)) as cur:
                row = await cur.fetchone()
            if row and row[0] != site:
                raise BrandInvalid(f"origin {origin} belongs to another site")
    try:
        if exists:
            if cols:
                sets = ", ".join(f"{c} = ?" for c in cols)  # fixed column names (COLUMNS), not input
                await db.execute(f"UPDATE site_brand SET {sets}, updated_at = ? WHERE site = ?",
                                 (*cols.values(), now_iso(), site))
        else:
            names = ", ".join(cols)
            await db.execute(f"INSERT INTO site_brand ({names}, site, updated_at) VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                             (*cols.values(), site, now_iso()))
        if "_logo" in vals:
            mime, raw = vals["_logo"] or (None, None)
            await db.execute("UPDATE site_brand SET logo = ?, logo_mime = ?, updated_at = ? WHERE site = ?",
                             (raw, mime, now_iso(), site))
        if "_footers" in vals:
            await db.execute("DELETE FROM site_mail_footer WHERE site = ?", (site,))
            for locale, text in vals["_footers"].items():
                await db.execute("INSERT INTO site_mail_footer (site, locale, footer) VALUES (?, ?, ?)", (site, locale, text))
        if "_redirects" in vals:
            await db.execute("DELETE FROM site_redirect WHERE site = ?", (site,))
            for (flow, locale, outcome), url in vals["_redirects"].items():
                await db.execute("INSERT INTO site_redirect (site, flow, locale, outcome, url) VALUES (?, ?, ?, ?, ?)",
                                 (site, flow, locale, outcome, url))
        if "_origins" in vals:
            await db.execute("DELETE FROM site_origin WHERE site = ?", (site,))
            for origin in vals["_origins"]:
                await db.execute("INSERT INTO site_origin (origin, site) VALUES (?, ?)", (origin, site))
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        raise BrandInvalid(f"refused by the schema ({exc.__class__.__name__})") from None
    except Exception:
        await db.rollback()
        raise
    return not exists


async def delete(db: aiosqlite.Connection, site: str) -> bool:
    for table in ("site_mail_footer", "site_redirect", "site_origin"):  # fixed names
        await db.execute(f"DELETE FROM {table} WHERE site = ?", (site,))
    cur = await db.execute("DELETE FROM site_brand WHERE site = ?", (site,))
    await db.commit()
    return cur.rowcount == 1


# ------------------------------------------------------------------------------- admin router


def build_router(*, require_admin: Callable[[Request], Optional[JSONResponse]],
                 on_change: Callable[[aiosqlite.Connection], object]) -> APIRouter:
    """/api/admin/brands (Bearer ADMIN_API_KEY). Every write reloads the caches (on_change)."""
    router = APIRouter()

    def bad_site(site: str) -> Optional[JSONResponse]:
        return None if SITE_RE.match(site) else JSONResponse({"error": "invalid site name"}, status_code=422)

    @router.get("/api/admin/brands")
    async def admin_list_brands(request: Request):
        if (err := require_admin(request)) is not None:
            return err
        return JSONResponse({"brands": [public_view(b) for _, b in sorted(_BRANDS.items())], "count": len(_BRANDS),
                             "default_site": DEFAULT_SITE})

    @router.get("/api/admin/brands/{site}")
    async def admin_get_brand(site: str, request: Request):
        if (err := require_admin(request) or bad_site(site)) is not None:
            return err
        b = get(site)
        return JSONResponse(public_view(b)) if b else JSONResponse({"error": "brand not found"}, status_code=404)

    @router.put("/api/admin/brands/{site}")
    async def admin_put_brand(site: str, request: Request):
        if (err := require_admin(request) or bad_site(site)) is not None:
            return err
        try:
            payload = BrandIn.model_validate(await request.json())
            vals = normalize(payload)
            db: aiosqlite.Connection = request.app.state.db
            created = await save(db, site, vals)
        except ValidationError as exc:
            return JSONResponse({"error": "invalid", "detail": exc.errors(include_url=False, include_input=False)},
                                status_code=422)
        except BrandInvalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ValueError:  # body is not JSON
            return JSONResponse({"error": "invalid JSON"}, status_code=422)
        await on_change(db)
        logger.info("ADMIN_BRAND_%s site=%s fields=%s", "CREATED" if created else "UPDATED", site,
                    ",".join(sorted(k.lstrip("_") for k in vals)))
        return JSONResponse({"status": "created" if created else "updated", "brand": public_view(get(site))},
                            status_code=201 if created else 200)

    @router.delete("/api/admin/brands/{site}")
    async def admin_delete_brand(site: str, request: Request):
        if (err := require_admin(request) or bad_site(site)) is not None:
            return err
        db: aiosqlite.Connection = request.app.state.db
        if not await delete(db, site):
            return JSONResponse({"error": "brand not found"}, status_code=404)
        await on_change(db)
        logger.info("ADMIN_BRAND_DELETED site=%s", site)
        return JSONResponse({"status": "deleted"})

    @router.get("/api/admin/brands/{site}/preview")
    async def admin_preview_brand(site: str, request: Request,
                                  flow: Literal["newsletter", "lead", "selfcheck", "neutral"] = Query("newsletter"),
                                  lang: Literal["de", "en"] = Query("de")):
        """The confirmation (or neutral) page as the person would see it, with a dummy token."""
        if (err := require_admin(request) or bad_site(site)) is not None:
            return err
        import confirm_page  # local: confirm_page imports this module
        b = get(site)
        if flow == "neutral":
            return confirm_page.neutral(brand=b)
        action = {"newsletter": "/api/confirm", "lead": "/api/lead/confirm", "selfcheck": "/api/selfcheck/confirm"}[flow]
        return confirm_page.render(flow, lang, action, "preview-token", [], brand=b)  # a click -> neutral page

    return router
