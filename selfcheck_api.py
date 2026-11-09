"""
Selbst-Check endpoints (simplify-erp.de /:lang/selbst-check, flywheel SPEC-020).

  POST /api/selfcheck            submission: e-mail + name + optional phone + picks (mail/call/radar)
                                 + answers. name required unless SELFCHECK_NAME_REQUIRED=false
                                 (rollout transition, db-patches/0010).
                                 Validated against the vendored rules, stored minimally, ONE
                                 double opt-in mail. Uniform 200 for every contact-specific outcome.
  GET  /api/selfcheck/confirm    DOI mail link, READ-ONLY: confirmation page with a "Bestätigen"
                                 button (confirm_page.py; mail scanners prefetch links). Invalid /
                                 expired / used: uniform neutral page.
  POST /api/selfcheck/confirm    form field token, the human click (atomic single-use claim):
                                 result mail (if picked), then the outbox (lead_sync):
                                 Migration Radar via the subscriber sink (if picked, not blacklisted),
                                 crm.lead in erp.wapsol (picks mail / call; SPEC-021), notification
                                 to the operator (always). 303 back to the website (?bestaetigt=1;
                                 misconfigured: =fehler).
                                 POST /api/lead with kind "selfcheck" is an alias of POST /api/selfcheck.
  POST /api/selfcheck/event      anonymous counter (started/completed/exited_early/submitted):
                                 stores day + event + lang only. No IP (global rate limit key,
                                 access log filtered), no cookie, no identifier.
  GET  /api/selfcheck/submissions  admin (Bearer ADMIN_API_KEY): confirmed submissions, for the
                                 indicative quote and the flywheel follow-up (G3 import).
  GET  /api/selfcheck/stats        admin: counters per day.

Nothing is written to any sink (Odoo / Mautic) before the confirmation. Retention: unconfirmed submissions are
deleted after SELFCHECK_PENDING_DAYS (7), confirmed ones after SELFCHECK_RETENTION_DAYS (90).
Rules + engine are vendored from flywheel (vendor/selfcheck, scripts/sync_selfcheck.py there).

Per site (db-patches/0006): site from the Origin (brand.site_for_origin), stored with the submission;
brand = mail sender, the vendored signature swapped for the site's mail footer (simplify-erp seed =
the same signature), website base of links / redirects, confirmation page. Routing (routing.py,
action "selfcheck"): the site's odoo target rides in lead_sync (crm.lead + Radar list), every other
target (Mautic Radar, flywheel webhook, ...) gets its own outbox row. The self-check CONTENT stays
the vendored simplify-erp rules.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

import brand
import confirm_page
import jobs
import lead_sync
import mailer
from vendor.selfcheck import selfcheck as engine

logger = logging.getLogger("mauxy.selfcheck")

EVENT_PATH = "/api/selfcheck/event"
RULES = engine.load_rules(os.environ.get("SELFCHECK_RULES_FILE") or None)

_raw = os.environ.get("SELFCHECK_ALLOWED_ORIGINS", "")
SELFCHECK_ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _raw.split(",") if o.strip()]
SELFCHECK_RATE_LIMIT = os.environ.get("SELFCHECK_RATE_LIMIT", "5/minute")
SELFCHECK_EVENT_RATE_LIMIT = os.environ.get("SELFCHECK_EVENT_RATE_LIMIT", "600/minute")
SELFCHECK_PENDING_DAYS = int(os.environ.get("SELFCHECK_PENDING_DAYS", "7"))
SELFCHECK_RETENTION_DAYS = int(os.environ.get("SELFCHECK_RETENTION_DAYS", "90"))
SELFCHECK_NOTIFY_TO = os.environ.get("SELFCHECK_NOTIFY_TO", "")
CONFIRM_TOKEN_SECRET = os.environ.get("CONFIRM_TOKEN_SECRET", "")
CONFIRM_TOKEN_MAX_AGE = int(os.environ.get("CONFIRM_TOKEN_MAX_AGE", "86400"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SELFCHECK_WEBSITE_URL = os.environ.get("SELFCHECK_WEBSITE_URL", RULES["website"]).rstrip("/")
MAX_PENDING_PER_EMAIL = int(os.environ.get("MAX_PENDING_PER_EMAIL", "3"))


def env_flag(raw: Optional[str], default: bool = True) -> bool:
    """'false' / '0' / 'no' / 'off' (any case) -> False, anything else -> True; unset / empty -> default."""
    raw = (raw or "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


# Rollout switch (owner 2026-11-09): name is required (default). Deploy mauxy with false first (the
# live website does not send it yet), then the website, then flip to true. Only a MISSING name is
# tolerated while false; a sent but blank / too long name is always 422.
SELFCHECK_NAME_REQUIRED = env_flag(os.environ.get("SELFCHECK_NAME_REQUIRED"))

if not SELFCHECK_ALLOWED_ORIGINS:
    logger.warning("SELFCHECK_ALLOWED_ORIGINS is not set -- /api/selfcheck rejects every browser origin")
if not SELFCHECK_NOTIFY_TO:
    logger.warning("SELFCHECK_NOTIFY_TO is not set -- confirmed self-checks reach nobody")

# Different salt than the newsletter: a newsletter token can never confirm a self-check.
_signer = URLSafeTimedSerializer(CONFIRM_TOKEN_SECRET, salt="mauxy-selfcheck-confirm") if CONFIRM_TOKEN_SECRET else None

PENDING = {"status": "pending_confirmation"}
QUESTION_IDS = [q["id"] for q in RULES["questions"]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- storage


# schema: db-patches/0003_website_consent.sql (applied by scripts/sqlite_db.py --migrate)
async def purge(db: aiosqlite.Connection) -> None:
    """Retention: unconfirmed after SELFCHECK_PENDING_DAYS, confirmed after SELFCHECK_RETENTION_DAYS."""
    now = _now()
    await db.execute(
        "DELETE FROM selfcheck_submission WHERE status = 'pending' AND created_at < ?",
        (_iso(now - timedelta(days=SELFCHECK_PENDING_DAYS)),),
    )
    await db.execute(
        "DELETE FROM selfcheck_submission WHERE status = 'confirmed' AND confirmed_at < ?",
        (_iso(now - timedelta(days=SELFCHECK_RETENTION_DAYS)),),
    )
    await db.commit()


# --------------------------------------------------------------------------- name / phone

NAME_MAX = 100   # first + last name in one field (website maxLength 100)
PHONE_MAX = 40
PHONE_RE = re.compile(r"^[0-9+()/ -]+$")  # loose: digits, spaces, + ( ) / - only (same as the website)
# invisible direction / line controls (category Cf / Zl / Zp): could reorder the operator mail or CRM text
_INVISIBLE = {"\u200e", "\u200f", "\u2028", "\u2029", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
              "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff"}


def clean_name(v: str) -> str:
    """Single line, no control characters: whitespace runs (incl. tab / newline) -> one space,
    other control (Cc) and direction characters removed, trimmed."""
    v = " ".join(v.split())
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cc" and ch not in _INVISIBLE)
    return " ".join(v.split())


def clean_phone(v: str) -> Optional[str]:
    """Trimmed phone or None when empty; ValueError for a foreign character / no digit / too long."""
    v = v.strip()
    if not v:
        return None
    if len(v) > PHONE_MAX or not PHONE_RE.match(v) or not any(ch.isdigit() for ch in v):
        raise ValueError("phone: digits, spaces, + ( ) / - only, max 40")
    return v


# Personal greeting of the result mail (Sie-form, after the DOI click only): replaces the vendored
# neutral opening line. The name is HTML-escaped (owner requirement: safe if the mail gets an HTML part).
GREETING = {"de": ("Guten Tag,", "Guten Tag {name},"), "en": ("Hello,", "Hello {name},")}


def greet(text: str, name: Optional[str], lang: str) -> str:
    plain, personal = GREETING[lang]
    if not name or not text.startswith(plain):
        return text
    return personal.format(name=name) + text[len(plain):]  # plain-text mail: no HTML escaping (name is control-char-stripped on input)


# --------------------------------------------------------------------------- models


class StayInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    licence: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    support: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    offer: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    users: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_users"])


class SelfCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    lang: Literal["de", "en"]
    picks: list[Literal["mail", "call", "radar"]] = Field(min_length=1, max_length=3)
    answers: dict[str, str | list[str]] = Field(max_length=len(QUESTION_IDS))
    stay: Optional[StayInputs] = None
    rules_version: str = Field(max_length=40)
    consent: Literal[True]
    # optional (SPEC-021): only used to name the CRM lead; letters, digits, punctuation, no controls
    company: Optional[str] = Field(default=None, min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")
    company_website: Optional[str] = Field(default=None, max_length=200)
    # first + last name (CRM contact_name, result mail greeting); raw cap here, NAME_MAX after cleaning
    name: Optional[str] = Field(default=None, max_length=400)
    phone: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = clean_name(v)
        if not v:
            raise ValueError("name is blank")
        if len(v) > NAME_MAX:
            raise ValueError(f"name longer than {NAME_MAX} characters")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else clean_phone(v)

    @model_validator(mode="after")
    def _name_required(self):
        if self.name is None and SELFCHECK_NAME_REQUIRED:
            raise ValueError("name is required")
        return self

    @field_validator("picks")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate pick")
        return v

    @field_validator("answers")
    @classmethod
    def _sizes(cls, v: dict) -> dict:
        for val in v.values():
            if isinstance(val, str) and len(val) > 40:
                raise ValueError("answer too long")
            if isinstance(val, list) and (len(val) > 12 or any(len(x) > 40 for x in val)):
                raise ValueError("answer too long")
        return v


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["started", "completed", "exited_early", "submitted"]
    lang: Literal["de", "en"] = "de"


# --------------------------------------------------------------------------- mail texts


def _picks_block(picks: list[str], lang: str) -> str:
    return "\n".join(f"- {t}" for t in engine.pick_titles(RULES, picks, lang))


# Signature the vendored mail texts end with; replaced by the site's footer (brand.replace_signature).
SIGNATURE = {lang: f"Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/{lang}/impressum"
             for lang in ("de", "en")}


def _site_url(lang: str, site: Optional[str] = None) -> str:
    return f"{brand.base_url(site, SELFCHECK_WEBSITE_URL)}/{lang}{RULES['route']}"


def _row_site(row) -> str:
    return brand.lead_site(row["site"] if "site" in row.keys() else "")


def result_mail(row: aiosqlite.Row) -> tuple[str, str]:
    lang = row["lang"]
    site = _row_site(row)
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    prog = engine.progress(RULES, answers)
    m = RULES["mail"]["result"]
    url = _site_url(lang, site)
    head = (m["intro"] if prog["complete"] else m["partial"])[lang].format(
        answered=prog["answered"], total=prog["total"], url=url)
    head = greet(head, row["name"], lang)
    body = "\n\n".join([head, engine.result_text(RULES, answers, lang, stay), m["outro"][lang].format(url=url)])
    return m["subject"][lang], brand.replace_signature(body, site, lang, SIGNATURE[lang])


def _summary(row, radar_result: str, mail_sent: Optional[bool]) -> tuple[str, list[str]]:
    """(short pick titles, German summary lines) shared by the operator mail and the CRM lead."""
    picks = json.loads(row["picks"])
    answers = json.loads(row["answers"])
    prog = engine.progress(RULES, answers)
    prec = RULES["precision"]["words"][prog["precision"]["level"]]["de"]
    short = ", ".join(engine.pick_titles(RULES, picks, "de"))
    lines = [
        f"Selbst-Check #{row['id']} bestätigt am {row['confirmed_at'] or _iso(_now())} (Sprache {row['lang']}).",
        f"E-Mail: {row['email']}",
    ]
    if row["name"]:
        lines.append(f"Name: {row['name']}")
    if row["phone"]:
        lines.append(f"Telefon: {row['phone']}")
    if row["company"]:
        lines.append(f"Firma: {row['company']}")
    lines += [
        f"Gewählt: {short}",
        f"Richtangebot: {'ja' if 'mail' in picks else 'nein'} ({prog['answered']} von {prog['total']} Antworten, Genauigkeit {prec})",
        f"Kennenlernen (Meetly): {'ja, Termin wählt die Person selbst' if 'call' in picks else 'nein'}",
        f"Migration Radar: {radar_result}",
    ]
    if mail_sent is not None:
        lines.append(f"Ergebnis-Mail an die Person: {'gesendet' if mail_sent else ('nicht gewählt' if 'mail' not in picks else 'FEHLER, bitte manuell senden')}")
    return short, lines


def _body(row, lines: list[str]) -> str:
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    return "\n".join(lines + ["", engine.result_text(RULES, answers, "de", stay), "",
                              f"Regeln {row['rules_version']} (Server {RULES['version']}). Aufbewahrung {SELFCHECK_RETENTION_DAYS} Tage."])


def notify_mail(row, radar_result: str, mail_sent: bool, crm_line: str = "") -> tuple[str, str]:
    """Operator mail (always German): indicative-quote request + call yes/no + answers + CRM lead."""
    answers = json.loads(row["answers"])
    prog = engine.progress(RULES, answers)
    prec = RULES["precision"]["words"][prog["precision"]["level"]]["de"]
    short, lines = _summary(row, radar_result, mail_sent)
    if crm_line:
        lines.append(crm_line)
    subject = RULES["mail"]["notify"]["subject"]["de"].format(picks_short=short, precision=prec)
    return subject, _body(row, lines)


def lead_for(row, ref: str) -> Optional[dict]:
    """CRM lead of a confirmed self-check (SPEC-021): only for picks mail (Richtangebot) / call.
    Radar alone is a subscription, not a lead. Title from submitted fields only."""
    picks = json.loads(row["picks"])
    if not ({"mail", "call"} & set(picks)):
        return None
    answers = json.loads(row["answers"])
    labels = {o["id"]: o["label"]["de"] for q in RULES["questions"] if q["id"] == "system" for o in q["options"]}
    picked = [labels[x] for x in (answers.get("system") or []) if x in labels]
    # short title: first system + count of the others (the full list is in the description)
    systems = picked[0] + (f" +{len(picked) - 1}" if len(picked) > 1 else "") if picked else ""
    who = row["company"] or row["name"] or row["email"]
    title = f"Selbst-Check: {systems} – {who}" if systems else f"Selbst-Check – {who}"
    _, lines = _summary(row, "gewählt" if "radar" in picks else "nicht gewählt", None)
    wanted = [t for p, t in zip(("mail", "call"), ("Ergebnis und Richtangebot per Mail", "30 Minuten Kennenlernen (Meetly)")) if p in picks]
    lines.insert(2, "Angefragt: " + "; ".join(wanted))
    if picked:
        lines.insert(3, "Systeme: " + " + ".join(picked))  # full list (title shows only the first)
    label = brand.label(_row_site(row), SELFCHECK_WEBSITE_URL)
    desc = _body(row, lines) + f"\n\nQuelle: {label} / Website / Kampagne selfcheck\nReferenz: {ref}"
    return {"ref": ref, "kind": "selfcheck", "campaign": "selfcheck", "title": title, "email": row["email"],
            "contact_name": row["name"], "partner_name": row["company"], "phone": row["phone"], "lang": row["lang"], "description": desc}


async def finish(db: aiosqlite.Connection, sync: dict, outcome: str) -> None:
    """lead_sync hook: store the Radar outcome, then the operator notification (always)."""
    sid = sync["origin_id"]
    async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    picks = json.loads(row["picks"])
    results = json.loads(sync["sub_results"] or "{}")
    if "radar" not in picks:
        radar = "nicht gewählt"
    else:
        radar = results.get("radar") or f"error:{(sync['last_error'] or 'unknown')[:80]}"
    if sync["odoo_lead_id"]:
        crm = f"CRM-Lead (erp.wapsol): #{sync['odoo_lead_id']}"
    elif not sync["lead"]:
        crm = "CRM-Lead: keiner (nur Radar)"
    elif outcome == "failed":
        crm = f"CRM-Lead: FEHLER, bitte manuell anlegen ({(sync['last_error'] or '')[:120]})"
    else:
        crm = "CRM-Lead: nicht angelegt (LEAD_SINK=none)"
    notify_sent = False
    if SELFCHECK_NOTIFY_TO:
        subject, body = notify_mail(row, radar, bool(row["mail_sent"]), crm)
        notify_sent = await mailer.send(SELFCHECK_NOTIFY_TO, subject, body, reply_to=row["email"],
                                        audience="internal", key=f"sc-notify-{sid}")
    await db.execute("UPDATE selfcheck_submission SET notify_sent = ?, radar_result = ? WHERE id = ?",
                     (int(notify_sent), radar, sid))
    await db.commit()
    logger.info("SELFCHECK_DELIVERED id=%s radar=%s lead=%s notify=%s", sid, radar, sync["odoo_lead_id"], notify_sent)


lead_sync.register("selfcheck", finish)


# --------------------------------------------------------------------------- router


def _global_key(*_args, **_kwargs) -> str:
    """Rate-limit key for the counter endpoint: one global bucket, the client IP is never read."""
    return "selfcheck-event"


def _origin_ok(request: Request) -> bool:
    return request.headers.get("origin", "").rstrip("/") in SELFCHECK_ALLOWED_ORIGINS


def _target(site: Optional[str], lang: str, ok: bool) -> str:
    """After the click: the site's explicit target > its website_url > SELFCHECK_WEBSITE_URL."""
    b = brand.get(site)
    lang = lang if lang in ("de", "en") else (b.default_locale if b else "de")
    return brand.redirect_url(site, "selfcheck", lang, "confirmed" if ok else "error", env_base=SELFCHECK_WEBSITE_URL,
                              path=f"/{lang}{RULES['route']}", params={"bestaetigt": "1" if ok else "fehler"})


