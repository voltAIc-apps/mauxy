"""
Generic website CTA endpoint (flywheel SPEC-021): one double opt-in flow for every simplify-erp.de
call to action that ends as a crm.lead in erp.wapsol.

  POST /api/lead                 {kind: contact | inquiry | webinar | checkout | selfcheck, ...}
                                 Per-kind schema (extra fields refused, lengths limited), honeypot
                                 company_website, Origin allowlist. kind selfcheck = alias of
                                 POST /api/selfcheck (selfcheck_api.py). Immediate uniform
                                 200 {"status": "pending_confirmation"}; storage + ONE DOI mail run
                                 as a job after the response. Nothing goes to any sink here.
  GET  /api/lead/confirm?token=  single use (SHA-256, constant time), own salt. Burns the token,
                                 queues the delivery (lead_sync): crm.lead + newsletter if ticked.
                                 302 to {LEAD_WEBSITE_URL}/{lang}/anfrage/bestaetigt?art=<kind>
                                 (error: /{lang}/anfrage/fehler).
  GET  /api/lead/submissions     admin (Bearer ADMIN_API_KEY): submissions + CRM state.
  POST /api/lead/sync/{id}/retry admin: re-queue a failed CRM delivery.

DOI mails contain server-side text only (no browser-supplied text goes to an arbitrary address).
The lead (title + plain-text description) is rendered at confirmation from the submitted fields.
Retention: pending LEAD_PENDING_DAYS (7), confirmed LEAD_RETENTION_DAYS (30): the CRM is the
system of record once delivered.
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

import jobs
import lead_sync
import mailer
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


class ContactLead(_Base):
    kind: Literal["contact"]
    name: Line(120)
    company: Line(160)
    phone: Optional[Phone] = None
    message: Text(4000)


class InquiryLead(_Base):
    kind: Literal["inquiry"]
    name: Optional[Line(120)] = None
    company: Optional[Line(160)] = None
    phone: Optional[Phone] = None
    subject_type: Literal["app", "page", "webinar", "card", "offer"] = "app"
    subject_id: Slug
    subject_title: Line(200)
    message: Optional[Text(2000)] = None


class WebinarLead(_Base):
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


class CheckoutLead(_Base):
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
DOI = {
    "de": ("Bitte bestätigen: Ihre Anfrage auf simplify-erp.de",
           "Guten Tag,\n\nSie haben auf simplify-erp.de {what} gesendet{nl}.\n\n"
           "Bitte bestätigen Sie mit diesem Link, dass die Anfrage von Ihnen stammt:\n{link}\n\n"
           "Der Link gilt {hours} Stunden und kann einmal verwendet werden. Ohne Bestätigung bearbeiten wir nichts "
           "und löschen Ihre Angaben nach {days} Tagen.\n\nWenn Sie das nicht waren, ignorieren Sie diese Mail einfach.\n\n"
           "Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/de/impressum"),
    "en": ("Please confirm: your request on simplify-erp.de",
           "Hello,\n\nYou sent {what} on simplify-erp.de{nl}.\n\n"
           "Please confirm with this link that the request came from you:\n{link}\n\n"
           "The link is valid for {hours} hours and can be used once. Without confirmation we do not process anything "
           "and delete your details after {days} days.\n\nIf this was not you, simply ignore this mail.\n\n"
           "Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/en/impressum"),
}
NL_NOTE = {"de": " und den Newsletter bestellt", "en": " and subscribed to the newsletter"}


def _fmt_date(iso: str) -> str:
    """'2026-12-03T09:00:00Z' -> '03.12.2026 10:00 Uhr' (German time: audience and CRM users)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin"))
    return dt.strftime("%d.%m.%Y %H:%M Uhr")


