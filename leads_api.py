"""
Generic website CTA endpoint (flywheel SPEC-021): one double opt-in flow for every simplify-erp.de
call to action that ends as a crm.lead in erp.wapsol.

  POST /api/lead                 {kind: contact | inquiry | webinar | checkout | selfcheck, ...}
                                 Per-kind schema (extra fields refused, lengths limited), honeypot
                                 company_website, Origin allowlist. kind selfcheck = alias of
                                 POST /api/selfcheck (selfcheck_api.py). Immediate uniform
                                 200 {"status": "pending_confirmation"}; storage + ONE DOI mail run
                                 as a job after the response. Nothing goes to any sink here.
  GET  /api/lead/confirm?token=  DOI mail link, READ-ONLY: confirmation page with a "Bestätigen"
                                 button (confirm_page.py; mail scanners prefetch links). Invalid /
                                 expired / used: uniform neutral page.
  POST /api/lead/confirm         form field token: single use (SHA-256, constant time, atomic
                                 claim), own salt. Burns the token, queues the delivery
                                 (lead_sync): crm.lead + newsletter if ticked.
                                 303 to {LEAD_WEBSITE_URL}/{lang}/anfrage/bestaetigt?art=<kind>
                                 (misconfigured: /{lang}/anfrage/fehler).
  GET  /api/lead/submissions     admin (Bearer ADMIN_API_KEY): submissions + CRM state.
  POST /api/lead/sync/{id}/retry admin: re-queue a failed CRM delivery.

DOI mails contain server-side text only (no browser-supplied text goes to an arbitrary address).
The lead (title + plain-text description) is rendered at confirmation from the submitted fields.
Retention: pending LEAD_PENDING_DAYS (7), confirmed LEAD_RETENTION_DAYS (30): the CRM is the
system of record once delivered.

Per site (db-patches/0006): the site comes from the Origin (brand.site_for_origin, site_origin
table; unmapped -> DEFAULT_SITE) and is stored with the submission. It picks the brand (DOI mail
sender / website name / footer, confirmation page, redirect) and the routing (routing.py: the
site's odoo target with its team / tags / UTM / lists rides in lead_sync, every other target --
e.g. a flywheel webhook -- gets its own outbox row; a ticked newsletter box follows the site's
subscribe routing). No routes = the fallback (LEAD_SINK / SUBSCRIBER_SINK) exactly as before.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, Literal, Optional, Union
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

import brand
import confirm_page
import jobs
import lead_sync
import mailer
import routing
import selfcheck_api

logger = logging.getLogger("mauxy.leads")

_raw = os.environ.get("LEAD_ALLOWED_ORIGINS", "") or os.environ.get("SELFCHECK_ALLOWED_ORIGINS", "")
LEAD_ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _raw.split(",") if o.strip()]
LEAD_RATE_LIMIT = os.environ.get("LEAD_RATE_LIMIT", "5/minute")
LEAD_PENDING_DAYS = int(os.environ.get("LEAD_PENDING_DAYS", "7"))
LEAD_RETENTION_DAYS = int(os.environ.get("LEAD_RETENTION_DAYS", "30"))
CONFIRM_TOKEN_SECRET = os.environ.get("CONFIRM_TOKEN_SECRET", "")
CONFIRM_TOKEN_MAX_AGE = int(os.environ.get("CONFIRM_TOKEN_MAX_AGE", "86400"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LEAD_WEBSITE_URL = (os.environ.get("LEAD_WEBSITE_URL", "") or os.environ.get("PUBLIC_WEBSITE_BASE_URL", "")
                    or selfcheck_api.RULES["website"]).rstrip("/")
MAX_PENDING_PER_EMAIL = int(os.environ.get("MAX_PENDING_PER_EMAIL", "3"))

if not LEAD_ALLOWED_ORIGINS:
    logger.warning("LEAD_ALLOWED_ORIGINS is not set -- /api/lead rejects every browser origin")

# Own salt: newsletter / self-check tokens can never confirm a lead (and vice versa).
_signer = URLSafeTimedSerializer(CONFIRM_TOKEN_SECRET, salt="mauxy-lead-confirm") if CONFIRM_TOKEN_SECRET else None
PENDING = {"status": "pending_confirmation"}
KINDS = ("contact", "inquiry", "webinar", "checkout")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- schemas

NO_CTRL = r"^[^\x00-\x1f\x7f]+$"            # single line: no control characters
MULTI = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$"  # multi line: \t \n \r allowed
Line = lambda n: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=n, pattern=NO_CTRL)]  # noqa: E731
Text = lambda n: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=n, pattern=MULTI)]  # noqa: E731
Slug = Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=40, pattern=r"^[0-9+()/. -]+$")]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    lang: Literal["de", "en"]
    newsletter: bool = False                   # ticked newsletter box = consent, confirmed by the same DOI
    page_url: Optional[str] = Field(default=None, max_length=500)
    company_website: Optional[str] = Field(default=None, max_length=200)  # honeypot

    @field_validator("page_url")
    @classmethod
    def _on_site(cls, v: Optional[str]) -> Optional[str]:
        if v and not any(v == o or v.startswith(o + "/") or v.startswith(o + "?") for o in LEAD_ALLOWED_ORIGINS):
            raise ValueError("page_url must be on an allowed site origin")
        return v


class _Named(_Base):
    # optional on the CTA kinds (no greeting mail there): crm.lead.title + "Salutation:" line when sent.
    # Optional: stated by the form, else guessed from the first name (owner 2026-11-10).
    salutation: Optional[selfcheck_api.Salutation] = None


class ContactLead(_Named):
    kind: Literal["contact"]
    name: Line(120)
    company: Line(160)
    phone: Optional[Phone] = None
    message: Text(4000)


class InquiryLead(_Named):
    kind: Literal["inquiry"]
    name: Optional[Line(120)] = None
    company: Optional[Line(160)] = None
    phone: Optional[Phone] = None
    subject_type: Literal["app", "page", "webinar", "card", "offer"] = "app"
    subject_id: Slug
    subject_title: Line(200)
    message: Optional[Text(2000)] = None


class WebinarLead(_Named):
    kind: Literal["webinar"]
    name: Line(120)
    company: Line(160)
    webinar_id: Slug
    webinar_title: Line(200)
    date: datetime

    @field_validator("date")
    @classmethod
    def _plausible(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("date needs a time zone")
        if not (_now() - timedelta(days=1) <= v <= _now() + timedelta(days=730)):
            raise ValueError("date out of range")
        return v


class Billing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    street: Line(160)
    postal_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=12, pattern=r"^[A-Za-z0-9 -]+$")]
    city: Line(120)
    country: Annotated[str, StringConstraints(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")]


class CheckoutLead(_Named):
    kind: Literal["checkout"]
    name: Line(120)
    company: Line(160)
    phone: Optional[Phone] = None
    product_id: Slug
    product_title: Line(200)
    bundle: Optional[Line(120)] = None
    technology: Optional[Line(80)] = None
    services: list[Line(120)] = Field(default_factory=list, max_length=20)
    price_net: float = Field(ge=0, le=10_000_000)
    currency: Literal["EUR"] = "EUR"
    order_ref: Annotated[str, StringConstraints(pattern=r"^ORD-[A-Z0-9]{4,20}$")]
    billing: Billing
    vat_id: Optional[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z]{2}[A-Z0-9]{2,13}$")]] = None
    terms: Literal[True]


class SelfCheckLead(selfcheck_api.SelfCheckRequest):
    kind: Literal["selfcheck"]


LeadRequest = Annotated[Union[ContactLead, InquiryLead, WebinarLead, CheckoutLead, SelfCheckLead], Field(discriminator="kind")]


# --------------------------------------------------------------------------- storage


# schema: db-patches/0003_website_consent.sql (applied by scripts/sqlite_db.py --migrate)
async def purge(db: aiosqlite.Connection) -> None:
    now = _now()
    await db.execute("DELETE FROM lead_submission WHERE status = 'pending' AND created_at < ?",
                     (_iso(now - timedelta(days=LEAD_PENDING_DAYS)),))
    await db.execute("DELETE FROM lead_submission WHERE status = 'confirmed' AND confirmed_at < ?",
                     (_iso(now - timedelta(days=LEAD_RETENTION_DAYS)),))
    await db.commit()


# --------------------------------------------------------------------------- texts

KIND_LABEL = {
    "contact": {"de": "eine Kontaktanfrage", "en": "a contact request"},
    "inquiry": {"de": "eine Anfrage zu einer App bzw. einem Modul", "en": "an inquiry about an app or module"},
    "webinar": {"de": "eine Webinar-Anmeldung", "en": "a webinar registration"},
    "checkout": {"de": "eine Bestellung im Shop", "en": "an order in the shop"},
}
# {site} = the site's website name (brand.label), {footer} = its mail footer (simplify-erp seed:
# "Ashant Chalasani / Simplify-ERP, wapsol GmbH / imprint", i.e. the text of before 0006)
DOI = {
    "de": ("Bitte bestätigen: Ihre Anfrage auf {site}",
           "Guten Tag,\n\nSie haben auf {site} {what} gesendet{nl}.\n\n"
           "Bitte bestätigen Sie mit diesem Link, dass die Anfrage von Ihnen stammt:\n{link}\n\n"
           "Der Link gilt {hours} Stunden und kann einmal verwendet werden. Ohne Bestätigung bearbeiten wir nichts "
           "und löschen Ihre Angaben nach {days} Tagen.\n\nWenn Sie das nicht waren, ignorieren Sie diese Mail einfach.{footer}"),
    "en": ("Please confirm: your request on {site}",
           "Hello,\n\nYou sent {what} on {site}{nl}.\n\n"
           "Please confirm with this link that the request came from you:\n{link}\n\n"
           "The link is valid for {hours} hours and can be used once. Without confirmation we do not process anything "
           "and delete your details after {days} days.\n\nIf this was not you, simply ignore this mail.{footer}"),
}
NL_NOTE = {"de": " und den Newsletter bestellt", "en": " and subscribed to the newsletter"}


def _fmt_date(iso: str) -> str:
    """'2026-12-03T09:00:00Z' -> '2026-12-03 10:00 CET' (Berlin time, where the webinars run;
    ISO order: unambiguous for the owner, who reads the CRM in English)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin"))
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _eur(x: float) -> str:
    return f"EUR {x:,.2f}"


