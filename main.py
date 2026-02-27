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
from typing import Optional

import aiosqlite
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import httpx
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
ACTION_LOG_DB = os.environ.get("ACTION_LOG_DB", "/data/actions.db")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

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