def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def lead_for(row, ref: str) -> dict:
    """crm.lead content (sink-agnostic) from a confirmed submission. German (internal)."""
    kind, d = row["kind"], json.loads(row["data"])
    who = d.get("company") or d.get("name") or row["email"]
    lines: list[str]
    if kind == "contact":
        title = f"Website: Kontaktanfrage – {who}"
        head = "Kontaktanfrage über das Kontaktformular"
        body = ["Nachricht:", d["message"]]
    elif kind == "inquiry":
        title = f"Website: Anfrage {d['subject_title']} – {who}"
        head = f"Anfrage zu {d['subject_type']} „{d['subject_title']}“ ({d['subject_id']})"
        body = ["Nachricht:", d["message"]] if d.get("message") else []
    elif kind == "webinar":
        date = _fmt_date(d["date"])
        title = f"Website: Webinar {d['webinar_title']} {date[:10]} – {who}"
        head = f"Webinar-Anmeldung: „{d['webinar_title']}“ ({d['webinar_id']}), Termin {date}"
        body = []
    else:  # checkout
        title = f"Website: Bestellung {d['product_title']} – {who}"
        head = f"Bestellung im Website-Shop, Bestellreferenz {d['order_ref']} (Angaben laut Website, Preise ungeprüft)"
        b = d["billing"]
        body = [f"Produkt: {d['product_title']} ({d['product_id']})"]
        if d.get("bundle"):
            body.append(f"Paket: {d['bundle']}")
        if d.get("technology"):
            body.append(f"Technologie: {d['technology']}")
        if d.get("services"):
            body.append("Zusatzleistungen: " + "; ".join(d["services"]))
        body += [f"Summe netto laut Website: {_eur(d['price_net'])}",
                 f"Rechnungsadresse: {b['street']}, {b['postal_code']} {b['city']}, {b['country']}",
                 f"USt-IdNr.: {d.get('vat_id') or '-'}", "AGB akzeptiert: ja"]
    lines = [f"{head}. Bestätigt per Double-Opt-in am {_fmt_date(row['confirmed_at'])} (Sprache {row['lang']}).", ""]
    for label, key in (("Name", "name"), ("Firma", "company"), ("Telefon", "phone")):
        if d.get(key):
            lines.append(f"{label}: {d[key]}")
    lines += [f"E-Mail: {row['email']}", f"Newsletter: {'ja (bestätigt)' if row['newsletter'] else 'nein'}"]
    if row["page_url"]:
        lines.append(f"Seite: {row['page_url']}")
    if body:
        lines += [""] + body
    lines += ["", f"Quelle: simplify-erp.de / Website / Kampagne {kind}", f"Referenz: {ref}"]
    return {"ref": ref, "kind": kind, "campaign": kind, "title": title[:200], "email": row["email"],
            "contact_name": d.get("name"), "partner_name": d.get("company"), "phone": d.get("phone"),
            "lang": row["lang"], "description": "\n".join(lines)}


async def finish(db: aiosqlite.Connection, sync: dict, outcome: str) -> None:
    """lead_sync hook for CTA leads. Failures are already mailed by lead_sync; with LEAD_SINK=none
    the lead is mailed to LEAD_NOTIFY_TO instead of being created (nothing gets lost)."""
    if outcome == "done" and sync["lead_sink"] == "none" and sync["lead"] and lead_sync.NOTIFY_TO:
        lead = json.loads(sync["lead"])
        await mailer.send(lead_sync.NOTIFY_TO, f"Website-Lead (nicht im CRM): {lead['title']}",
                          lead_sync.lead_text(sync), reply_to=lead["email"])


lead_sync.register("lead", finish)


# --------------------------------------------------------------------------- router


def _origin_ok(request: Request) -> bool:
    return request.headers.get("origin", "").rstrip("/") in LEAD_ALLOWED_ORIGINS


def _redirect(lang: str, ok: bool, kind: str = "") -> RedirectResponse:
    lang = lang if lang in ("de", "en") else "de"
    path = f"anfrage/bestaetigt?art={kind}" if ok else "anfrage/fehler"
    return RedirectResponse(f"{LEAD_WEBSITE_URL}/{lang}/{path}", status_code=302)


