"""
mauxy -- centralized newsletter subscription + unsubscribe proxy for Mautic.
Manages contacts, segments, and DNC lists via Basic Auth -- credentials never
exposed to browser.
"""
import os
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import aiosqlite
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

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
MAUTIC_CONFIRM_EMAIL_ID_EN = os.environ.get("MAUTIC_CONFIRM_EMAIL_ID_EN", "")
MAUTIC_CONFIRM_EMAIL_ID_DE = os.environ.get("MAUTIC_CONFIRM_EMAIL_ID_DE", "")

# CORS origins (comma-separated)
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "",
)
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# -- Startup validation -------------------------------------------------------
if not MAUTIC_BASE_URL:
    logger.warning("MAUTIC_BASE_URL is not set -- all unsubscribe requests will fail")
if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS is not set -- CORS will block all browser requests")
if not CONFIRM_TOKEN_SECRET:
    logger.warning("CONFIRM_TOKEN_SECRET is not set -- newsletter subscribe + confirm will fail")
if not PUBLIC_WEBSITE_BASE_URL:
    logger.warning("PUBLIC_WEBSITE_BASE_URL is not set -- confirm redirects will be broken")
if not (MAUTIC_CONFIRM_EMAIL_ID_EN and MAUTIC_CONFIRM_EMAIL_ID_DE):
    logger.warning(
        "MAUTIC_CONFIRM_EMAIL_ID_{EN,DE} not both set -- subscribe will fail for missing locale"
    )

# -- Token signer -----------------------------------------------------------
# Self-contained signed token for /api/confirm. Payload: {email, locale}.
# Expiry enforced via max_age on loads(); no server-side state needed.
_token_signer: URLSafeTimedSerializer | None = (
    URLSafeTimedSerializer(CONFIRM_TOKEN_SECRET, salt="mauxy-newsletter-confirm")
    if CONFIRM_TOKEN_SECRET
    else None
)

# -- Mautic health check cache -----------------------------------------------
_mautic_health = {"ok": True, "checked_at": 0.0, "detail": "pending"}
HEALTH_CHECK_CACHE_TTL = 30
HEALTH_CHECK_TIMEOUT = 5.0


# -- SQLite lifespan --------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await aiosqlite.connect(ACTION_LOG_DB)
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
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_log_email ON action_log(email)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_log_result ON action_log(result)"
    )
    await db.commit()
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


# -- Models -----------------------------------------------------------------
class UnsubscribeRequest(BaseModel):
    email: EmailStr