def lead_for(row, ref: str) -> dict:
    """crm.lead content (sink-agnostic) from a confirmed submission. English: internal text for the
    owner (decision 2026-11-09); the contact's own language is the `lang` field / line."""
    kind, d = row["kind"], json.loads(row["data"])
    who = d.get("company") or d.get("name") or row["email"]
    lines: list[str]
    if kind == "contact":
        title = f"Website: Contact request – {who}"
        head = "Contact request via the contact form"
        body = ["Message:", d["message"]]
    elif kind == "inquiry":
        title = f"Website: Inquiry {d['subject_title']} – {who}"
        head = f"Inquiry about {d['subject_type']} “{d['subject_title']}” ({d['subject_id']})"
        body = ["Message:", d["message"]] if d.get("message") else []
    elif kind == "webinar":
        date = _fmt_date(d["date"])
        title = f"Website: Webinar {d['webinar_title']} {date[:10]} – {who}"
        head = f"Webinar registration: “{d['webinar_title']}” ({d['webinar_id']}), date {date}"
        body = []
    else:  # checkout
        title = f"Website: Order {d['product_title']} – {who}"
        head = f"Order in the website shop, order reference {d['order_ref']} (as submitted on the website, prices unchecked)"
        b = d["billing"]
        body = [f"Product: {d['product_title']} ({d['product_id']})"]
        if d.get("bundle"):
            body.append(f"Bundle: {d['bundle']}")
        if d.get("technology"):
            body.append(f"Technology: {d['technology']}")
        if d.get("services"):
            body.append("Additional services: " + "; ".join(d["services"]))
        body += [f"Net total per website: {_eur(d['price_net'])}",
                 f"Billing address: {b['street']}, {b['postal_code']} {b['city']}, {b['country']}",
                 f"VAT ID: {d.get('vat_id') or '-'}", "Terms accepted: yes"]
    lines = [f"{head}. Confirmed by double opt-in on {_fmt_date(row['confirmed_at'])} (language {row['lang']}).", ""]
    sal = selfcheck_api.resolve_salutation(d.get("salutation"), d.get("name")) if d.get("name") or "salutation" in d else None
    if sal is not None:  # stated by the form, else guessed from the name (owner 2026-11-10)
        lines.append(f"Salutation: {selfcheck_api.salutation_label(sal)}")
    for label, key in (("Name", "name"), ("Company", "company"), ("Phone", "phone")):
        if d.get(key):
            lines.append(f"{label}: {d[key]}")
    lines += [f"E-mail: {row['email']}", f"Newsletter: {'yes (confirmed)' if row['newsletter'] else 'no'}"]
    if row["page_url"]:
        lines.append(f"Page: {row['page_url']}")
    if body:
        lines += [""] + body
    site = brand.lead_site(row["site"])
    lines += ["", f"Source: {brand.label(site, LEAD_WEBSITE_URL)} / Website / campaign {kind}", f"Reference: {ref}"]
    return {"ref": ref, "kind": kind, "campaign": kind, "title": title[:200], "email": row["email"],
            "contact_name": d.get("name"), "salutation": sal, "partner_name": d.get("company"), "phone": d.get("phone"),
            "lang": row["lang"], "description": "\n".join(lines)}


