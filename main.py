"""
mauxy -- the website's interface to other systems (owner decision 2026-11-08): public consent
service of simplify-erp.de for newsletter, Migration Radar, Selbst-Check and every lead CTA.

  newsletter   POST /api/subscribe, GET /api/confirm, POST /api/unsubscribe   (this file)
  self-check   /api/selfcheck*                                              (selfcheck_api.py, SPEC-020)
  CTA leads    /api/lead*                                                   (leads_api.py, SPEC-021)
  login        /api/auth/linkedin/start|callback, /api/auth/session         (auth_api.py, sign-in only)

Confirmed data goes to sinks (sinks.py): crm.lead + Email Marketing lists in erp.wapsol (Odoo,
default) -- Mautic only as deprecated SUBSCRIBER_SINK=mautic. Credentials stay server-side.

Double opt-in keeps ALL pending data server-side in SQLite and writes nothing to any sink before
the confirmation link is clicked; confirmation tokens are signed, time-limited and single-use
(stored as SHA-256, compared in constant time).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import aiosqlite
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import auth_api
import jobs
import lead_sync
import leads_api
import mailer
import selfcheck_api
import sinks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Config from env --------------------------------------------------------
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5/minute")
CONFIRM_RATE_LIMIT = os.environ.get("CONFIRM_RATE_LIMIT", "30/minute")
ACTION_LOG_DB = os.environ.get("ACTION_LOG_DB", "/data/actions.db")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

# Newsletter double opt-in
CONFIRM_TOKEN_SECRET = os.environ.get("CONFIRM_TOKEN_SECRET", "")
CONFIRM_TOKEN_MAX_AGE = int(os.environ.get("CONFIRM_TOKEN_MAX_AGE", "86400"))  # 24h
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
PUBLIC_WEBSITE_BASE_URL = os.environ.get("PUBLIC_WEBSITE_BASE_URL", "").rstrip("/")
# Unconfirmed newsletter requests are deleted after this many days (data minimisation)
NEWSLETTER_PENDING_DAYS = int(os.environ.get("NEWSLETTER_PENDING_DAYS", "7"))
# Max. DOI mails per address and 24h (stops mail-bombing a victim through the form)
MAX_PENDING_PER_EMAIL = int(os.environ.get("MAX_PENDING_PER_EMAIL", "3"))

# CORS origins (comma-separated). Also the allowlist for the newsletter page_url field.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _raw_origins.split(",") if o.strip()]

# -- Startup validation -------------------------------------------------------
if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS is not set -- CORS will block all browser requests")
if not CONFIRM_TOKEN_SECRET:
    logger.warning("CONFIRM_TOKEN_SECRET is not set -- newsletter subscribe + confirm will fail")
if not PUBLIC_WEBSITE_BASE_URL:
    logger.warning("PUBLIC_WEBSITE_BASE_URL is not set -- confirm redirects will be broken")
if not PUBLIC_BASE_URL:
    logger.warning("PUBLIC_BASE_URL is not set -- confirmation links in mails will be broken")

# -- Token signer -----------------------------------------------------------
# Signed token for /api/confirm. Payload: {"p": <pending id>} only -- no e-mail address in the
# URL. The pending row (SQLite) holds the data; its SHA-256 token hash makes the token single-use.
_token_signer: URLSafeTimedSerializer | None = (
    URLSafeTimedSerializer(CONFIRM_TOKEN_SECRET, salt="mauxy-newsletter-confirm")
    if CONFIRM_TOKEN_SECRET
    else None
)

# -- Sink health check cache ---------------------------------------------------
_sink_health: dict = {"checked_at": -1e9, "detail": {}}
HEALTH_CHECK_CACHE_TTL = 30


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(stored_hash: str | None, token: str) -> bool:
    """Constant-time comparison of the stored hash with the presented token."""
    return bool(stored_hash) and hmac.compare_digest(stored_hash, token_hash(token))


def admin_ok(request: Request) -> bool:
    """Bearer ADMIN_API_KEY, constant-time compare (False when the key is unset)."""
    if not ADMIN_API_KEY:
        return False
    return hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {ADMIN_API_KEY}")


# -- SQLite lifespan --------------------------------------------------------
async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            email       TEXT    NOT NULL,
            source_origin TEXT,
            source_ip   TEXT,
            result      TEXT    NOT NULL,
            contact_id  TEXT,
            error_detail TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_email ON action_log(email)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_result ON action_log(result)")
    # Newsletter DOI: pending signups live here, NOT in any sink, until confirmed.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS newsletter_pending (
            id          TEXT PRIMARY KEY,
            token_hash  TEXT,
            email       TEXT NOT NULL,
            locale      TEXT NOT NULL,
            source      TEXT NOT NULL,
            page_url    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            used_at     TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nl_pending_email ON newsletter_pending(email)")
    await selfcheck_api.init_db(db)
    await leads_api.init_db(db)
    await lead_sync.init_db(db)
    await auth_api.init_db(db)
    await db.commit()


async def purge_newsletter_pending(db: aiosqlite.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWSLETTER_PENDING_DAYS)).isoformat(timespec="seconds")
    await db.execute("DELETE FROM newsletter_pending WHERE created_at < ?", (cutoff,))
    await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await aiosqlite.connect(ACTION_LOG_DB)
    db.row_factory = aiosqlite.Row
    await init_db(db)
    await purge_newsletter_pending(db)
    await selfcheck_api.purge(db)
    await leads_api.purge(db)
    await lead_sync.purge(db)
    await auth_api.purge(db)
    app.state.db = db
    logger.info("ACTION_LOG_DB opened: %s", ACTION_LOG_DB)
    await lead_sync.resume(db)  # CRM deliveries interrupted by a restart
    auth_purger = asyncio.get_running_loop().create_task(auth_api.purger(db), name="auth_purger")
    yield
    auth_purger.cancel()
    # finish pending DOI mails before the DB closes; retries sleeping in their backoff are
    # cancelled (state stays in SQLite, resumed on the next start)
    await jobs.drain(timeout=float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "10")))
    await db.close()
    logger.info("ACTION_LOG_DB closed")


# -- App setup --------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Mauxy",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# -- Helpers ----------------------------------------------------------------
async def log_action(
    request: Request,
    email: str,
    result: str,
    contact_id: Optional[str] = None,
    error_detail: Optional[str] = None,
):
    origin = request.headers.get("origin", "")
    ip = request.client.host if request.client else ""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        db: aiosqlite.Connection = request.app.state.db
        await db.execute(
            """INSERT INTO action_log
               (ts, email, source_origin, source_ip, result, contact_id, error_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, email, origin, ip, result, contact_id, error_detail),
        )
        await db.commit()
    except Exception as exc:
        logger.error("ACTION_LOG_WRITE_ERROR error=%s", exc)