class SubscribeRequest(BaseModel):
    """Accepts FE payload from website/src/config/api.ts (mauxySubscribe)."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    email: EmailStr
    locale: Literal["en", "de"]
    source: str
    consent: bool
    consented_at: str = Field(alias="consentedAt")
    page_url: str = Field(alias="pageUrl")
    # Honeypot -- must be empty. Any non-empty value silently succeeds.
    company_website: str | None = None


class SubscribeResponse(BaseModel):
    status: Literal[
        "pending_confirmation",
        "already_subscribed",
        "already_unsubscribed_no_email_sent",
    ]


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


async def _send_mautic_email(client: httpx.AsyncClient, email_id: str, contact_id: str) -> int:
    """
    Trigger a transactional send of Mautic email template to a specific contact.
    Returns HTTP status code.
    """
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    resp = await client.post(
        f"{MAUTIC_BASE_URL}/api/emails/{email_id}/contact/{contact_id}/send",
        auth=auth,
    )
    return resp.status_code


def _confirm_email_id(locale: str) -> str:
    return MAUTIC_CONFIRM_EMAIL_ID_DE if locale == "de" else MAUTIC_CONFIRM_EMAIL_ID_EN


def _website_redirect(locale: str, path: str, reason: str | None = None) -> str:
    """Build redirect URL back to the website. Falls back to EN if locale not valid."""
    lang = locale if locale in ("en", "de") else "en"
    base = PUBLIC_WEBSITE_BASE_URL or ""
    qs = f"?reason={reason}" if reason else ""
    return f"{base}/{lang}/{path}{qs}"


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


@app.post("/api/subscribe")
@limiter.limit(RATE_LIMIT)
async def subscribe(payload: SubscribeRequest, request: Request):
    """
    Newsletter signup with Mautic double opt-in.

    Flow:
      1. Honeypot filled? -> 200 pending_confirmation, no Mautic call.
      2. Search Mautic for contact.
         - Mautic unreachable -> 503.
         - Already confirmed (tag 'newsletter_confirmed') -> 200 already_subscribed.
         - DNC on email channel -> 200 already_unsubscribed_no_email_sent.
      3. Sign a self-contained confirmation token (email+locale, 24h).
      4. Upsert contact with token, locale, source, consented_at, page_url
         + tag 'newsletter_pending'.
      5. Trigger Mautic transactional send of the locale-specific confirm email.
      6. 200 pending_confirmation.
    """
    email = payload.email.lower()

    # Honeypot -- FE already short-circuits; this is defence in depth.
    if payload.company_website:
        await log_action(request, email, "subscribe_honeypot")
        return JSONResponse({"status": "pending_confirmation"})

    if not _token_signer:
        await log_action(request, email, "subscribe_error", error_detail="token_secret_unset")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    confirm_email_id = _confirm_email_id(payload.locale)
    if not confirm_email_id:
        await log_action(
            request, email, "subscribe_error",
            error_detail=f"confirm_email_id_missing locale={payload.locale}",
        )
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    logger.info("SUBSCRIBE_START email=%s locale=%s source=%s", email, payload.locale, payload.source)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Phase 1: find existing contact (if any)
        try:
            status, contact = await _search_mautic_contact(client, email)
        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_MAUTIC_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if status != 200:
            logger.warning("SUBSCRIBE_SEARCH_FAILED email=%s status=%s", email, status)
            await log_action(request, email, "mautic_error", error_detail=f"search_status={status}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        # Phase 2: short-circuit on already-confirmed / DNC
        existing_id: str | None = None
        if contact:
            existing_id = str(contact["id"])
            if _contact_has_tag(contact, "newsletter_confirmed"):
                logger.info("SUBSCRIBE_ALREADY_CONFIRMED email=%s contact_id=%s", email, existing_id)
                await log_action(request, email, "subscribe_already_confirmed", contact_id=existing_id)
                return JSONResponse({"status": "already_subscribed"})
            if _contact_is_dnc(contact):
                logger.info("SUBSCRIBE_DNC_SKIP email=%s contact_id=%s", email, existing_id)
                await log_action(request, email, "subscribe_dnc_skip", contact_id=existing_id)
                return JSONResponse({"status": "already_unsubscribed_no_email_sent"})

        # Phase 3: sign token + upsert contact
        token = _token_signer.dumps({"email": email, "locale": payload.locale})
        fields = {
            "confirmation_token": token,
            "newsletter_locale": payload.locale,
            "newsletter_source": payload.source,
            "newsletter_consented_at": payload.consented_at,
            "newsletter_page_url": payload.page_url,
        }

        try:
            upsert_status, upsert_contact = await _upsert_contact(
                client,
                existing_id=existing_id,
                email=email,
                payload_fields=fields,
                add_tags=["newsletter_pending"],
            )
        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_UPSERT_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"upsert_httpx: {exc}")
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if not upsert_contact:
            logger.error("SUBSCRIBE_UPSERT_FAILED email=%s status=%s", email, upsert_status)
            await log_action(
                request, email, "subscribe_error",
                error_detail=f"upsert_status={upsert_status}",
            )
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        contact_id = str(upsert_contact.get("id") or existing_id or "")
        logger.info("SUBSCRIBE_UPSERT_OK email=%s contact_id=%s", email, contact_id)

        # Phase 4: send confirm email via Mautic
        try:
            send_status = await _send_mautic_email(client, confirm_email_id, contact_id)
        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_SEND_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(
                request, email, "subscribe_error",
                contact_id=contact_id, error_detail=f"send_httpx: {exc}",
            )
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if send_status not in (200, 201):
            logger.error(
                "SUBSCRIBE_SEND_FAILED email=%s contact_id=%s status=%s",
                email, contact_id, send_status,
            )
            await log_action(
                request, email, "subscribe_error",
                contact_id=contact_id, error_detail=f"send_status={send_status}",
            )
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

    await log_action(request, email, "subscribe_ok", contact_id=contact_id)
    return JSONResponse({"status": "pending_confirmation"})


@app.get("/api/confirm")
@limiter.limit(CONFIRM_RATE_LIMIT)
async def confirm(request: Request, token: str = Query(..., min_length=1)):
    """
    Double opt-in landing. Validates signed token, flips Mautic tags, and
    redirects the browser back to the website's confirmed or error page.
    The user navigates here by clicking the confirm link in the Mautic email,
    so response is always a 302 redirect (not JSON).
    """
    if not _token_signer or not PUBLIC_WEBSITE_BASE_URL:
        logger.error("CONFIRM_MISCONFIGURED signer=%s site=%s", bool(_token_signer), bool(PUBLIC_WEBSITE_BASE_URL))
        return RedirectResponse(
            _website_redirect("en", "newsletter/confirm-error", reason="server_misconfigured"),
            status_code=302,
        )

    try:
        data = _token_signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except SignatureExpired:
        await log_action(request, "", "confirm_expired")
        return RedirectResponse(
            _website_redirect("en", "newsletter/confirm-error", reason="invalid_or_expired"),
            status_code=302,
        )
    except BadSignature:
        await log_action(request, "", "confirm_invalid")
        return RedirectResponse(
            _website_redirect("en", "newsletter/confirm-error", reason="invalid_or_expired"),
            status_code=302,
        )

    email = (data.get("email") or "").lower()
    locale = data.get("locale") if data.get("locale") in ("en", "de") else "en"
    if not email:
        await log_action(request, "", "confirm_invalid", error_detail="no_email_in_token")
        return RedirectResponse(
            _website_redirect(locale, "newsletter/confirm-error", reason="invalid_or_expired"),
            status_code=302,
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            status, contact = await _search_mautic_contact(client, email)
        except httpx.RequestError as exc:
            logger.error("CONFIRM_MAUTIC_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "confirm_error", error_detail=f"search_httpx: {exc}")
            return RedirectResponse(
                _website_redirect(locale, "newsletter/confirm-error", reason="service_unavailable"),
                status_code=302,
            )

        if status != 200 or contact is None:
            await log_action(
                request, email, "confirm_contact_missing",
                error_detail=f"search_status={status}",
            )
            return RedirectResponse(
                _website_redirect(locale, "newsletter/confirm-error", reason="contact_missing"),
                status_code=302,
            )

        contact_id = str(contact["id"])

        try:
            upsert_status, _ = await _upsert_contact(
                client,
                existing_id=contact_id,
                email=email,
                payload_fields={"confirmation_token": ""},
                add_tags=["newsletter_confirmed"],
                remove_tags=["newsletter_pending"],
            )
        except httpx.RequestError as exc:
            logger.error("CONFIRM_UPSERT_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(
                request, email, "confirm_error",
                contact_id=contact_id, error_detail=f"upsert_httpx: {exc}",
            )
            return RedirectResponse(
                _website_redirect(locale, "newsletter/confirm-error", reason="service_unavailable"),
                status_code=302,
            )

        if upsert_status not in (200, 201):
            await log_action(
                request, email, "confirm_error",
                contact_id=contact_id, error_detail=f"upsert_status={upsert_status}",
            )
            return RedirectResponse(
                _website_redirect(locale, "newsletter/confirm-error", reason="service_unavailable"),
                status_code=302,
            )

    await log_action(request, email, "confirm_ok", contact_id=contact_id)
    return RedirectResponse(
        _website_redirect(locale, "newsletter/confirmed"),
        status_code=302,
    )


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

    auth_header = request.headers.get("authorization", "")
    if auth_header != f"Bearer {ADMIN_API_KEY}":
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