def _redirect(lang: str, ok: bool, status: int = 302, site: Optional[str] = None) -> RedirectResponse:
    return RedirectResponse(_target(site, lang, ok), status_code=status)


async def _pending(db: aiosqlite.Connection, token: Optional[str]):
    """Read-only token check (own salt, max_age, pending row, SHA-256 constant time). Row or None."""
    if not _signer or not token or len(token) > 1000:
        return None
    try:
        data = _signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    sid = data.get("s") if isinstance(data, dict) else None
    if not isinstance(sid, int):
        return None
    async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    # single use: token hash is cleared on confirm; constant-time compare
    if row is None or row["status"] != "pending" or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], _hash(token)):
        return None
    return row


async def _submit_job(db: aiosqlite.Connection, payload: "SelfCheckRequest", clean: dict, site: str) -> None:
    """Contact-dependent part of POST /api/selfcheck (runs after the response is sent)."""
    await purge(db)
    email = payload.email.lower()
    since = _iso(_now() - timedelta(hours=24))
    stay = payload.stay.model_dump(exclude_none=True) if payload.stay else None
    prog = engine.progress(RULES, clean)
    # cap + insert in ONE statement (atomic, no COUNT-then-INSERT race; security review 2026-11-09)
    cur = await db.execute(
        "INSERT INTO selfcheck_submission (created_at, email, lang, picks, answers, stay, rules_version, complete, company, "
        "site, name, phone) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
        "WHERE (SELECT COUNT(*) FROM selfcheck_submission WHERE email = ? AND created_at >= ?) < ?",
        (_iso(_now()), email, payload.lang, json.dumps(payload.picks), json.dumps(clean, ensure_ascii=False),
         json.dumps(stay) if stay else None, payload.rules_version, int(prog["complete"]),
         " ".join(payload.company.split()) if payload.company else None, site, payload.name, payload.phone,
         email, since, MAX_PENDING_PER_EMAIL),
    )
    if cur.rowcount != 1:
        await db.commit()  # end the (empty) implicit transaction
        logger.info("SELFCHECK_THROTTLED")
        return
    sid = cur.lastrowid
    token = _signer.dumps({"s": sid, "b": site})  # b: brand of the neutral page only
    await db.execute("UPDATE selfcheck_submission SET token_hash = ? WHERE id = ?", (_hash(token), sid))
    await db.commit()

    doi = RULES["mail"]["doi"]
    body = brand.replace_signature(doi["body"][payload.lang].format(
        picks=_picks_block(payload.picks, payload.lang),
        link=f"{PUBLIC_BASE_URL}/api/selfcheck/confirm?token={token}",
        hours=CONFIRM_TOKEN_MAX_AGE // 3600,
        retention_pending_days=SELFCHECK_PENDING_DAYS,
    ), site, payload.lang, SIGNATURE[payload.lang])
    ok = await mailer.send(email, doi["subject"][payload.lang], body, audience="doi", key=f"sc-doi-{sid}",
                           sender=brand.sender(site))
    if not ok:
        await db.execute("DELETE FROM selfcheck_submission WHERE id = ?", (sid,))
        await db.commit()
        logger.error("SELFCHECK_DOI_MAIL_FAILED id=%s", sid)
        return
    logger.info("SELFCHECK_PENDING id=%s picks=%s complete=%s", sid, ",".join(payload.picks), prog["complete"])