async def _submit_job(db: aiosqlite.Connection, payload) -> None:
    """Contact-dependent part of POST /api/lead (after the response): throttle, store, DOI mail."""
    await purge(db)
    email = payload.email.lower()
    since = _iso(_now() - timedelta(hours=24))
    async with db.execute("SELECT COUNT(*) FROM lead_submission WHERE email = ? AND created_at >= ?", (email, since)) as cur:
        (recent,) = await cur.fetchone()
    if recent >= MAX_PENDING_PER_EMAIL:
        logger.info("LEAD_THROTTLED kind=%s", payload.kind)
        return
    data = payload.model_dump(mode="json", exclude={"email", "lang", "newsletter", "page_url", "company_website", "kind"},
                              exclude_none=True)
    cur = await db.execute(
        "INSERT INTO lead_submission (kind, created_at, email, lang, newsletter, page_url, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (payload.kind, _iso(_now()), email, payload.lang, int(payload.newsletter), payload.page_url,
         json.dumps(data, ensure_ascii=False)))
    lid = cur.lastrowid
    token = _signer.dumps({"l": lid})
    await db.execute("UPDATE lead_submission SET token_hash = ? WHERE id = ?", (_hash(token), lid))
    await db.commit()
    subject, body = DOI[payload.lang]
    ok = await mailer.send(email, subject, body.format(
        what=KIND_LABEL[payload.kind][payload.lang], nl=NL_NOTE[payload.lang] if payload.newsletter else "",
        link=f"{PUBLIC_BASE_URL}/api/lead/confirm?token={token}", hours=CONFIRM_TOKEN_MAX_AGE // 3600, days=LEAD_PENDING_DAYS))
    if not ok:
        await db.execute("DELETE FROM lead_submission WHERE id = ?", (lid,))
        await db.commit()
        logger.error("LEAD_DOI_MAIL_FAILED id=%s", lid)
        return
    logger.info("LEAD_PENDING id=%s kind=%s newsletter=%s", lid, payload.kind, payload.newsletter)


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
        jobs.spawn(_submit_job(request.app.state.db, payload), name="lead_submit")
        return JSONResponse(PENDING)

    @router.get("/api/lead/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def lead_confirm(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
        if not _signer:
            return _redirect("de", False)
        try:
            data = _signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
        except (SignatureExpired, BadSignature):
            return _redirect("de", False)
        lid = data.get("l") if isinstance(data, dict) else None
        if not isinstance(lid, int):
            return _redirect("de", False)
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute("SELECT * FROM lead_submission WHERE id = ?", (lid,)) as cur:
            row = await cur.fetchone()
        if row is None or row["status"] != "pending" or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], _hash(token)):
            return _redirect(row["lang"] if row else "de", False)
        # burn first: a parallel second click finds no pending row
        await db.execute("UPDATE lead_submission SET status = 'confirmed', confirmed_at = ?, token_hash = NULL WHERE id = ?",
                         (_iso(_now()), lid))
        await db.commit()
        async with db.execute("SELECT * FROM lead_submission WHERE id = ?", (lid,)) as cur:
            row = await cur.fetchone()
        d = json.loads(row["data"])
        subscribe = {"email": row["email"], "lang": row["lang"], "source": f"lead-{row['kind']}",
                     "page_url": row["page_url"] or "", "name": d.get("name"), "company": d.get("company"),
                     "lists": ["newsletter"] if row["newsletter"] else []}
        await lead_sync.enqueue(db, origin="lead", origin_id=lid, lead=lead_for(row, lead_sync.new_ref()), subscribe=subscribe)
        logger.info("LEAD_CONFIRMED id=%s kind=%s", lid, row["kind"])
        return _redirect(row["lang"], True, row["kind"])

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
