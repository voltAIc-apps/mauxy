"""
mauxy -- centralized newsletter subscription + unsubscribe proxy for Mautic, and the public
consent service of the simplify-erp.de Selbst-Check (selfcheck_api.py, flywheel SPEC-020).
Manages contacts, segments, and DNC lists via Basic Auth -- credentials never exposed to browser.

Double opt-in (newsletter and self-check) keeps ALL pending data server-side in SQLite and
changes nothing in Mautic before the confirmation link is clicked; confirmation tokens are
signed, time-limited and single-use (stored as SHA-256, compared in constant time).
"""
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
import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import mailer
import selfcheck_api

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Config from env --------------------------------------------------------
MAUTIC_BASE_URL = os.environ.get("MAUTIC_BASE_URL", "")
MAUTIC_USERNAME = os.environ.get("MAUTIC_USERNAME", "")
MAUTIC_PASSWORD = os.environ.get("MAUTIC_PASSWORD", "")
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
if not MAUTIC_BASE_URL:
    logger.warning("MAUTIC_BASE_URL is not set -- all unsubscribe requests will fail")
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

# -- Mautic health check cache -----------------------------------------------
_mautic_health = {"ok": True, "checked_at": 0.0, "detail": "pending"}
HEALTH_CHECK_CACHE_TTL = 30
HEALTH_CHECK_TIMEOUT = 5.0


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
    # Newsletter DOI: pending signups live here, NOT in Mautic, until confirmed.
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
    app.state.db = db
    logger.info("ACTION_LOG_DB opened: %s", ACTION_LOG_DB)
    yield
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


# -- Mautic connectivity check -----------------------------------------------
async def _check_mautic() -> dict:
    """Check Mautic API reachability; cache result for HEALTH_CHECK_CACHE_TTL seconds."""
    global _mautic_health
    now = time.monotonic()
    if now - _mautic_health["checked_at"] < HEALTH_CHECK_CACHE_TTL:
        return _mautic_health

    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            resp = await client.get(
                f"{MAUTIC_BASE_URL}/api/contacts?limit=1",
                auth=(MAUTIC_USERNAME, MAUTIC_PASSWORD),
            )
        if resp.status_code == 200:
            _mautic_health = {"ok": True, "checked_at": now, "detail": "reachable"}
        else:
            detail = f"HTTP {resp.status_code}"
            logger.warning("HEALTH_MAUTIC_HTTP_ERROR status=%s", resp.status_code)
            _mautic_health = {"ok": False, "checked_at": now, "detail": detail}
    except httpx.RequestError as exc:
        detail = f"connection error: {exc.__class__.__name__}"
        logger.error("HEALTH_MAUTIC_CONNECT_ERROR error=%s", exc)
        _mautic_health = {"ok": False, "checked_at": now, "detail": detail}
    except Exception as exc:
        detail = f"unexpected: {exc.__class__.__name__}"
        logger.error("HEALTH_MAUTIC_UNEXPECTED_ERROR error=%s", exc)
        _mautic_health = {"ok": False, "checked_at": now, "detail": detail}

    return _mautic_health


# -- Mautic contact helpers --------------------------------------------------
async def _search_mautic_contact(client: httpx.AsyncClient, email: str) -> tuple[int, dict | None]:
    """
    Search Mautic for exact-match contact by email.
    Returns (status_code, contact_dict_or_None). On non-200 status, contact is None.
    contact dict includes: {"id": str, "fields": ..., "doNotContact": [...], "tags": [...]}.
    """
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    resp = await client.get(
        f"{MAUTIC_BASE_URL}/api/contacts",
        params={
            "where[0][col]": "email",
            "where[0][expr]": "eq",
            "where[0][val]": email,
        },
        auth=auth,
    )
    if resp.status_code != 200:
        return resp.status_code, None

    contacts = resp.json().get("contacts", {}) or {}
    for cid, cdata in contacts.items():
        core = cdata.get("fields", {}).get("core", {})
        contact_email = (core.get("email", {}).get("value") or "").lower()
        if contact_email == email:
            return 200, {"id": cid, **cdata}
    return 200, None


