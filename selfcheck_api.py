"""
Selbst-Check endpoints (simplify-erp.de /:lang/selbst-check, flywheel SPEC-020).

  POST /api/selfcheck            submission: e-mail + picks (mail/call/radar) + answers.
                                 Validated against the vendored rules, stored minimally, ONE
                                 double opt-in mail. Uniform 200 for every contact-specific outcome.
  GET  /api/selfcheck/confirm    DOI link: result mail (if picked), Migration Radar in Mautic
                                 (if picked, not DNC), notification to the operator (always).
                                 Then 302 back to the website (?bestaetigt=1 / =fehler).
  POST /api/selfcheck/event      anonymous counter (started/completed/exited_early/submitted):
                                 stores day + event + lang only. No IP (global rate limit key,
                                 access log filtered), no cookie, no identifier.
  GET  /api/selfcheck/submissions  admin (Bearer ADMIN_API_KEY): confirmed submissions, for the
                                 indicative quote and the flywheel follow-up (G3 import).
  GET  /api/selfcheck/stats        admin: counters per day.

Nothing is written to Mautic before the confirmation. Retention: unconfirmed submissions are
deleted after SELFCHECK_PENDING_DAYS (7), confirmed ones after SELFCHECK_RETENTION_DAYS (90).
Rules + engine are vendored from flywheel (vendor/selfcheck, scripts/sync_selfcheck.py there).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Literal, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

import jobs
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
MAUTIC_RADAR_TAG = os.environ.get("MAUTIC_RADAR_TAG", "migration_radar_confirmed")
MAUTIC_RADAR_SEGMENT_ID = os.environ.get("MAUTIC_RADAR_SEGMENT_ID", "")
CONFIRM_TOKEN_SECRET = os.environ.get("CONFIRM_TOKEN_SECRET", "")
CONFIRM_TOKEN_MAX_AGE = int(os.environ.get("CONFIRM_TOKEN_MAX_AGE", "86400"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SELFCHECK_WEBSITE_URL = os.environ.get("SELFCHECK_WEBSITE_URL", RULES["website"]).rstrip("/")
MAX_PENDING_PER_EMAIL = int(os.environ.get("MAX_PENDING_PER_EMAIL", "3"))

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


async def init_db(db: aiosqlite.Connection) -> None:
    # Minimal: what the DOI and the indicative quote need. No IP, no user agent.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS selfcheck_submission (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            email         TEXT NOT NULL,
            lang          TEXT NOT NULL,
            picks         TEXT NOT NULL,
            answers       TEXT NOT NULL,
            stay          TEXT,
            rules_version TEXT NOT NULL,
            complete      INTEGER NOT NULL,
            token_hash    TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            confirmed_at  TEXT,
            mail_sent     INTEGER,
            notify_sent   INTEGER,
            radar_result  TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sc_email ON selfcheck_submission(email, created_at)")
    # Anonymous counters: day + event + lang, nothing else.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS selfcheck_counter (
            day    TEXT NOT NULL,
            event  TEXT NOT NULL,
            lang   TEXT NOT NULL,
            count  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, event, lang)
        )
    """)


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
    company_website: Optional[str] = Field(default=None, max_length=200)

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


def _site_url(lang: str) -> str:
    return f"{SELFCHECK_WEBSITE_URL}/{lang}{RULES['route']}"


def result_mail(row: aiosqlite.Row) -> tuple[str, str]:
    lang = row["lang"]
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    prog = engine.progress(RULES, answers)
    m = RULES["mail"]["result"]
    url = _site_url(lang)
    head = (m["intro"] if prog["complete"] else m["partial"])[lang].format(
        answered=prog["answered"], total=prog["total"], url=url)
    body = "\n\n".join([head, engine.result_text(RULES, answers, lang, stay), m["outro"][lang].format(url=url)])
    return m["subject"][lang], body


