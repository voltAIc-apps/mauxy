"""
mauxy -- centralized newsletter (un)subscribe authority for Mautic.

Mauxy is the ONLY newsletter (un)subscribe entry point for all sites (ludo,
flywheel, …). It manages Mautic contacts, segments and DNC lists via Basic Auth
-- Mautic credentials never reach the browser. Double-opt-in + sending live in
Mautic.

Two protections sit in front of every (un)subscribe:
  1. Per-site API key (Bearer). Each calling site is configured in MAUXY_SITES
     with its own key, target Mautic segment and quiz topic. Sent over HTTPS/TLS
     (the MITM defence). For server-side callers (ludo) the key is a real secret;
     for static sites it is a per-site identifier backed by the anti-bot layers.
  2. Bot-defence quiz. GET /api/challenge returns a multiple-choice question for
     the site's topic plus a signed, expiring token; (un)subscribe must echo back
     the token and the correct answer index, or it is rejected (409). A honeypot
     field (`company_website`) silently drops bots.
"""
import os
import time
import json
import hmac
import random
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
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

# Per-site registry. MAUXY_SITES is a JSON array, one object per calling site:
#   [{"key": "<secret>", "site": "ludo", "segment": "ludo", "topic": "odoo"}, …]
# The Bearer key identifies the site; its segment + quiz topic come from here, NOT
# from the request body (so a caller can never target another site's segment).
_SITES_BY_KEY: dict[str, dict] = {}
try:
    for _s in json.loads(os.environ.get("MAUXY_SITES", "[]")):
        if _s.get("key"):
            _SITES_BY_KEY[_s["key"]] = {
                "site": _s.get("site", "unknown"),
                "segment": _s.get("segment", ""),
                "topic": _s.get("topic", ""),
            }
except json.JSONDecodeError as _e:
    logger.error("MAUXY_SITES is not valid JSON -- all sites disabled: %s", _e)

# Secret for signing bot-defence challenge tokens (stateless HMAC). Required for
# the quiz to function; if unset, challenges cannot be issued or verified.
CHALLENGE_SECRET = os.environ.get("CHALLENGE_SECRET", "")
CHALLENGE_TTL = int(os.environ.get("CHALLENGE_TTL", "600"))  # seconds (10 min)

# Bot-defence question bank, keyed by topic. Shipped as quiz_bank.json next to this
# module; per-topic so each site asks audience-appropriate questions.
QUIZ_BANK: dict[str, list] = {}
try:
    _bank_path = Path(__file__).with_name("quiz_bank.json")
    QUIZ_BANK = {k: v for k, v in json.loads(_bank_path.read_text()).items() if isinstance(v, list)}
except Exception as _e:  # missing/broken bank → no quizzes (challenges 503)
    logger.error("quiz_bank.json could not be loaded: %s", _e)

# CORS origins (comma-separated)
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# -- Startup validation -------------------------------------------------------
if not MAUTIC_BASE_URL:
    logger.warning("MAUTIC_BASE_URL is not set -- all Mautic requests will fail")
if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS is not set -- CORS will block all browser requests")
if not _SITES_BY_KEY:
    logger.warning("MAUXY_SITES is empty -- every subscribe/unsubscribe will 401")
if not CHALLENGE_SECRET:
    logger.warning("CHALLENGE_SECRET is not set -- the bot-defence quiz is disabled")

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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_email ON action_log(email)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_result ON action_log(result)")
    # Idempotent column migrations (SQLite has no ADD COLUMN IF NOT EXISTS). `action`
    # distinguishes subscribe|unsubscribe; `site` records the calling site.
    cur = await db.execute("PRAGMA table_info(action_log)")
    cols = [row[1] for row in await cur.fetchall()]
    if "action" not in cols:
        await db.execute("ALTER TABLE action_log ADD COLUMN action TEXT NOT NULL DEFAULT 'unsubscribe'")
    if "site" not in cols:
        await db.execute("ALTER TABLE action_log ADD COLUMN site TEXT NOT NULL DEFAULT ''")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_action ON action_log(action)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_action_log_site ON action_log(site)")
    await db.commit()
    app.state.db = db
    logger.info("ACTION_LOG_DB opened: %s", ACTION_LOG_DB)
    yield
    await db.close()
    logger.info("ACTION_LOG_DB closed")