def _contact_is_dnc(contact: dict) -> bool:
    """Mautic exposes DNC state via contact['doNotContact'] list with channel entries."""
    dnc = contact.get("doNotContact") or []
    return any(
        (entry.get("channel") == "email") and int(entry.get("reason", 0)) != 0
        for entry in dnc
    )


def _contact_has_tag(contact: dict, tag: str) -> bool:
    tags = contact.get("tags") or []
    for t in tags:
        # Mautic tag entries can be {"tag": "..."} or plain strings depending on API shape.
        name = t.get("tag") if isinstance(t, dict) else t
        if name == tag:
            return True
    return False


async def _upsert_contact(
    client: httpx.AsyncClient,
    *,
    existing_id: str | None,
    email: str,
    payload_fields: dict,
    add_tags: list[str],
    remove_tags: list[str] | None = None,
) -> tuple[int, dict | None]:
    """
    Create or patch a Mautic contact. Returns (status, contact_json) where contact_json
    is the Mautic response's 'contact' object. On failure returns (status, None).
    Mautic tag convention: prefix '-' on a tag string removes it.
    """
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    body: dict = {"email": email, **payload_fields}
    tag_strs = list(add_tags)
    if remove_tags:
        tag_strs.extend(f"-{t}" for t in remove_tags)
    if tag_strs:
        body["tags"] = tag_strs

    if existing_id:
        resp = await client.patch(
            f"{MAUTIC_BASE_URL}/api/contacts/{existing_id}/edit",
            json=body,
            auth=auth,
        )
    else:
        resp = await client.post(
            f"{MAUTIC_BASE_URL}/api/contacts/new",
            json=body,
            auth=auth,
        )
    if resp.status_code not in (200, 201):
        return resp.status_code, None
    return resp.status_code, resp.json().get("contact")




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
    Newsletter signup with double opt-in. Nothing is written to Mautic here.

    Flow:
      1. Honeypot filled -> uniform 200, nothing stored.
      2. Search Mautic (read only). Unreachable / 5xx -> 503 (not contact-specific).
      3. Already confirmed or DNC -> uniform 200, no mail (no enumeration: the HTTP answer is
         the same for every address).
      4. More than MAX_PENDING_PER_EMAIL requests for this address in 24h -> uniform 200, no mail.
      5. Store the request in SQLite (newsletter_pending) with the SHA-256 of a signed token
         ({"p": id}, CONFIRM_TOKEN_MAX_AGE) and mail the confirmation link via SMTP.
      6. Uniform 200 {"status": "pending_confirmation"}.
    """
    email = payload.email.lower()
    db: aiosqlite.Connection = request.app.state.db

    if payload.company_website:
        await log_action(request, email, "subscribe_honeypot")
        return JSONResponse(PENDING)

    if not _token_signer or not PUBLIC_BASE_URL or not mailer.configured():
        await log_action(request, email, "subscribe_error", error_detail="doi_not_configured")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    logger.info("SUBSCRIBE_START locale=%s source=%s", payload.locale, payload.source)

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            status, contact = await _search_mautic_contact(client, email)
        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_MAUTIC_UNREACHABLE error=%s", exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
    if status != 200:
        await log_action(request, email, "mautic_error", error_detail=f"search_status={status}")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    if contact and _contact_has_tag(contact, "newsletter_confirmed"):
        await log_action(request, email, "subscribe_already_confirmed", contact_id=str(contact["id"]))
        return JSONResponse(PENDING)
    if contact and _contact_is_dnc(contact):
        await log_action(request, email, "subscribe_dnc_skip", contact_id=str(contact["id"]))
        return JSONResponse(PENDING)

    await purge_newsletter_pending(db)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    async with db.execute(
        "SELECT COUNT(*) FROM newsletter_pending WHERE email = ? AND created_at >= ?", (email, since)
    ) as cur:
        (recent,) = await cur.fetchone()
    if recent >= MAX_PENDING_PER_EMAIL:
        await log_action(request, email, "subscribe_throttled")
        return JSONResponse(PENDING)

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
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    await log_action(request, email, "subscribe_ok")
    return JSONResponse(PENDING)


@app.get("/api/confirm")
@limiter.limit(CONFIRM_RATE_LIMIT)
async def confirm(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
    """
    Double opt-in landing (link in the DOI mail). Always a 302 back to the website.

    Checks, in order: signature + max_age; pending row exists; stored token hash equals the
    presented token (constant time) and is unused (single use); contact not DNC in Mautic.
    Only then the contact is created/updated in Mautic (tag newsletter_confirmed, server-side
    consent timestamp) and the token is burnt.
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            status, contact = await _search_mautic_contact(client, email)
        except httpx.RequestError as exc:
            await log_action(request, email, "confirm_error", error_detail=f"search_httpx: {exc}")
            return fail(locale, "service_unavailable")  # token stays valid, user can retry
        if status != 200:
            await log_action(request, email, "confirm_error", error_detail=f"search_status={status}")
            return fail(locale, "service_unavailable")

        async def burn():
            await db.execute("UPDATE newsletter_pending SET used_at = ?, token_hash = NULL WHERE id = ?", (now_iso(), pid))
            await db.commit()

        if contact and _contact_is_dnc(contact):
            await burn()
            await log_action(request, email, "confirm_dnc_refused", contact_id=str(contact["id"]))
            return fail(locale, "unavailable")

        existing_id = str(contact["id"]) if contact else None
        try:
            upsert_status, upserted = await _upsert_contact(
                client,
                existing_id=existing_id,
                email=email,
                payload_fields={
                    "confirmation_token": "",  # clears tokens of the pre-2026-11 flow
                    "newsletter_locale": locale,
                    "newsletter_source": row["source"],
                    "newsletter_consented_at": now_iso(),
                    "newsletter_page_url": row["page_url"],
                },
                add_tags=["newsletter_confirmed"],
                remove_tags=["newsletter_pending"],
            )
        except httpx.RequestError as exc:
            await log_action(request, email, "confirm_error", contact_id=existing_id, error_detail=f"upsert_httpx: {exc}")
            return fail(locale, "service_unavailable")
        if not upserted:
            await log_action(request, email, "confirm_error", contact_id=existing_id, error_detail=f"upsert_status={upsert_status}")
            return fail(locale, "service_unavailable")

    await burn()
    await log_action(request, email, "confirm_ok", contact_id=str(upserted.get("id") or existing_id or ""))
    return RedirectResponse(_website_redirect(locale, "newsletter/confirmed"), status_code=302)