def notify_mail(row: aiosqlite.Row, radar_result: str, mail_sent: bool) -> tuple[str, str]:
    """Operator mail (always German): indicative-quote request + call yes/no + answers."""
    picks = json.loads(row["picks"])
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    prog = engine.progress(RULES, answers)
    prec = RULES["precision"]["words"][prog["precision"]["level"]]["de"]
    short = ", ".join(engine.pick_titles(RULES, picks, "de"))
    subject = RULES["mail"]["notify"]["subject"]["de"].format(picks_short=short, precision=prec)
    lines = [
        f"Selbst-Check #{row['id']} bestätigt am {row['confirmed_at'] or _iso(_now())} (Sprache {row['lang']}).",
        f"E-Mail: {row['email']}",
        f"Gewählt: {short}",
        f"Richtangebot: {'ja' if 'mail' in picks else 'nein'} ({prog['answered']} von {prog['total']} Antworten, Genauigkeit {prec})",
        f"Kennenlernen (Meetly): {'ja, Termin wählt die Person selbst' if 'call' in picks else 'nein'}",
        f"Migration Radar: {radar_result}",
        f"Ergebnis-Mail an die Person: {'gesendet' if mail_sent else ('nicht gewählt' if 'mail' not in picks else 'FEHLER, bitte manuell senden')}",
        "",
        engine.result_text(RULES, answers, "de", stay),
        "",
        f"Regeln {row['rules_version']} (Server {RULES['version']}). Aufbewahrung {SELFCHECK_RETENTION_DAYS} Tage.",
    ]
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------- router


def _global_key(*_args, **_kwargs) -> str:
    """Rate-limit key for the counter endpoint: one global bucket, the client IP is never read."""
    return "selfcheck-event"


def _origin_ok(request: Request) -> bool:
    return request.headers.get("origin", "").rstrip("/") in SELFCHECK_ALLOWED_ORIGINS


def _redirect(lang: str, ok: bool) -> RedirectResponse:
    lang = lang if lang in ("de", "en") else "de"
    return RedirectResponse(f"{_site_url(lang)}?bestaetigt={'1' if ok else 'fehler'}", status_code=302)


async def _submit_job(db: aiosqlite.Connection, payload: "SelfCheckRequest", clean: dict) -> None:
    """Contact-dependent part of POST /api/selfcheck (runs after the response is sent)."""
    await purge(db)
    email = payload.email.lower()
    since = _iso(_now() - timedelta(hours=24))
    async with db.execute(
        "SELECT COUNT(*) FROM selfcheck_submission WHERE email = ? AND created_at >= ?", (email, since)
    ) as cur:
        (recent,) = await cur.fetchone()
    if recent >= MAX_PENDING_PER_EMAIL:
        logger.info("SELFCHECK_THROTTLED")
        return

    stay = payload.stay.model_dump(exclude_none=True) if payload.stay else None
    prog = engine.progress(RULES, clean)
    cur = await db.execute(
        "INSERT INTO selfcheck_submission (created_at, email, lang, picks, answers, stay, rules_version, complete) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_iso(_now()), email, payload.lang, json.dumps(payload.picks), json.dumps(clean, ensure_ascii=False),
         json.dumps(stay) if stay else None, payload.rules_version, int(prog["complete"])),
    )
    sid = cur.lastrowid
    token = _signer.dumps({"s": sid})
    await db.execute("UPDATE selfcheck_submission SET token_hash = ? WHERE id = ?", (_hash(token), sid))
    await db.commit()

    doi = RULES["mail"]["doi"]
    ok = await mailer.send(email, doi["subject"][payload.lang], doi["body"][payload.lang].format(
        picks=_picks_block(payload.picks, payload.lang),
        link=f"{PUBLIC_BASE_URL}/api/selfcheck/confirm?token={token}",
        hours=CONFIRM_TOKEN_MAX_AGE // 3600,
        retention_pending_days=SELFCHECK_PENDING_DAYS,
    ))
    if not ok:
        await db.execute("DELETE FROM selfcheck_submission WHERE id = ?", (sid,))
        await db.commit()
        logger.error("SELFCHECK_DOI_MAIL_FAILED id=%s", sid)
        return
    logger.info("SELFCHECK_PENDING id=%s picks=%s complete=%s", sid, ",".join(payload.picks), prog["complete"])