# -- App setup --------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Mauxy", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# -- Auth + quiz helpers -----------------------------------------------------
def _site_for_request(request: Request) -> Optional[dict]:
    """Resolve the calling site from its Bearer key, or None if unknown/missing."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    return _SITES_BY_KEY.get(header[len("Bearer "):])


def _sign_challenge(topic: str, qid: str, exp: int) -> str:
    """Stateless challenge token: `topic.qid.exp.sig`. The signature authenticates
    the (topic, qid, exp) tuple so a client cannot swap in an easier question or
    extend the expiry. The correct answer is NEVER in the token -- it is looked up
    server-side from QUIZ_BANK by (topic, qid) at verify time."""
    body = f"{topic}.{qid}.{exp}"
    sig = hmac.new(CHALLENGE_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_challenge(token: str, answer: int) -> bool:
    """True iff the token is well-signed, unexpired, and `answer` is the correct
    choice index for its (topic, qid) per the server-side quiz bank."""
    if not CHALLENGE_SECRET or not token:
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    topic, qid, exp_s, sig = parts
    expected = _sign_challenge(topic, qid, int(exp_s)) if exp_s.isdigit() else ""
    if not expected or not hmac.compare_digest(expected, token):
        return False
    if int(exp_s) < int(time.time()):
        return False
    for q in QUIZ_BANK.get(topic, []):
        if q.get("id") == qid:
            return int(answer) == int(q.get("answer", -1))
    return False


# -- Helpers ----------------------------------------------------------------
async def log_action(
    request: Request,
    email: str,
    result: str,
    contact_id: Optional[str] = None,
    error_detail: Optional[str] = None,
    action: str = "unsubscribe",
    site: str = "",
):
    origin = request.headers.get("origin", "")
    ip = request.client.host if request.client else ""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        db: aiosqlite.Connection = request.app.state.db
        await db.execute(
            """INSERT INTO action_log
               (ts, email, source_origin, source_ip, result, contact_id, error_detail, action, site)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, email, origin, ip, result, contact_id, error_detail, action, site),
        )
        await db.commit()
    except Exception as exc:
        logger.error("ACTION_LOG_WRITE_ERROR error=%s", exc)


# -- Models -----------------------------------------------------------------
class SubscribeRequest(BaseModel):
    email: EmailStr
    challenge: str = ""           # signed token from GET /api/challenge
    answer: int = -1              # chosen choice index
    company_website: str = ""     # honeypot — must stay empty
    # Accepted from richer clients (e.g. flywheel) but advisory only; the segment
    # comes from the site key, never the body.
    source: str = ""
    consent: bool = False
    consentedAt: str = ""
    pageUrl: str = ""
    locale: str = "de"


class UnsubscribeRequest(BaseModel):
    email: EmailStr
    challenge: str = ""
    answer: int = -1
    company_website: str = ""     # honeypot


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
            logger.warning("HEALTH_MAUTIC_HTTP_ERROR status=%s", resp.status_code)
            _mautic_health = {"ok": False, "checked_at": now, "detail": f"HTTP {resp.status_code}"}
    except httpx.RequestError as exc:
        logger.error("HEALTH_MAUTIC_CONNECT_ERROR error=%s", exc)
        _mautic_health = {"ok": False, "checked_at": now, "detail": f"connection error: {exc.__class__.__name__}"}
    except Exception as exc:
        logger.error("HEALTH_MAUTIC_UNEXPECTED_ERROR error=%s", exc)
        _mautic_health = {"ok": False, "checked_at": now, "detail": f"unexpected: {exc.__class__.__name__}"}

    return _mautic_health


async def _resolve_segment_id(client: httpx.AsyncClient, alias: str, auth) -> tuple:
    """Resolve a Mautic segment by alias, creating it if absent.
    Returns (segment_id, None) on success or (None, error_detail) on failure."""
    resp = await client.get(f"{MAUTIC_BASE_URL}/api/segments", params={"limit": 200}, auth=auth)
    if resp.status_code != 200:
        return None, f"segments_list_status={resp.status_code}"
    lists = resp.json().get("lists", {})
    items = lists.values() if isinstance(lists, dict) else (lists or [])
    for item in items:
        if (item.get("alias") or "").lower() == alias.lower():
            return str(item.get("id")), None
    created = await client.post(
        f"{MAUTIC_BASE_URL}/api/segments/new",
        json={"name": alias, "alias": alias, "isPublished": True},
        auth=auth,
    )
    if created.status_code in (200, 201):
        new_id = created.json().get("list", {}).get("id")
        logger.info("SEGMENT_CREATED alias=%s id=%s", alias, new_id)
        return str(new_id), None
    return None, f"segment_create_status={created.status_code}"


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


@app.get("/api/challenge")
@limiter.limit("30/minute")
async def challenge(request: Request):
    """Issue a bot-defence question for the calling site's topic + a signed token.
    The answer index is never returned; it is verified server-side on (un)subscribe."""
    site = _site_for_request(request)
    if site is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not CHALLENGE_SECRET:
        return JSONResponse({"error": "quiz disabled"}, status_code=503)
    questions = QUIZ_BANK.get(site["topic"] or "", [])
    if not questions:
        logger.error("CHALLENGE_NO_TOPIC site=%s topic=%s", site["site"], site["topic"])
        return JSONResponse({"error": "no quiz configured"}, status_code=503)
    q = random.choice(questions)
    exp = int(time.time()) + CHALLENGE_TTL
    return {
        "challenge": _sign_challenge(site["topic"], q["id"], exp),
        "question": q["q"],
        "choices": q["choices"],
    }