def page_url_allowed(url: str) -> bool:
    """page_url must belong to one of the allowed site origins (no foreign URLs stored)."""
    return any(url == o or url.startswith(o + "/") or url.startswith(o + "?") for o in ALLOWED_ORIGINS)


# -- Models -----------------------------------------------------------------
class UnsubscribeRequest(BaseModel):
    email: EmailStr


class SubscribeRequest(BaseModel):
    """Accepts FE payload from website/src/config/api.ts (mauxySubscribe)."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    email: EmailStr
    locale: Literal["en", "de"]
    source: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")
    # Consent must be explicitly true (False -> 422)
    consent: Literal[True]
    # Client clock is not evidence: accepted for compatibility, length-limited, never stored.
    # The consent timestamp is the server time of the confirmation click.
    consented_at: Optional[str] = Field(default=None, alias="consentedAt", max_length=40)
    page_url: str = Field(alias="pageUrl", min_length=1, max_length=500)
    # Honeypot -- must be empty. Any non-empty value silently succeeds.
    company_website: str | None = Field(default=None, max_length=200)

    @field_validator("page_url")
    @classmethod
    def _page_url_on_site(cls, v: str) -> str:
        if not page_url_allowed(v):
            raise ValueError("page_url must be on an allowed site origin")
        return v


class SubscribeResponse(BaseModel):
    # Uniform for every contact-specific outcome (no enumeration).
    status: Literal["pending_confirmation"]


NEWSLETTER_DOI = {
    "de": (
        "Bitte bestätigen Sie Ihre Newsletter-Anmeldung",
        "Guten Tag,\n\nbitte bestätigen Sie Ihre Anmeldung zum Newsletter mit diesem Link:\n{link}\n\n"
        "Der Link gilt {hours} Stunden und kann einmal verwendet werden. Wenn Sie sich nicht angemeldet haben, "
        "ignorieren Sie diese Mail; wir speichern dann nichts weiter und löschen die Anfrage nach {days} Tagen.\n",
    ),
    "en": (
        "Please confirm your newsletter subscription",
        "Hello,\n\nplease confirm your newsletter subscription with this link:\n{link}\n\n"
        "The link is valid for {hours} hours and can be used once. If you did not subscribe, simply ignore "
        "this mail; we then store nothing further and delete the request after {days} days.\n",
    ),
}


# -- Sink health ---------------------------------------------------------------
async def _check_sinks() -> dict:
    """Lead + subscriber sink reachability; cached HEALTH_CHECK_CACHE_TTL seconds."""
    global _sink_health
    now = time.monotonic()
    if now - _sink_health["checked_at"] >= HEALTH_CHECK_CACHE_TTL:
        _sink_health = {"checked_at": now, "detail": await sinks.health()}
    return _sink_health


def _website_redirect(locale: str, path: str, reason: str | None = None) -> str:
    """Build redirect URL back to the website. Falls back to EN if locale not valid."""
    lang = locale if locale in ("en", "de") else "en"
    base = PUBLIC_WEBSITE_BASE_URL or ""
    qs = f"?reason={reason}" if reason else ""
    return f"{base}/{lang}/{path}{qs}"


PENDING = {"status": "pending_confirmation"}


@app.post("/api/subscribe")
@limiter.limit(RATE_LIMIT)
async def subscribe(payload: SubscribeRequest, request: Request):
    """
    Newsletter signup with double opt-in. Nothing is written to any sink here.

    Once the input is valid the answer is ALWAYS an immediate 200 {"status":
    "pending_confirmation"} -- same status, body and timing for every address (security
    review 2026-11-08: enumeration via response content, status code and timing). All
    contact-dependent work runs afterwards as a job (_subscribe_job): subscriber-sink lookup (read
    only: Odoo list / blacklist, or deprecated Mautic), skip if already subscribed / blocked /
    throttled, pending row in SQLite with the SHA-256 of a
    signed token ({"p": id}), DOI mail via SMTP. Failures there are logged, never returned.
    503 only for a misconfigured service (independent of the address).
    """
    email = payload.email.lower()
    if payload.company_website:  # honeypot
        await log_action(request, email, "subscribe_honeypot")
        return JSONResponse(PENDING)
    if not _token_signer or not PUBLIC_BASE_URL or not mailer.configured():
        await log_action(request, email, "subscribe_error", error_detail="doi_not_configured")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)
    jobs.spawn(_subscribe_job(request, payload, email), name="subscribe")
    return JSONResponse(PENDING)


async def _subscribe_job(request: Request, payload: SubscribeRequest, email: str) -> None:
    db: aiosqlite.Connection = request.app.state.db
    try:
        state = await sinks.subscriber_sink().status(email, "newsletter")
    except (sinks.SinkTransient, sinks.SinkPermanent) as exc:
        # not returned to the caller (enumeration); no DOI now, the person can retry later
        logger.error("SUBSCRIBE_SINK_ERROR error=%s", str(exc)[:200])
        await log_action(request, email, "sink_error", error_detail=str(exc)[:200])
        return
    if state == "subscribed":
        await log_action(request, email, "subscribe_already_confirmed")
        return
    if state == "blocked":
        await log_action(request, email, "subscribe_dnc_skip")
        return

    await purge_newsletter_pending(db)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    async with db.execute(
        "SELECT COUNT(*) FROM newsletter_pending WHERE email = ? AND created_at >= ?", (email, since)
    ) as cur:
        (recent,) = await cur.fetchone()
    if recent >= MAX_PENDING_PER_EMAIL:
        await log_action(request, email, "subscribe_throttled")
        return

    pid = secrets.token_urlsafe(16)
    token = _token_signer.dumps({"p": pid})
    await db.execute(
        "INSERT INTO newsletter_pending (id, token_hash, email, locale, source, page_url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, token_hash(token), email, payload.locale, payload.source, payload.page_url, now_iso()),
    )
    await db.commit()

    subject, body = NEWSLETTER_DOI[payload.locale]
    ok = await mailer.send(email, subject, body.format(
        link=f"{PUBLIC_BASE_URL}/api/confirm?token={token}",
        hours=CONFIRM_TOKEN_MAX_AGE // 3600, days=NEWSLETTER_PENDING_DAYS,
    ))
    if not ok:
        await db.execute("DELETE FROM newsletter_pending WHERE id = ?", (pid,))
        await db.commit()
        await log_action(request, email, "subscribe_error", error_detail="mail_failed")
        return
    await log_action(request, email, "subscribe_ok")


@app.get("/api/confirm")
@limiter.limit(CONFIRM_RATE_LIMIT)
async def confirm(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
    """
    Double opt-in landing (link in the DOI mail). Always a 302 back to the website.

    Checks, in order: signature + max_age; pending row exists; stored token hash equals the
    presented token (constant time) and is unused (single use). Then the subscriber sink
    subscribes (Odoo: mailing.contact / mailing.subscription on ODOO_LIST_NEWSLETTER_ID;
    deprecated Mautic: tag newsletter_confirmed, server-side consent time). Blocked (blacklist /
    DNC) -> token burnt, error page. Sink unreachable -> token stays valid, the person can retry.
    """
    db: aiosqlite.Connection = request.app.state.db

    def fail(locale: str, reason: str) -> RedirectResponse:
        return RedirectResponse(_website_redirect(locale, "newsletter/confirm-error", reason=reason), status_code=302)

    if not _token_signer or not PUBLIC_WEBSITE_BASE_URL:
        logger.error("CONFIRM_MISCONFIGURED signer=%s site=%s", bool(_token_signer), bool(PUBLIC_WEBSITE_BASE_URL))
        return fail("en", "server_misconfigured")

    try:
        data = _token_signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except SignatureExpired:
        await log_action(request, "", "confirm_expired")
        return fail("en", "invalid_or_expired")
    except BadSignature:
        await log_action(request, "", "confirm_invalid")
        return fail("en", "invalid_or_expired")

    pid = data.get("p") if isinstance(data, dict) else None
    row = None
    if isinstance(pid, str):
        async with db.execute("SELECT * FROM newsletter_pending WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
    # Legacy tokens ({email, locale}) and replays both end here: no pending row / hash burnt.
    if row is None or row["used_at"] or not tokens_equal(row["token_hash"], token):
        await log_action(request, row["email"] if row else "", "confirm_invalid", error_detail="not_pending_or_used")
        return fail(row["locale"] if row else "en", "invalid_or_expired")

    email, locale = row["email"], row["locale"]

    async def burn():
        await db.execute("UPDATE newsletter_pending SET used_at = ?, token_hash = NULL WHERE id = ?", (now_iso(), pid))
        await db.commit()

    try:
        result = await sinks.subscriber_sink().subscribe(
            email, "newsletter", lang=locale, source=row["source"], page_url=row["page_url"])
    except (sinks.SinkTransient, sinks.SinkPermanent) as exc:
        await log_action(request, email, "confirm_error", error_detail=str(exc)[:200])
        return fail(locale, "service_unavailable")  # token stays valid, user can retry
    if result == "blocked":
        await burn()
        await log_action(request, email, "confirm_dnc_refused")
        return fail(locale, "unavailable")

    await burn()
    await log_action(request, email, "confirm_ok", error_detail=None if result == "ok" else result)
    return RedirectResponse(_website_redirect(locale, "newsletter/confirmed"), status_code=302)


# -- Routes -----------------------------------------------------------------
@app.get("/health")
async def health():
    """k8s liveness / readiness probe -- always 200 and cheap (no sink call)."""
    return {"status": "ok"}


@app.get("/health/detail")
async def health_detail():
    """Operator view: sink reachability (cached 30 s). Always 200."""
    result = await _check_sinks()
    ok = all(not v.startswith("error") for v in result["detail"].values())
    return {
        "status": "ok" if ok else "degraded",
        **result["detail"],
        "auth_linkedin": auth_api.status(),  # 'configured' or the names of missing settings
        "cache_age_seconds": round(time.monotonic() - result["checked_at"], 1),
    }


@app.post("/api/unsubscribe")
@limiter.limit(RATE_LIMIT)
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """
    Opt-out via the subscriber sink (Odoo: opt_out on the address' subscriptions of the
    configured lists; deprecated Mautic: DNC list).
    Returns 200 {"status":"ok"} for all contact-specific outcomes (prevents enumeration).
    Returns 503 {"status":"service_unavailable"} when the sink cannot be reached at all.
    """
    email = payload.email.lower()
    try:
        result, ref = await sinks.subscriber_sink().unsubscribe(email)
    except sinks.SinkTransient as exc:
        logger.error("UNSUBSCRIBE_SINK_UNREACHABLE error=%s", str(exc)[:200])
        await log_action(request, email, "sink_unreachable", error_detail=str(exc)[:200])
        return JSONResponse({"status": "service_unavailable"}, status_code=503)
    except sinks.SinkPermanent as exc:
        logger.error("UNSUBSCRIBE_SINK_ERROR error=%s", str(exc)[:200])
        await log_action(request, email, "error", error_detail=str(exc)[:200])
        return JSONResponse({"status": "ok"})
    await log_action(request, email, result, contact_id=ref)
    # 200 for all contact-specific outcomes -- no enumeration leak
    return JSONResponse({"status": "ok"})


@app.get("/api/actions")
async def get_actions(
    request: Request,
    email: Optional[str] = Query(None),
    result: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Admin endpoint: query the action log. Requires Bearer token."""
    if not ADMIN_API_KEY:
        return JSONResponse({"error": "admin endpoint disabled"}, status_code=403)

    if not admin_ok(request):  # constant-time compare
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    clauses = []
    params: list = []
    if email:
        clauses.append("email = ?")
        params.append(email.lower())
    if result:
        clauses.append("result = ?")
        params.append(result)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM action_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    db: aiosqlite.Connection = request.app.state.db
    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(query, params)
    actions = [dict(row) for row in rows]
    return JSONResponse({"actions": actions, "count": len(actions)})


# -- Self-check (flywheel SPEC-020) and CTA leads (SPEC-021) ------------------------
app.include_router(selfcheck_api.build_router(limiter=limiter, admin_ok=admin_ok))
app.include_router(leads_api.build_router(limiter=limiter, admin_ok=admin_ok))
# -- Website visitor LinkedIn login (sign-in only; replaces app2li, owner decision 2026-11-08) --
app.include_router(auth_api.build_router(limiter=limiter))


class _NoEventAccessLog(logging.Filter):
    """The anonymous counter endpoint must not leave IPs in the access log (SPEC-020 s.6)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not any(isinstance(a, str) and a.startswith(selfcheck_api.EVENT_PATH) for a in args)


logging.getLogger("uvicorn.access").addFilter(_NoEventAccessLog())
