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
# Target Mautic version (manual). Persisted in SQLite and exposed via get_mautic_version()
# so future REST calls can branch on it. Empty = unknown (no version-specific branching).
MAUTIC_VERSION = os.environ.get("MAUTIC_VERSION", "")
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5/minute")
ACTION_LOG_DB = os.environ.get("ACTION_LOG_DB", "/data/actions.db")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

# Per-site registry + bot-defence question bank now live in SQLite (managed at runtime
# via the /api/admin/* CRUD endpoints — no rebuild/redeploy to onboard a site or edit a
# quiz). Both caches below are EMPTY at import and filled from the DB at startup by
# reload_config(); the MAUXY_SITES env var and quiz_bank.json are kept ONLY as a one-time
# seed source for an empty DB (see _seed_if_empty).
#
# A site record: {"site": ..., "segment": ..., "topic": ...} keyed by its Bearer key.
# The key identifies the site; its segment + quiz topic come from here, NOT from the
# request body (so a caller can never target another site's segment).
_SITES_BY_KEY: dict[str, dict] = {}
# Question bank keyed by topic: {topic: [{"id", "q", "choices", "answer"}, ...]}.
QUIZ_BANK: dict[str, list] = {}

# Secret for signing bot-defence challenge tokens (stateless HMAC). Required for
# the quiz to function; if unset, challenges cannot be issued or verified.
CHALLENGE_SECRET = os.environ.get("CHALLENGE_SECRET", "")
CHALLENGE_TTL = int(os.environ.get("CHALLENGE_TTL", "600"))  # seconds (10 min)


def _now() -> str:
    """UTC timestamp as ISO-8601 string (shared by log + config writers)."""
    return datetime.now(timezone.utc).isoformat()


def _parse_seed_sites() -> list[dict]:
    """Parse MAUXY_SITES (JSON array) into site dicts. Seed source for an empty DB only."""
    sites: list[dict] = []
    try:
        for _s in json.loads(os.environ.get("MAUXY_SITES", "[]")):
            if _s.get("key"):
                sites.append({
                    "key": _s["key"],
                    "site": _s.get("site", "unknown"),
                    "segment": _s.get("segment", ""),
                    "topic": _s.get("topic", ""),
                })
    except json.JSONDecodeError as _e:
        logger.error("MAUXY_SITES is not valid JSON -- cannot seed sites: %s", _e)
    return sites


def _parse_seed_quiz() -> dict:
    """Parse quiz_bank.json into {topic: [questions]}. Seed source for an empty DB only."""
    try:
        _bank_path = Path(__file__).with_name("quiz_bank.json")
        return {k: v for k, v in json.loads(_bank_path.read_text()).items() if isinstance(v, list)}
    except Exception as _e:  # missing/broken bank → nothing to seed
        logger.error("quiz_bank.json could not be loaded for seeding: %s", _e)
        return {}

# CORS origins (comma-separated)
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# -- Startup validation -------------------------------------------------------
if not MAUTIC_BASE_URL:
    logger.warning("MAUTIC_BASE_URL is not set -- all Mautic requests will fail")
if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS is not set -- CORS will block all browser requests")
if not CHALLENGE_SECRET:
    logger.warning("CHALLENGE_SECRET is not set -- the bot-defence quiz is disabled")

# -- Mautic health check cache -----------------------------------------------
_mautic_health = {"ok": True, "checked_at": 0.0, "detail": "pending"}
HEALTH_CHECK_CACHE_TTL = 30
HEALTH_CHECK_TIMEOUT = 5.0

# Cached identity of the target Mautic instance (the single mautic_instance row). The
# version here is the one branch point for future version-specific REST calls.
_mautic_instance = {"base_url": "", "version": "", "source": ""}


def get_mautic_version() -> str:
    """Single resolver for the active Mautic version. Empty string = unknown. Future
    version-specific call sites branch on this; today nothing branches (no behavior
    change while the version is unknown)."""
    return _mautic_instance.get("version", "")