@app.post("/api/subscribe")
@limiter.limit("60/minute")
async def subscribe(payload: SubscribeRequest, request: Request):
    """
    Add an email to the calling site's Mautic segment (find/create the contact,
    then add it to the segment). Mautic runs the double-opt-in campaign, so a fresh
    signup returns {"status": "pending_confirmation"}.

    Requires a valid per-site Bearer key and a correct bot-defence answer.
    """
    site = _site_for_request(request)
    if site is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    email = payload.email.lower()
    # Honeypot: a real user never fills `company_website`. Accept-and-drop so the bot
    # gets a success-looking response but nothing reaches Mautic.
    if payload.company_website.strip():
        logger.info("SUBSCRIBE_HONEYPOT site=%s email=%s", site["site"], email)
        await log_action(request, email, "honeypot", action="subscribe", site=site["site"])
        return JSONResponse({"status": "pending_confirmation"})

    if not _verify_challenge(payload.challenge, payload.answer):
        await log_action(request, email, "quiz_failed", action="subscribe", site=site["site"])
        return JSONResponse({"status": "quiz_failed"}, status_code=409)

    segment = site["segment"]
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    logger.info("SUBSCRIBE_START site=%s email=%s segment=%s", site["site"], email, segment)

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            search = await client.get(
                f"{MAUTIC_BASE_URL}/api/contacts",
                params={"where[0][col]": "email", "where[0][expr]": "eq", "where[0][val]": email},
                auth=auth,
            )
        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_MAUTIC_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}", action="subscribe", site=site["site"])
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if search.status_code != 200:
            logger.warning("SUBSCRIBE_SEARCH_FAILED email=%s status=%s", email, search.status_code)
            await log_action(request, email, "mautic_error", error_detail=f"search_status={search.status_code}", action="subscribe", site=site["site"])
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        try:
            contact_id = None
            for cid, cdata in (search.json().get("contacts", {}) or {}).items():
                cemail = (cdata.get("fields", {}).get("core", {}).get("email", {}).get("value") or "").lower()
                if cemail == email:
                    contact_id = cid
                    break

            if contact_id is None:
                created = await client.post(
                    f"{MAUTIC_BASE_URL}/api/contacts/new", json={"email": email}, auth=auth)
                if created.status_code not in (200, 201):
                    logger.error("SUBSCRIBE_CREATE_FAILED email=%s status=%s", email, created.status_code)
                    await log_action(request, email, "error", error_detail=f"contact_create_status={created.status_code}", action="subscribe", site=site["site"])
                    return JSONResponse({"status": "error"}, status_code=502)
                contact_id = str(created.json().get("contact", {}).get("id"))
                logger.info("SUBSCRIBE_CONTACT_CREATED email=%s contact_id=%s", email, contact_id)

            segment_id, seg_err = await _resolve_segment_id(client, segment, auth)
            if segment_id is None:
                logger.error("SUBSCRIBE_SEGMENT_UNRESOLVED email=%s segment=%s err=%s", email, segment, seg_err)
                await log_action(request, email, "error", contact_id=str(contact_id), error_detail=seg_err, action="subscribe", site=site["site"])
                return JSONResponse({"status": "error"}, status_code=502)

            add = await client.post(
                f"{MAUTIC_BASE_URL}/api/segments/{segment_id}/contact/{contact_id}/add", auth=auth)
            if add.status_code in (200, 201):
                logger.info("SUBSCRIBE_OK email=%s contact_id=%s segment_id=%s", email, contact_id, segment_id)
                await log_action(request, email, "ok", contact_id=str(contact_id), action="subscribe", site=site["site"])
                return JSONResponse({"status": "pending_confirmation"})

            logger.error("SUBSCRIBE_ADD_FAILED email=%s status=%s", email, add.status_code)
            await log_action(request, email, "error", contact_id=str(contact_id), error_detail=f"segment_add_status={add.status_code}", action="subscribe", site=site["site"])
            return JSONResponse({"status": "error"}, status_code=502)

        except httpx.RequestError as exc:
            logger.error("SUBSCRIBE_REQUEST_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}", action="subscribe", site=site["site"])
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
        except Exception as exc:
            logger.error("SUBSCRIBE_UNEXPECTED_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "error", error_detail=f"unexpected: {exc}", action="subscribe", site=site["site"])
            return JSONResponse({"status": "error"}, status_code=500)


@app.post("/api/unsubscribe")
@limiter.limit(RATE_LIMIT)
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """
    Add an email to the Mautic DNC list. Requires a valid per-site Bearer key and a
    correct bot-defence answer. Contact-specific outcomes return 200 {"status":"ok"}
    (no enumeration leak); key/quiz failures return 401/409 (email-independent, so
    still no leak). 503 only when Mautic is unreachable.
    """
    site = _site_for_request(request)
    if site is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    email = payload.email.lower()
    if payload.company_website.strip():           # honeypot
        await log_action(request, email, "honeypot", action="unsubscribe", site=site["site"])
        return JSONResponse({"status": "ok"})
    if not _verify_challenge(payload.challenge, payload.answer):
        await log_action(request, email, "quiz_failed", action="unsubscribe", site=site["site"])
        return JSONResponse({"status": "quiz_failed"}, status_code=409)

    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    logger.info("UNSUBSCRIBE_START site=%s email=%s", site["site"], email)

    async with httpx.AsyncClient(timeout=15.0) as client:
        search_url = f"{MAUTIC_BASE_URL}/api/contacts"
        try:
            resp = await client.get(
                search_url,
                params={"where[0][col]": "email", "where[0][expr]": "eq", "where[0][val]": email},
                auth=auth,
            )
        except httpx.RequestError as exc:
            logger.error("UNSUBSCRIBE_MAUTIC_UNREACHABLE email=%s error=%s", email, exc)
            await log_action(request, email, "mautic_unreachable", error_detail=f"httpx_error: {exc}", site=site["site"])
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        if resp.status_code != 200:
            logger.warning("UNSUBSCRIBE_SEARCH_FAILED email=%s status=%s", email, resp.status_code)
            await log_action(request, email, "mautic_error", error_detail=f"search_status={resp.status_code}", site=site["site"])
            return JSONResponse({"status": "service_unavailable"}, status_code=503)

        try:
            contacts = resp.json().get("contacts", {})
            if not contacts:
                await log_action(request, email, "not_found", site=site["site"])
                return JSONResponse({"status": "ok"})

            contact_id = None
            for cid, cdata in contacts.items():
                cemail = (cdata.get("fields", {}).get("core", {}).get("email", {}).get("value") or "").lower()
                if cemail == email:
                    contact_id = cid
                    break

            if contact_id is None:
                await log_action(request, email, "not_found", error_detail="no_exact_match", site=site["site"])
                return JSONResponse({"status": "ok"})

            logger.info("UNSUBSCRIBE_CONTACT_FOUND email=%s contact_id=%s", email, contact_id)
            dnc_url = f"{MAUTIC_BASE_URL}/api/contacts/{contact_id}/dnc/email/add"
            dnc_ok = False
            for attempt in range(1, 3):
                dnc_resp = await client.post(
                    dnc_url, json={"reason": 1, "comments": "Unsubscribed via website"}, auth=auth)
                if dnc_resp.status_code in (200, 201):
                    logger.info("UNSUBSCRIBE_DNC_OK email=%s contact_id=%s", email, contact_id)
                    dnc_ok = True
                    break
                logger.warning("UNSUBSCRIBE_DNC_FAILED email=%s status=%s attempt=%d", email, dnc_resp.status_code, attempt)

            if dnc_ok:
                await log_action(request, email, "ok", contact_id=str(contact_id), site=site["site"])
            else:
                logger.error("UNSUBSCRIBE_DNC_FAILED_RETRY_EXHAUSTED email=%s contact_id=%s", email, contact_id)
                await log_action(request, email, "error", contact_id=str(contact_id), error_detail="dnc_retry_exhausted", site=site["site"])

        except httpx.RequestError as exc:
            logger.error("UNSUBSCRIBE_DNC_REQUEST_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "error", error_detail=f"httpx_error: {exc}", site=site["site"])
        except Exception as exc:
            logger.error("UNSUBSCRIBE_UNEXPECTED_ERROR email=%s error=%s", email, exc)
            await log_action(request, email, "error", error_detail=f"unexpected: {exc}", site=site["site"])

    return JSONResponse({"status": "ok"})


@app.get("/api/actions")
async def get_actions(
    request: Request,
    email: Optional[str] = Query(None),
    result: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Admin endpoint: query the action log. Requires Bearer ADMIN_API_KEY."""
    if not ADMIN_API_KEY:
        return JSONResponse({"error": "admin endpoint disabled"}, status_code=403)
    if request.headers.get("authorization", "") != f"Bearer {ADMIN_API_KEY}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    clauses, params = [], []
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
    return JSONResponse({"actions": [dict(r) for r in rows], "count": len(rows)})