def build_router(
    *,
    limiter,
    search_contact: Callable[[httpx.AsyncClient, str], Awaitable[tuple[int, dict | None]]],
    contact_is_dnc: Callable[[dict], bool],
    upsert_contact: Callable[..., Awaitable[tuple[int, dict | None]]],
    admin_ok: Callable[[Request], bool],
    mautic: Callable[[], tuple[str, str, str]],
) -> APIRouter:
    """Router with mauxy's Mautic helpers injected (no import cycle with main.py)."""
    router = APIRouter()

    async def radar_subscribe(email: str, lang: str) -> str:
        """Migration Radar in Mautic, only after DOI. Returns ok / dnc_skip / error:<detail>."""
        base, user, pwd = mautic()
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                status, contact = await search_contact(client, email)
                if status != 200:
                    return f"error:search_status={status}"
                if contact and contact_is_dnc(contact):
                    return "dnc_skip"
                ustatus, upserted = await upsert_contact(
                    client,
                    existing_id=str(contact["id"]) if contact else None,
                    email=email,
                    payload_fields={
                        "newsletter_locale": lang,
                        "newsletter_source": "selfcheck",
                        "newsletter_consented_at": _iso(_now()),
                        "newsletter_page_url": _site_url(lang),
                    },
                    add_tags=[MAUTIC_RADAR_TAG],
                )
                if not upserted:
                    return f"error:upsert_status={ustatus}"
                if MAUTIC_RADAR_SEGMENT_ID:
                    resp = await client.post(
                        f"{base}/api/segments/{MAUTIC_RADAR_SEGMENT_ID}/contact/{upserted.get('id')}/add",
                        auth=(user, pwd),
                    )
                    if resp.status_code not in (200, 201):
                        return f"error:segment_status={resp.status_code}"
                return "ok"
            except httpx.RequestError as exc:
                return f"error:{exc.__class__.__name__}"

    @router.post("/api/selfcheck")
    @limiter.limit(SELFCHECK_RATE_LIMIT)
    async def selfcheck_submit(payload: SelfCheckRequest, request: Request):
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
        jobs.spawn(_submit_job(request.app.state.db, payload, clean), name="selfcheck_submit")
        return JSONResponse(PENDING)

    @router.get("/api/selfcheck/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def selfcheck_confirm(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
        if not _signer:
            return _redirect("de", False)
        try:
            data = _signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
        except (SignatureExpired, BadSignature):
            return _redirect("de", False)
        sid = data.get("s") if isinstance(data, dict) else None
        if not isinstance(sid, int):
            return _redirect("de", False)

        db: aiosqlite.Connection = request.app.state.db
        async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
        # single use: token hash is cleared on confirm; constant-time compare
        if row is None or row["status"] != "pending" or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], _hash(token)):
            return _redirect(row["lang"] if row else "de", False)

        confirmed_at = _iso(_now())
        # burn the token first: a parallel second click cannot trigger a second round of mails
        await db.execute(
            "UPDATE selfcheck_submission SET status = 'confirmed', confirmed_at = ?, token_hash = NULL WHERE id = ?",
            (confirmed_at, sid),
        )
        await db.commit()
        async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()

        picks = json.loads(row["picks"])
        radar = await radar_subscribe(row["email"], row["lang"]) if "radar" in picks else "nicht gewählt"
        mail_sent = False
        if "mail" in picks:
            subject, body = result_mail(row)
            mail_sent = await mailer.send(row["email"], subject, body)
        notify_sent = False
        if SELFCHECK_NOTIFY_TO:
            subject, body = notify_mail(row, radar, mail_sent)
            notify_sent = await mailer.send(SELFCHECK_NOTIFY_TO, subject, body, reply_to=row["email"])
        await db.execute(
            "UPDATE selfcheck_submission SET mail_sent = ?, notify_sent = ?, radar_result = ? WHERE id = ?",
            (int(mail_sent), int(notify_sent), radar, sid),
        )
        await db.commit()
        logger.info("SELFCHECK_CONFIRMED id=%s radar=%s mail=%s notify=%s", sid, radar, mail_sent, notify_sent)
        return _redirect(row["lang"], True)

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
        sql = ("SELECT id, created_at, confirmed_at, email, lang, picks, answers, stay, rules_version, complete, "
               "mail_sent, notify_sent, radar_result FROM selfcheck_submission WHERE status = ?")
        params: list = [status]
        if since:
            sql += " AND COALESCE(confirmed_at, created_at) >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
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