async def finish(db: aiosqlite.Connection, sync: dict, outcome: str) -> None:
    """lead_sync hook for CTA leads. Failures are already mailed by lead_sync; with LEAD_SINK=none
    the lead is mailed to LEAD_NOTIFY_TO instead of being created (nothing gets lost)."""
    if outcome == "done" and sync["lead_sink"] == "none" and sync["lead"] and lead_sync.NOTIFY_TO:
        lead = json.loads(sync["lead"])
        await mailer.send(lead_sync.NOTIFY_TO, f"Website lead (not in CRM): {lead['title']}",
                          lead_sync.lead_text(sync), reply_to=lead["email"], audience="internal",
                          key=f"lead-none-{sync['id']}")


lead_sync.register("lead", finish)


# --------------------------------------------------------------------------- router


def _origin_ok(request: Request) -> bool:
    return request.headers.get("origin", "").rstrip("/") in LEAD_ALLOWED_ORIGINS


def _target(site: Optional[str], lang: str, ok: bool, kind: str = "") -> str:
    """After the click: the site's explicit target > its website_url > LEAD_WEBSITE_URL (brand.py)."""
    lang = brand.locale_for(site, lang)  # unknown locale -> the site's default_locale
    if ok:
        return brand.redirect_url(site, "lead", lang, "confirmed", env_base=LEAD_WEBSITE_URL,
                                  path=f"/{lang}/anfrage/bestaetigt", params={"art": kind})
    return brand.redirect_url(site, "lead", lang, "error", env_base=LEAD_WEBSITE_URL, path=f"/{lang}/anfrage/fehler")