# -- Runtime config: load / seed / refresh from SQLite -----------------------
async def reload_config(db: aiosqlite.Connection):
    """Reload the in-memory site registry + quiz bank from SQLite. Called at startup and
    after every admin write so the request path always sees current config. Preserves the
    legacy lookup shapes so _site_for_request / _verify_challenge / challenge are unchanged."""
    global _SITES_BY_KEY, QUIZ_BANK
    sites: dict[str, dict] = {}
    async with db.execute("SELECT key, site, segment, topic FROM sites") as cur:
        async for row in cur:
            sites[row[0]] = {"site": row[1], "segment": row[2], "topic": row[3]}
    bank: dict[str, list] = {}
    async with db.execute("SELECT topic, qid, question, choices_json, answer FROM quiz_questions") as cur:
        async for row in cur:
            topic, qid, question, choices_json, answer = row
            try:
                choices = json.loads(choices_json)
            except (json.JSONDecodeError, TypeError):
                logger.error("QUIZ_BAD_CHOICES topic=%s qid=%s -- skipped", topic, qid)
                continue
            bank.setdefault(topic, []).append(
                {"id": qid, "q": question, "choices": choices, "answer": answer})
    _SITES_BY_KEY = sites
    QUIZ_BANK = bank


async def _seed_if_empty(db: aiosqlite.Connection):
    """One-time seed for an empty DB: sites from MAUXY_SITES, quiz from quiz_bank.json.
    Skipped once the tables hold rows (DB is then authoritative)."""
    cur = await db.execute("SELECT COUNT(*) FROM sites")
    if (await cur.fetchone())[0] == 0:
        seed_sites = _parse_seed_sites()
        for s in seed_sites:
            await db.execute(
                "INSERT OR IGNORE INTO sites (key, site, segment, topic, created_at) VALUES (?,?,?,?,?)",
                (s["key"], s["site"], s["segment"], s["topic"], _now()),
            )
        if seed_sites:
            logger.info("SEED_SITES_FROM_ENV count=%d", len(seed_sites))

    cur = await db.execute("SELECT COUNT(*) FROM quiz_questions")
    if (await cur.fetchone())[0] == 0:
        seeded = 0
        for topic, questions in _parse_seed_quiz().items():
            for q in questions:
                await db.execute(
                    "INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES (?,?,?,?,?)",
                    (topic, q.get("id"), q.get("q"), json.dumps(q.get("choices", [])), int(q.get("answer", -1))),
                )
                seeded += 1
        if seeded:
            logger.info("SEED_QUIZ_FROM_FILE count=%d", seeded)
    await db.commit()


async def _refresh_mautic_instance(db: aiosqlite.Connection):
    """Refresh the in-memory Mautic instance cache from the single SQLite row."""
    global _mautic_instance
    cur = await db.execute("SELECT base_url, version, source FROM mautic_instance WHERE id = 1")
    row = await cur.fetchone()
    if row:
        _mautic_instance = {"base_url": row[0] or "", "version": row[1] or "", "source": row[2] or ""}