# -- Routes -----------------------------------------------------------------
@app.get("/health")
async def health():
    """k8s liveness / readiness probe — always 200, includes Mautic status."""
    result = await _check_mautic()
    return {"status": "ok", "mautic": result["detail"]}


@app.get("/health/detail")
async def health_detail():
    """Richer health endpoint for operator debugging."""
    result = await _check_mautic()
    return {
        "status": "ok" if result["ok"] else "degraded",
        "mautic": result["detail"],
        "cache_age_seconds": round(time.monotonic() - result["checked_at"], 1),
    }


@app.post("/api/unsubscribe")
@limiter.limit(RATE_LIMIT)
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """
    Add email to Mautic DNC list.
    Returns 200 {"status":"ok"} for all contact-specific outcomes (prevents enumeration).
    Returns 503 {"status":"service_unavailable"} when Mautic cannot be reached at all.
    """
    email = payload.email.lower()
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    logger.info("UNSUBSCRIBE_START email=%s", email)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Phase 1: Search for contact -- failures here are email-independent, return 503
        search_url = f"{MAUTIC_BASE_URL}/api/contacts"
        try:
            resp = await client.get(
                search_url,
                params={
                    "where[0][col]": "email",
                    "where[0][expr]": "eq",
                    "where[0][val]": email,
                },
                auth=auth,
            )
        except httpx.RequestError as exc:
            logger.error("UNSUBSCRIBE_MAUTIC_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if resp.status_code != 200:
            logger.warning("UNSUBSCRIBE_SEARCH_FAILED email=%s status=%s", email, resp.status_code)
            await log_action(request, email, "mautic_error", error_detail=f"search_status={resp.status_code}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        # Phase 2: Contact-specific logic -- always 200 to prevent enumeration
        try:
            contacts = resp.json().get("contacts", {})
            if not contacts:
                logger.warning("UNSUBSCRIBE_NO_CONTACT email=%s", email)
                await log_action(request, email, "not_found")
                return JSONResponse({"status": "ok"})

            # Defense in depth: verify exact email match among results
            contact_id = None
            for cid, cdata in contacts.items():
                fields = cdata.get("fields", {}).get("core", {})
                contact_email = (fields.get("email", {}).get("value") or "").lower()
                if contact_email == email:
                    contact_id = cid
                    break

            if contact_id is None:
                candidates = list(contacts.keys())
                logger.warning("UNSUBSCRIBE_NO_EXACT_MATCH email=%s candidates=%s", email, candidates)
                await log_action(request, email, "not_found", error_detail=f"no_exact_match candidates={candidates}")
                return JSONResponse({"status": "ok"})

            logger.info("UNSUBSCRIBE_CONTACT_FOUND email=%s contact_id=%s", email, contact_id)

            # Add contact to DNC with retry (2 attempts)
            dnc_url = f"{MAUTIC_BASE_URL}/api/contacts/{contact_id}/dnc/email/add"
            dnc_ok = False
            for attempt in range(1, 3):
                dnc_resp = await client.post(
                    dnc_url,
                    json={"reason": 1, "comments": "Unsubscribed via website"},
                    auth=auth,
                )
                if dnc_resp.status_code in (200, 201):
                    logger.info("UNSUBSCRIBE_DNC_OK email=%s contact_id=%s", email, contact_id)
                    dnc_ok = True
                    break
                logger.warning(
                    "UNSUBSCRIBE_DNC_FAILED email=%s status=%s attempt=%d",
                    email, dnc_resp.status_code, attempt,
                )

            if dnc_ok:
                await log_action(request, email, "ok", contact_id=str(contact_id))
            else:
                logger.error("UNSUBSCRIBE_DNC_FAILED_RETRY_EXHAUSTED email=%s contact_id=%s", email, contact_id)
                await log_action(request, email, "error", contact_id=str(contact_id), error_detail="dnc_retry_exhausted")

        except httpx.RequestError as exc:
            logger.error("UNSUBSCRIBE_DNC_REQUEST_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "error", error_detail=f"httpx_error: {exc}")
        except Exception as exc:
            logger.error("UNSUBSCRIBE_UNEXPECTED_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "error", error_detail=f"unexpected: {exc}")

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


# -- Self-check (flywheel SPEC-020) -----------------------------------------------
app.include_router(selfcheck_api.build_router(
    limiter=limiter,
    search_contact=_search_mautic_contact,
    contact_is_dnc=_contact_is_dnc,
    upsert_contact=_upsert_contact,
    admin_ok=admin_ok,
    mautic=lambda: (MAUTIC_BASE_URL, MAUTIC_USERNAME, MAUTIC_PASSWORD),
))


class _NoEventAccessLog(logging.Filter):
    """The anonymous counter endpoint must not leave IPs in the access log (SPEC-020 s.6)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not any(isinstance(a, str) and a.startswith(selfcheck_api.EVENT_PATH) for a in args)


logging.getLogger("uvicorn.access").addFilter(_NoEventAccessLog())