def _redirect(lang: str, ok: bool, kind: str = "", status: int = 302, site: Optional[str] = None) -> RedirectResponse:
    return RedirectResponse(_target(site, lang, ok, kind), status_code=status)


async def _pending(db: aiosqlite.Connection, token: Optional[str]):
    """Read-only token check (own salt, max_age, pending row, SHA-256 constant time). Row or None."""
    if not _signer or not token or len(token) > 1000:
        return None
    try:
        data = _signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    lid = data.get("l") if isinstance(data, dict) else None
    if not isinstance(lid, int):
        return None
    async with db.execute("SELECT * FROM lead_submission WHERE id = ?", (lid,)) as cur:
        row = await cur.fetchone()
    if row is None or row["status"] != "pending" or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], _hash(token)):
        return None
    return row


async def _submit_job(db: aiosqlite.Connection, payload, site: str) -> None:
    """Contact-dependent part of POST /api/lead (after the response): throttle, store, DOI mail."""
    await purge(db)
    email = payload.email.lower()
    since = _iso(_now() - timedelta(hours=24))
    data = payload.model_dump(mode="json", exclude={"email", "lang", "newsletter", "page_url", "company_website", "kind"},
                              exclude_none=True)
    # cap + insert in ONE statement (atomic, no COUNT-then-INSERT race; security review 2026-11-09)
    cur = await db.execute(
        "INSERT INTO lead_submission (kind, created_at, email, lang, newsletter, page_url, data, site) "
        "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE (SELECT COUNT(*) FROM lead_submission WHERE email = ? AND created_at >= ?) < ?",
        (payload.kind, _iso(_now()), email, payload.lang, int(payload.newsletter), payload.page_url,
         json.dumps(data, ensure_ascii=False), site, email, since, MAX_PENDING_PER_EMAIL))
    if cur.rowcount != 1:
        await db.commit()  # end the (empty) implicit transaction
        logger.info("LEAD_THROTTLED kind=%s", payload.kind)
        return
    lid = cur.lastrowid
    token = _signer.dumps({"l": lid, "b": site})  # b: brand of the neutral page only
    await db.execute("UPDATE lead_submission SET token_hash = ? WHERE id = ?", (_hash(token), lid))
    await db.commit()
    subject, body = doi_mail(site, payload.lang, kind=payload.kind, newsletter=payload.newsletter,
                             link=f"{PUBLIC_BASE_URL}/api/lead/confirm?token={token}")
    ok = await mailer.send(email, subject, body, audience="doi", key=f"lead-doi-{lid}", sender=brand.sender(site))
    if not ok:
        await db.execute("DELETE FROM lead_submission WHERE id = ?", (lid,))
        await db.commit()
        logger.error("LEAD_DOI_MAIL_FAILED id=%s", lid)
        return
    logger.info("LEAD_PENDING id=%s kind=%s newsletter=%s", lid, payload.kind, payload.newsletter)