async def submit(payload: SelfCheckRequest, request: Request) -> JSONResponse:
    """POST /api/selfcheck and POST /api/lead {kind: selfcheck}."""
    if not _origin_ok(request):
        return JSONResponse({"status": "forbidden"}, status_code=403)
    if payload.company_website:  # honeypot: looks like success, nothing stored or sent
        return JSONResponse(PENDING)
    clean, errors = engine.validate_answers(RULES, payload.answers)
    if errors:
        return JSONResponse({"status": "invalid", "detail": errors[:5]}, status_code=422)
    if not _signer or not PUBLIC_BASE_URL or not mailer.configured():
        logger.error("SELFCHECK_NOT_CONFIGURED")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    # From here on the answer is the same for every address, immediately (no enumeration via
    # content, status code or timing); storage + DOI mail run as a job after the response.
    site = brand.site_for_origin(request.headers.get("origin"))
    jobs.spawn(_submit_job(request.app.state.db, payload, clean, site), name="selfcheck_submit")
    return JSONResponse(PENDING)


def build_router(*, limiter, admin_ok: Callable[[Request], bool]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/selfcheck")
    @limiter.limit(SELFCHECK_RATE_LIMIT)
    async def selfcheck_submit(payload: SelfCheckRequest, request: Request):
        return await submit(payload, request)

    @router.get("/api/selfcheck/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def selfcheck_confirm_page(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
        """DOI link, step 1 READ-ONLY (mail scanners prefetch links): confirmation page with a
        "Bestätigen" button (POST). Invalid / expired / used: uniform neutral page."""
        if not _signer:
            return _redirect("de", False)
        row = await _pending(request.app.state.db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        site = _row_site(row)
        return confirm_page.render("selfcheck", row["lang"], "/api/selfcheck/confirm", token,
                                   [_target(site, row["lang"], True), _target(site, row["lang"], False)],
                                   brand=brand.get(site))

    @router.post("/api/selfcheck/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def selfcheck_confirm(request: Request):
        """DOI step 2, the human click: claims the token atomically (single use), result mail, then
        Radar + CRM lead + operator mail via the outbox; 303 back to the website."""
        if not _signer:
            return _redirect("de", False, status=303)
        if not confirm_page.same_origin(request):
            logger.warning("SELFCHECK_CONFIRM_FOREIGN_ORIGIN")
            return confirm_page.neutral(403)
        token = await confirm_page.form_token(request)
        db: aiosqlite.Connection = request.app.state.db
        row = await _pending(db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        sid = row["id"]
        confirmed_at = _iso(_now())
        # burn the token first, conditionally: a parallel second POST cannot trigger a second round of mails
        cur = await db.execute(
            "UPDATE selfcheck_submission SET status = 'confirmed', confirmed_at = ?, token_hash = NULL "
            "WHERE id = ? AND status = 'pending' AND token_hash = ?",
            (confirmed_at, sid, _hash(token)),
        )
        claimed = cur.rowcount == 1
        await db.commit()
        if not claimed:
            return confirm_page.neutral(brand=brand.get(_row_site(row)))
        async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
        site = _row_site(row)

        picks = json.loads(row["picks"])
        mail_sent = False
        if "mail" in picks:
            subject, body = result_mail(row)
            # address confirmed by the DOI click; the report PDF / images go here as mailer.Attachment
            mail_sent = await mailer.send(row["email"], subject, body, audience="confirmed", key=f"sc-result-{sid}",
                                          sender=brand.sender(site))
        await db.execute("UPDATE selfcheck_submission SET mail_sent = ? WHERE id = ?", (int(mail_sent), sid))
        await db.commit()

        # Radar subscription + CRM lead + operator mail: outbox with retries (lead_sync, SPEC-021)
        import leads_api  # local: leads_api imports this module (shared route_confirmed)
        ref = lead_sync.new_ref()
        page_url = _site_url(row["lang"], site)
        subscribe = {"email": row["email"], "lang": row["lang"], "source": "selfcheck", "page_url": page_url,
                     "name": row["name"], "company": row["company"], "lists": ["radar"] if "radar" in picks else []}
        await leads_api.route_confirmed(
            db, origin="selfcheck", origin_id=sid, site=site, action="selfcheck", lead=lead_for(row, ref),
            subscribe=subscribe, data={"ref": ref, "email": row["email"], "lang": row["lang"], "picks": picks,
                                       "name": row["name"], "phone": row["phone"], "company": row["company"],
                                       "source": "selfcheck", "page_url": page_url})
        logger.info("SELFCHECK_CONFIRMED id=%s mail=%s site=%s", sid, mail_sent, site)
        return _redirect(row["lang"], True, status=303, site=site)

    @router.post(EVENT_PATH, status_code=204)
    @limiter.limit(SELFCHECK_EVENT_RATE_LIMIT, key_func=_global_key)
    async def selfcheck_event(payload: EventRequest, request: Request):
        if not _origin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        db: aiosqlite.Connection = request.app.state.db
        await db.execute(
            "INSERT INTO selfcheck_counter (day, event, lang, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(day, event, lang) DO UPDATE SET count = count + 1",
            (_now().date().isoformat(), payload.event, payload.lang),
        )
        await db.commit()
        return Response(status_code=204)

    @router.get("/api/selfcheck/submissions")
    async def selfcheck_submissions(
        request: Request,
        status: Literal["pending", "confirmed"] = Query("confirmed"),
        since: Optional[str] = Query(None, max_length=40),
        limit: int = Query(100, ge=1, le=500),
    ):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sql = ("SELECT s.id, s.created_at, s.confirmed_at, s.email, s.name, s.phone, s.company, s.lang, s.picks, s.answers, s.stay, "
               "s.rules_version, s.complete, s.mail_sent, s.notify_sent, s.radar_result, "
               "l.odoo_lead_id, l.state AS crm_state FROM selfcheck_submission s "
               "LEFT JOIN lead_sync l ON l.origin = 'selfcheck' AND l.origin_id = s.id WHERE s.status = ?")
        params: list = [status]
        if since:
            sql += " AND COALESCE(s.confirmed_at, s.created_at) >= ?"
            params.append(since)
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.append(limit)
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("picks", "answers", "stay"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return JSONResponse({"submissions": out, "count": len(out), "retention_days": SELFCHECK_RETENTION_DAYS})

    @router.get("/api/selfcheck/stats")
    async def selfcheck_stats(request: Request, days: int = Query(30, ge=1, le=366)):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        since = (_now().date() - timedelta(days=days - 1)).isoformat()
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(
            "SELECT day, event, lang, count FROM selfcheck_counter WHERE day >= ? ORDER BY day, event, lang", (since,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        totals: dict[str, int] = {}
        for r in rows:
            totals[r["event"]] = totals.get(r["event"], 0) + r["count"]
        return JSONResponse({"since": since, "totals": totals, "rows": rows})

    return router