async def _init_mautic_instance(db: aiosqlite.Connection):
    """Persist the target Mautic instance (manual). MAUTIC_VERSION env (when set) is
    authoritative and upserts the row; when unset, an existing admin-set value is left
    intact, and a missing row is created with an unknown version."""
    if MAUTIC_VERSION:
        await db.execute(
            """INSERT INTO mautic_instance (id, base_url, version, source, updated_at)
               VALUES (1, ?, ?, 'manual', ?)
               ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url,
                   version=excluded.version, source='manual', updated_at=excluded.updated_at""",
            (MAUTIC_BASE_URL, MAUTIC_VERSION, _now()),
        )
        await db.commit()
    else:
        cur = await db.execute("SELECT 1 FROM mautic_instance WHERE id = 1")
        if await cur.fetchone() is None:
            await db.execute(
                "INSERT INTO mautic_instance (id, base_url, version, source, updated_at) VALUES (1, ?, '', 'unset', ?)",
                (MAUTIC_BASE_URL, _now()),
            )
            await db.commit()
    await _refresh_mautic_instance(db)


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

    # Runtime config tables (created alongside action_log). sites holds the per-site
    # Bearer key + segment + quiz topic; quiz_questions the bot-defence bank; the single
    # mautic_instance row the target Mautic identity + version.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            key        TEXT PRIMARY KEY,
            site       TEXT NOT NULL,
            segment    TEXT NOT NULL,
            topic      TEXT NOT NULL,
            created_at TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS quiz_questions (
            topic        TEXT    NOT NULL,
            qid          TEXT    NOT NULL,
            question     TEXT    NOT NULL,
            choices_json TEXT    NOT NULL,
            answer       INTEGER NOT NULL,
            PRIMARY KEY (topic, qid)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mautic_instance (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            base_url   TEXT,
            version    TEXT,
            source     TEXT,
            updated_at TEXT
        )
    """)
    await db.commit()

    # First-boot seed from env/file, then load everything into the in-memory caches.
    await _seed_if_empty(db)
    await _init_mautic_instance(db)
    await reload_config(db)
    app.state.db = db
    logger.info("ACTION_LOG_DB opened: %s sites=%d topics=%d mautic_version=%s",
                ACTION_LOG_DB, len(_SITES_BY_KEY), len(QUIZ_BANK), get_mautic_version() or "unknown")
    if not _SITES_BY_KEY:
        logger.warning("No sites configured (sites table empty) -- every subscribe/unsubscribe will 401")
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


def _require_admin(request: Request) -> Optional[JSONResponse]:
    """Gate admin endpoints behind Bearer ADMIN_API_KEY. Returns an error response if the
    endpoint is disabled (no key set) or the caller is unauthorized, else None. Shared by
    /api/actions and all /api/admin/* routes (constant-time key compare)."""
    if not ADMIN_API_KEY:
        return JSONResponse({"error": "admin endpoint disabled"}, status_code=403)
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {ADMIN_API_KEY}"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


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


# -- Admin CRUD models ------------------------------------------------------
class SiteCreate(BaseModel):
    key: str                      # per-site Bearer key (stored plaintext in SQLite)
    site: str
    segment: str
    topic: str


class SiteUpdate(BaseModel):      # all optional — only provided fields are changed
    site: Optional[str] = None
    segment: Optional[str] = None
    topic: Optional[str] = None


class QuizQuestionModel(BaseModel):
    topic: str
    id: str
    q: str
    choices: list[str]
    answer: int                   # 0-based index into choices


class MauticInstanceUpdate(BaseModel):
    version: str
    base_url: Optional[str] = None   # defaults to MAUTIC_BASE_URL when omitted


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
        "mautic_version": get_mautic_version() or "unknown",
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
    if (err := _require_admin(request)) is not None:
        return err

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


# -- Admin: site registry CRUD ----------------------------------------------
# All require Bearer ADMIN_API_KEY. Every write calls reload_config() so the change is
# live on the next request without a restart.
@app.get("/api/admin/sites")
async def admin_list_sites(request: Request):
    """List all configured sites (incl. their Bearer keys — admin already holds the key)."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        "SELECT key, site, segment, topic, created_at FROM sites ORDER BY site")
    return JSONResponse({"sites": [dict(r) for r in rows], "count": len(rows)})


@app.post("/api/admin/sites")
async def admin_create_site(payload: SiteCreate, request: Request):
    """Onboard a site. 409 if the key already exists."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    try:
        await db.execute(
            "INSERT INTO sites (key, site, segment, topic, created_at) VALUES (?,?,?,?,?)",
            (payload.key, payload.site, payload.segment, payload.topic, _now()),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        return JSONResponse({"error": "site key already exists"}, status_code=409)
    await reload_config(db)
    logger.info("ADMIN_SITE_CREATED site=%s", payload.site)
    return JSONResponse({"status": "created", "site": payload.site}, status_code=201)


@app.put("/api/admin/sites/{key}")
async def admin_update_site(key: str, payload: SiteUpdate, request: Request):
    """Update a site's site/segment/topic by its key. The key itself is immutable."""
    if (err := _require_admin(request)) is not None:
        return err
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    sets = ", ".join(f"{col} = ?" for col in fields)   # cols are fixed model fields, not user input
    cur = await db.execute(f"UPDATE sites SET {sets} WHERE key = ?", [*fields.values(), key])
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "site not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/sites/{key}")
async def admin_delete_site(key: str, request: Request):
    """Remove a site by its key."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute("DELETE FROM sites WHERE key = ?", (key,))
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "site not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "deleted"})


# -- Admin: quiz question CRUD ----------------------------------------------
@app.get("/api/admin/quiz")
async def admin_list_quiz(request: Request, topic: Optional[str] = Query(None)):
    """List quiz questions, optionally filtered by topic."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    db.row_factory = aiosqlite.Row
    if topic:
        rows = await db.execute_fetchall(
            "SELECT topic, qid, question, choices_json, answer FROM quiz_questions WHERE topic = ? ORDER BY qid",
            (topic,))
    else:
        rows = await db.execute_fetchall(
            "SELECT topic, qid, question, choices_json, answer FROM quiz_questions ORDER BY topic, qid")
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["choices"] = json.loads(d.pop("choices_json"))
        except (json.JSONDecodeError, TypeError):
            d.pop("choices_json", None)
            d["choices"] = []
        out.append(d)
    return JSONResponse({"questions": out, "count": len(out)})


@app.post("/api/admin/quiz")
async def admin_create_quiz(payload: QuizQuestionModel, request: Request):
    """Add a quiz question. 409 if (topic, id) already exists; 400 if answer out of range."""
    if (err := _require_admin(request)) is not None:
        return err
    if not 0 <= payload.answer < len(payload.choices):
        return JSONResponse({"error": "answer index out of range"}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    try:
        await db.execute(
            "INSERT INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES (?,?,?,?,?)",
            (payload.topic, payload.id, payload.q, json.dumps(payload.choices), payload.answer),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        return JSONResponse({"error": "question (topic, id) already exists"}, status_code=409)
    await reload_config(db)
    return JSONResponse({"status": "created"}, status_code=201)


@app.put("/api/admin/quiz/{topic}/{qid}")
async def admin_update_quiz(topic: str, qid: str, payload: QuizQuestionModel, request: Request):
    """Replace a question's text/choices/answer. The (topic, qid) in the path locates it."""
    if (err := _require_admin(request)) is not None:
        return err
    if not 0 <= payload.answer < len(payload.choices):
        return JSONResponse({"error": "answer index out of range"}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute(
        "UPDATE quiz_questions SET question = ?, choices_json = ?, answer = ? WHERE topic = ? AND qid = ?",
        (payload.q, json.dumps(payload.choices), payload.answer, topic, qid),
    )
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "question not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/quiz/{topic}/{qid}")
async def admin_delete_quiz(topic: str, qid: str, request: Request):
    """Remove a quiz question by (topic, qid)."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute("DELETE FROM quiz_questions WHERE topic = ? AND qid = ?", (topic, qid))
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "question not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "deleted"})


# -- Admin: Mautic instance (version) ---------------------------------------
@app.get("/api/admin/mautic-instance")
async def admin_get_mautic_instance(request: Request):
    """Read the persisted target Mautic instance (base_url, version, source)."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        "SELECT base_url, version, source, updated_at FROM mautic_instance WHERE id = 1")
    return JSONResponse({"mautic_instance": dict(rows[0]) if rows else None})


@app.put("/api/admin/mautic-instance")
async def admin_set_mautic_instance(payload: MauticInstanceUpdate, request: Request):
    """Manually set the target Mautic version (and optionally base_url). Updates the
    resolver cache immediately so get_mautic_version() reflects the new value."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    base_url = payload.base_url if payload.base_url is not None else MAUTIC_BASE_URL
    await db.execute(
        """INSERT INTO mautic_instance (id, base_url, version, source, updated_at)
           VALUES (1, ?, ?, 'manual', ?)
           ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url,
               version=excluded.version, source='manual', updated_at=excluded.updated_at""",
        (base_url, payload.version, _now()),
    )
    await db.commit()
    await _refresh_mautic_instance(db)
    logger.info("ADMIN_MAUTIC_VERSION_SET version=%s", payload.version)
    return JSONResponse({"status": "updated", "version": payload.version})