def doi_mail(site: str, lang: str, *, kind: str, newsletter: bool, link: str) -> tuple[str, str]:
    """(subject, body) of the lead DOI mail in the site's brand (server-side text only)."""
    label = brand.label(site, LEAD_WEBSITE_URL)
    foot = brand.footer(site, lang)
    subject, body = DOI[lang]
    return subject.format(site=label), body.format(
        site=label, what=KIND_LABEL[kind][lang], nl=NL_NOTE[lang] if newsletter else "", link=link,
        hours=CONFIRM_TOKEN_MAX_AGE // 3600, days=LEAD_PENDING_DAYS, footer=f"\n\n{foot}" if foot else "")


async def route_confirmed(db: aiosqlite.Connection, *, origin: str, origin_id: int, site: str, action: str,
                          lead: Optional[dict], subscribe: dict, data: dict) -> None:
    """Delivery of a confirmed lead / self-check: lead_sync (the CRM target, routed or fallback)
    + one outbox row per other routed target; a ticked newsletter box follows the site's
    subscribe routing when it has one (shared by leads_api and selfcheck_api)."""
    lists = list(subscribe.get("lists") or [])
    if lists and await routing.opted_out(db, site, subscribe["email"]):
        # do-not-contact (mauxy's opt-out record): no list subscription at all. The crm.lead (a contact
        # request the person confirmed) and the operator mail still happen; routed marketing webhooks
        # are skipped by the outbox gate (routing.GATED).
        logger.info("LEAD_LISTS_DNC_SKIP origin=%s id=%s", origin, origin_id)
        lists = []
    if "newsletter" in lists and (nl_to := routing.targets(site, "subscribe")):
        lists.remove("newsletter")
        await routing.fan_out(db, site=site, action="subscribe", event_ref=f"{origin}-nl:{origin_id}", to=nl_to, data={
            "email": subscribe["email"], "locale": subscribe["lang"], "source": subscribe["source"],
            "page_url": subscribe.get("page_url") or "", "name": subscribe.get("name"),
            "company": subscribe.get("company"), "lists": ["newsletter"]})
    await lead_sync.enqueue(db, origin=origin, origin_id=origin_id, lead=lead, subscribe={**subscribe, "lists": lists},
                            target=routing.crm_target(site, action))
    if to := routing.targets(site, action):
        await routing.fan_out(db, site=site, action=action, event_ref=f"{origin}:{origin_id}", to=to, data=data)


def build_router(*, limiter, admin_ok: Callable[[Request], bool]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/lead")
    @limiter.limit(LEAD_RATE_LIMIT)
    async def lead_submit(request: Request, payload: LeadRequest = Body(...)):
        if isinstance(payload, SelfCheckLead):  # alias of POST /api/selfcheck
            return await selfcheck_api.submit(payload, request)
        if not _origin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        if payload.company_website:  # honeypot: looks like success, nothing stored or sent
            return JSONResponse(PENDING)
        if not _signer or not PUBLIC_BASE_URL or not mailer.configured():
            logger.error("LEAD_NOT_CONFIGURED")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
        site = brand.site_for_origin(request.headers.get("origin"))
        jobs.spawn(_submit_job(request.app.state.db, payload, site), name="lead_submit")
        return JSONResponse(PENDING)

    @router.get("/api/lead/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def lead_confirm_page(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
        """DOI link, step 1 READ-ONLY (mail scanners prefetch links): confirmation page with a
        "Bestätigen" button (POST). Invalid / expired / used: uniform neutral page."""
        if not _signer:
            return _redirect("", False, site=brand.DEFAULT_SITE)
        row = await _pending(request.app.state.db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        site = brand.lead_site(row["site"])
        return confirm_page.render("lead", row["lang"], "/api/lead/confirm", token,
                                   [_target(site, row["lang"], True, row["kind"]), _target(site, row["lang"], False)],
                                   brand=brand.get(site))

    @router.post("/api/lead/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def lead_confirm(request: Request):
        """DOI step 2, the human click: claims the token atomically (single use), queues the
        delivery (crm.lead + newsletter if ticked), 303 to the website's confirmed page."""
        if not _signer:
            return _redirect("", False, status=303, site=brand.DEFAULT_SITE)
        if not confirm_page.same_origin(request):
            logger.warning("LEAD_CONFIRM_FOREIGN_ORIGIN")
            return confirm_page.neutral(403)
        token = await confirm_page.form_token(request)
        db: aiosqlite.Connection = request.app.state.db
        row = await _pending(db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        lid = row["id"]
        # burn first, conditionally: a parallel second POST finds no pending row (rowcount 0)
        cur = await db.execute("UPDATE lead_submission SET status = 'confirmed', confirmed_at = ?, token_hash = NULL "
                               "WHERE id = ? AND status = 'pending' AND token_hash = ?", (_iso(_now()), lid, _hash(token)))
        claimed = cur.rowcount == 1
        await db.commit()
        if not claimed:
            return confirm_page.neutral(brand=brand.get(brand.lead_site(row["site"])))
        async with db.execute("SELECT * FROM lead_submission WHERE id = ?", (lid,)) as cur:
            row = await cur.fetchone()
        d = json.loads(row["data"])
        site = brand.lead_site(row["site"])
        subscribe = {"email": row["email"], "lang": row["lang"], "source": f"lead-{row['kind']}",
                     "page_url": row["page_url"] or "", "name": d.get("name"), "company": d.get("company"),
                     "lists": ["newsletter"] if row["newsletter"] else []}
        lead = lead_for(row, lead_sync.new_ref())
        await route_confirmed(db, origin="lead", origin_id=lid, site=site, action="lead", lead=lead, subscribe=subscribe,
                              data={"ref": lead["ref"], "kind": row["kind"], "email": row["email"], "lang": row["lang"],
                                    "name": d.get("name"), "salutation": lead["salutation"], "company": d.get("company"),
                                    "phone": d.get("phone"),
                                    "newsletter": bool(row["newsletter"]), "page_url": row["page_url"] or ""})
        logger.info("LEAD_CONFIRMED id=%s kind=%s site=%s", lid, row["kind"], site)
        return _redirect(row["lang"], True, row["kind"], status=303, site=site)

    @router.get("/api/lead/submissions")
    async def lead_submissions(request: Request, status: Literal["pending", "confirmed"] = Query("confirmed"),
                               kind: Optional[Literal["contact", "inquiry", "webinar", "checkout"]] = Query(None),
                               limit: int = Query(100, ge=1, le=500)):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sql = ("SELECT s.id, s.kind, s.created_at, s.confirmed_at, s.email, s.lang, s.newsletter, s.page_url, s.data, "
               "l.id AS sync_id, l.state AS crm_state, l.odoo_lead_id, l.attempts, l.last_error, l.sub_results "
               "FROM lead_submission s LEFT JOIN lead_sync l ON l.origin = 'lead' AND l.origin_id = s.id WHERE s.status = ?")
        params: list = [status]
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.append(limit)
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["data"] = json.loads(r["data"])
            r["sub_results"] = json.loads(r["sub_results"]) if r["sub_results"] else None
        return JSONResponse({"submissions": rows, "count": len(rows), "retention_days": LEAD_RETENTION_DAYS})

    @router.post("/api/lead/sync/{sync_id}/retry")
    async def lead_sync_retry(request: Request, sync_id: int):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ok = await lead_sync.requeue(request.app.state.db, sync_id)
        return JSONResponse({"requeued": ok}, status_code=200 if ok else 409)

    return router
