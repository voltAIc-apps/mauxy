"""
mauxy -- the website's interface to other systems (owner decision 2026-11-08): public consent
service of simplify-erp.de for newsletter, Migration Radar, Selbst-Check and every lead CTA.

  newsletter   GET /api/challenge, POST /api/subscribe, GET+POST /api/confirm,
               POST /api/unsubscribe                                        (this file)
  admin        /api/actions, /api/admin/sites|quiz|mautic-instance          (this file)
  self-check   /api/selfcheck*                                              (selfcheck_api.py, SPEC-020)
  CTA leads    /api/lead*                                                   (leads_api.py, SPEC-021)
  login        /api/auth/linkedin/start|callback, /api/auth/session         (auth_api.py, sign-in only)

Confirmed data goes to sinks (sinks.py): crm.lead + Email Marketing lists in erp.wapsol (Odoo,
default) -- Mautic only as deprecated SUBSCRIBER_SINK=mautic. Credentials stay server-side.

Newsletter authority (upstream design, kept): mauxy is the ONLY newsletter (un)subscribe entry
point for all sites (simplify-erp, ludo, re-cloud, ...). Two gates sit in front of every
(un)subscribe:
  1. Per-site Bearer key. The `sites` table (seeded from MAUXY_SITES, admin CRUD) maps the key to
     site, segment and quiz topic; the segment never comes from the request body.
  2. Bot-defence quiz. GET /api/challenge returns a question for the site's topic plus a signed,
     expiring token; (un)subscribe must echo the token and the correct answer (else 409).
     Honeypot `company_website` silently drops bots.
Behind the gates mauxy owns the double opt-in (not Mautic): ALL pending data stays server-side in
SQLite, nothing is written to any sink before the person clicks "Bestätigen" on the confirmation
page (GET of the mail link is read-only: scanners prefetch links, confirm_page.py); confirmation
tokens are signed, time-limited and single-use (stored as SHA-256, compared in constant time).

Per site (owner requirement 2026-11-09, db-patches/0006): brand profile (brand.py: confirm /
neutral page, DOI mails, redirects, mail sender) and action routing (routing.py: ordered targets
per site and action, fan-out through the route_delivery outbox). Sites without a profile / routes
behave exactly as before.

Schema: versioned patches in db-patches/ (db_migrate.py), applied by the deployer
(`scripts/sqlite_db.py --migrate`, k8s initContainer); startup only checks the version.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import aiosqlite
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import auth_api
import brand
import confirm_page
import db_migrate  # schema patches live in db-patches/; this is the shared runner
import jobs
import lead_sync
import leads_api
import mailer
import routing
import selfcheck_api
import sinks
import webhooks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Config from env --------------------------------------------------------
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5/minute")  # unsubscribe (and subscribe fallback)
# subscribe per IP; upstream raised it to 60/minute for server-side callers (ludo) behind the
# quiz. Mail bombing of one address is capped separately by MAX_PENDING_PER_EMAIL.
SUBSCRIBE_RATE_LIMIT = os.environ.get("SUBSCRIBE_RATE_LIMIT", "60/minute")
CHALLENGE_RATE_LIMIT = os.environ.get("CHALLENGE_RATE_LIMIT", "30/minute")
CONFIRM_RATE_LIMIT = os.environ.get("CONFIRM_RATE_LIMIT", "30/minute")
ACTION_LOG_DB = os.environ.get("ACTION_LOG_DB", "/data/actions.db")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
# If true (default), refuse to start when the DB is behind the shipped schema patches.
MIGRATE_STRICT = os.environ.get("MIGRATE_STRICT", "true").lower() not in ("false", "0", "no")

# Mautic identity, only for the mautic_instance row / get_mautic_version() (upstream). Mautic
# itself is the DEPRECATED subscriber sink (mautic_sink.py reads its own credentials).
MAUTIC_BASE_URL = os.environ.get("MAUTIC_BASE_URL", "")
MAUTIC_VERSION = os.environ.get("MAUTIC_VERSION", "")

# Per-site registry + quiz bank live in SQLite (admin CRUD, no redeploy). Both caches are EMPTY
# at import and filled by reload_config() at startup and after every admin write.
# A site record: {"site", "segment", "topic"} keyed by its Bearer key.
_SITES_BY_KEY: dict[str, dict] = {}
# {topic: [{"id", "q", "choices", "answer"}, ...]}
QUIZ_BANK: dict[str, list] = {}
# HMAC secret for the stateless challenge tokens; quiz (and so every (un)subscribe) off if unset.
CHALLENGE_SECRET = os.environ.get("CHALLENGE_SECRET", "")
CHALLENGE_TTL = int(os.environ.get("CHALLENGE_TTL", "600"))  # seconds

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
if not CHALLENGE_SECRET:
    logger.warning("CHALLENGE_SECRET is not set -- the bot-defence quiz (and so newsletter (un)subscribe) is disabled")
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


def _require_admin(request: Request) -> Optional[JSONResponse]:
    """Admin gate for /api/actions and /api/admin/*: 403 when disabled (no key), 401 when wrong."""
    if not ADMIN_API_KEY:
        return JSONResponse({"error": "admin endpoint disabled"}, status_code=403)
    if not admin_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


# -- Runtime config (sites, quiz, Mautic instance) from SQLite ------------------
_mautic_instance = {"base_url": "", "version": "", "source": ""}


def get_mautic_version() -> str:
    """Single resolver for the target Mautic version ('' = unknown)."""
    return _mautic_instance.get("version", "")


def policy_error(subscriber_sink: Optional[str], doi_owner: str) -> Optional[str]:
    """Per-site policy check (seed, admin CRUD, request time). doi_owner 'sink' is only allowed
    with the Mautic sink: Odoo has no opt-in campaign, so an immediate write would put an
    unconfirmed address on a list."""
    if subscriber_sink not in (None, *sinks.SUBSCRIBER_KINDS):
        return f"unknown subscriber_sink {subscriber_sink!r}"
    if doi_owner not in ("mauxy", "sink"):
        return f"unknown doi_owner {doi_owner!r}"
    effective = subscriber_sink or sinks.default_subscriber_kind()
    if doi_owner == "sink" and effective != "mautic":
        return "doi_owner 'sink' needs subscriber_sink 'mautic'"
    return None


def _parse_seed_sites() -> list[dict]:
    """MAUXY_SITES (JSON array) -> site dicts. Seed source for an EMPTY sites table only.
    Optional per site: subscribe_quiz (bool, default true), subscriber_sink (odoo|mautic|none,
    default = global), doi_owner (mauxy|sink, default mauxy). Invalid entries are skipped."""
    sites: list[dict] = []
    try:
        for s in json.loads(os.environ.get("MAUXY_SITES", "[]")):
            if not s.get("key"):
                continue
            site = {"key": s["key"], "site": s.get("site", "unknown"), "segment": s.get("segment", ""),
                    "topic": s.get("topic", ""), "subscribe_quiz": 0 if s.get("subscribe_quiz") is False else 1,
                    "subscriber_sink": s.get("subscriber_sink") or None, "doi_owner": s.get("doi_owner") or "mauxy"}
            if err := policy_error(site["subscriber_sink"], site["doi_owner"]):
                logger.error("MAUXY_SITES entry for site=%s skipped: %s", site["site"], err)
                continue
            sites.append(site)
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        logger.error("MAUXY_SITES is not a valid JSON array -- cannot seed sites: %s", exc.__class__.__name__)
    return sites


async def reload_config(db: aiosqlite.Connection) -> None:
    """Reload the in-memory site registry + quiz bank from SQLite (startup, every admin write)."""
    global _SITES_BY_KEY, QUIZ_BANK
    sites: dict[str, dict] = {}
    async with db.execute(
            "SELECT key, site, segment, topic, subscribe_quiz, subscriber_sink, doi_owner FROM sites") as cur:
        async for row in cur:
            sites[row[0]] = {"site": row[1], "segment": row[2], "topic": row[3], "subscribe_quiz": bool(row[4]),
                             "sink": row[5] or None, "doi_owner": row[6]}
    bank: dict[str, list] = {}
    async with db.execute("SELECT topic, qid, question, choices_json, answer FROM quiz_questions") as cur:
        async for row in cur:
            topic, qid, question, choices_json, answer = tuple(row)
            try:
                choices = json.loads(choices_json)
            except (json.JSONDecodeError, TypeError):
                logger.error("QUIZ_BAD_CHOICES topic=%s qid=%s -- skipped", topic, qid)
                continue
            bank.setdefault(topic, []).append({"id": qid, "q": question, "choices": choices, "answer": answer})
    _SITES_BY_KEY = sites
    QUIZ_BANK = bank
    await brand.load(db)    # per-site brand profiles (db-patches/0006)
    await routing.load(db)  # per-site action routing (db-patches/0006)


async def _seed_if_empty(db: aiosqlite.Connection) -> None:
    """One-time seed of `sites` from MAUXY_SITES (secret keys, never in a committed patch)."""
    async with db.execute("SELECT COUNT(*) FROM sites") as cur:
        (count,) = await cur.fetchone()
    if count == 0:
        seed = _parse_seed_sites()
        for s in seed:
            await db.execute(
                "INSERT OR IGNORE INTO sites (key, site, segment, topic, subscribe_quiz, subscriber_sink, doi_owner, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (s["key"], s["site"], s["segment"], s["topic"], s["subscribe_quiz"], s["subscriber_sink"],
                 s["doi_owner"], now_iso()))
        if seed:
            logger.info("SEED_SITES_FROM_ENV count=%d", len(seed))
    await db.commit()


async def _refresh_mautic_instance(db: aiosqlite.Connection) -> None:
    global _mautic_instance
    async with db.execute("SELECT base_url, version, source FROM mautic_instance WHERE id = 1") as cur:
        row = await cur.fetchone()
    if row:
        _mautic_instance = {"base_url": row[0] or "", "version": row[1] or "", "source": row[2] or ""}


async def _init_mautic_instance(db: aiosqlite.Connection) -> None:
    """MAUTIC_VERSION env (when set) upserts the single row; else an admin-set value is kept."""
    if MAUTIC_VERSION:
        await db.execute(
            """INSERT INTO mautic_instance (id, base_url, version, source, updated_at)
               VALUES (1, ?, ?, 'manual', ?)
               ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url,
                   version=excluded.version, source='manual', updated_at=excluded.updated_at""",
            (MAUTIC_BASE_URL, MAUTIC_VERSION, now_iso()))
    else:
        await db.execute(
            "INSERT OR IGNORE INTO mautic_instance (id, base_url, version, source, updated_at) "
            "VALUES (1, ?, '', 'unset', ?)", (MAUTIC_BASE_URL, now_iso()))
    await db.commit()
    await _refresh_mautic_instance(db)


def _site_fallback(site_name: str) -> Optional[str]:
    """Pinned subscriber sink of a site name (sites.subscriber_sink), None = global SUBSCRIBER_SINK."""
    return next((s["sink"] for s in _SITES_BY_KEY.values() if s["site"] == site_name and s.get("sink")), None)


routing.FALLBACK = _site_fallback  # the do-not-contact gate always checks the site's fallback sink


def _site_for_request(request: Request) -> Optional[dict]:
    """Calling site from its Bearer key (constant-time compare against every key), or None."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer "):]
    found = None
    for key, site in _SITES_BY_KEY.items():
        if hmac.compare_digest(key.encode(), presented.encode()):
            found = site
    return found


def _sign_challenge(topic: str, qid: str, exp: int) -> str:
    """Stateless token `topic.qid.exp.sig`; the answer is NEVER in it (looked up server-side)."""
    body = f"{topic}.{qid}.{exp}"
    sig = hmac.new(CHALLENGE_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_challenge(token: str, answer: int, topic: str) -> bool:
    """Well-signed, unexpired, issued for THIS site's topic, and `answer` is the right index."""
    if not CHALLENGE_SECRET or not token:
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    t_topic, qid, exp_s, _sig = parts
    if not exp_s.isdigit() or not hmac.compare_digest(_sign_challenge(t_topic, qid, int(exp_s)), token):
        return False
    if int(exp_s) < int(time.time()) or t_topic != topic:
        return False
    for q in QUIZ_BANK.get(t_topic, []):
        if q.get("id") == qid:
            return int(answer) == int(q.get("answer", -1))
    return False


# -- SQLite lifespan --------------------------------------------------------
async def _apply_patches_in_memory(db: aiosqlite.Connection) -> None:
    """ACTION_LOG_DB=':memory:' (tests, local runs): no deployer step can migrate a DB that only
    exists inside this process, so the committed patches are applied here, in order."""
    await db.execute(db_migrate.migrations_table_sql())
    for version, name, sql in db_migrate.patch_scripts():
        await db.executescript(sql)
        await db.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                         (version, name, now_iso()))
    await db.commit()


async def purge_newsletter_pending(db: aiosqlite.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWSLETTER_PENDING_DAYS)).isoformat(timespec="seconds")
    await db.execute("DELETE FROM newsletter_pending WHERE created_at < ?", (cutoff,))
    await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    in_memory = ACTION_LOG_DB == ":memory:"
    if not in_memory:
        # Schema is owned by db-patches/ and applied by the deployer (sqlite_db.py --migrate,
        # k8s initContainer). Here we only CHECK: a DB behind the shipped patches never serves.
        state = db_migrate.check(ACTION_LOG_DB)
        if state["pending"]:
            missing = ", ".join(f"{v:04d}_{n}" for v, n in state["pending"])
            msg = (f"DB schema behind shipped patches (current={state['current']} latest={state['latest']}); "
                   f"pending: {missing}. Run `python scripts/sqlite_db.py --migrate`.")
            if MIGRATE_STRICT:
                logger.error("MIGRATION_REQUIRED %s", msg)
                raise RuntimeError(msg)
            logger.warning("MIGRATION_PENDING (MIGRATE_STRICT=false) %s", msg)

    db = await aiosqlite.connect(ACTION_LOG_DB)
    db.row_factory = aiosqlite.Row
    if in_memory:
        await _apply_patches_in_memory(db)
    # first-boot seed of sites from MAUXY_SITES, then the in-memory caches
    await _seed_if_empty(db)
    await _init_mautic_instance(db)
    await reload_config(db)
    if not _SITES_BY_KEY:
        logger.warning("No sites configured (sites table empty) -- every newsletter (un)subscribe will 401")
    await purge_newsletter_pending(db)
    await selfcheck_api.purge(db)
    await leads_api.purge(db)
    await lead_sync.purge(db)
    await auth_api.purge(db)
    await routing.purge(db)
    mailer.bind(db)  # mail_outbox (idempotency, retries, per-recipient limit)
    await mailer.purge(db)
    app.state.db = db
    logger.info("ACTION_LOG_DB opened: %s sites=%d topics=%d mautic_version=%s",
                ACTION_LOG_DB, len(_SITES_BY_KEY), len(QUIZ_BANK), get_mautic_version() or "unknown")
    await lead_sync.resume(db)  # CRM deliveries interrupted by a restart
    await routing.resume(db)    # per-target fan-out deliveries interrupted by a restart
    await mailer.resume(jobs.spawn)  # mails that were queued when the process stopped
    auth_purger = asyncio.get_running_loop().create_task(auth_api.purger(db), name="auth_purger")
    yield
    auth_purger.cancel()
    # finish pending DOI mails before the DB closes; retries sleeping in their backoff are
    # cancelled (state stays in SQLite, resumed on the next start)
    await jobs.drain(timeout=float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "10")))
    mailer.bind(None)
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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


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
    """One action_log row; `action` = subscribe|confirm|unsubscribe, `site` = calling site."""
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


def page_url_allowed(url: str) -> bool:
    """page_url must belong to one of the allowed site origins (no foreign URLs stored)."""
    return any(url == o or url.startswith(o + "/") or url.startswith(o + "?") for o in ALLOWED_ORIGINS)


# -- Models -----------------------------------------------------------------
class UnsubscribeRequest(BaseModel):
    email: EmailStr
    challenge: str = Field(default="", max_length=400)  # signed token from GET /api/challenge
    answer: int = -1                                     # chosen choice index
    company_website: str | None = Field(default=None, max_length=200)  # honeypot


class LegacySubscribeRequest(BaseModel):
    """Upstream body for sites with doi_owner='sink' (ludo): only email + quiz are used; the other
    fields are accepted but advisory (the segment comes from the site key)."""
    email: EmailStr
    challenge: str = Field(default="", max_length=400)
    answer: int = -1
    company_website: str = Field(default="", max_length=200)  # honeypot
    source: str = Field(default="", max_length=200)
    consent: bool = False
    consentedAt: str = Field(default="", max_length=100)
    pageUrl: str = Field(default="", max_length=2000)
    locale: str = Field(default="de", max_length=10)


class SubscribeRequest(BaseModel):
    """mauxy-DOI sites: FE payload from website/src/config/api.ts (mauxySubscribe)."""
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
    # Bot-defence quiz (GET /api/challenge): signed token + chosen choice index.
    challenge: str = Field(default="", max_length=400)
    answer: int = -1

    @field_validator("page_url")
    @classmethod
    def _page_url_on_site(cls, v: str) -> str:
        if not page_url_allowed(v):
            raise ValueError("page_url must be on an allowed site origin")
        return v


class SiteCreate(BaseModel):
    key: str = Field(min_length=16, max_length=200)  # per-site Bearer key (stored plaintext, upstream decision)
    site: str = Field(min_length=1, max_length=80)
    segment: str = Field(default="", max_length=120)
    topic: str = Field(min_length=1, max_length=80)
    subscribe_quiz: bool = True                         # False: site relies on mauxy's DOI (subscribe only)
    subscriber_sink: Optional[Literal["odoo", "mautic", "none"]] = None  # None = global SUBSCRIBER_SINK
    doi_owner: Literal["mauxy", "sink"] = "mauxy"


class SiteUpdate(BaseModel):  # only provided fields change; the key is immutable
    site: Optional[str] = Field(default=None, min_length=1, max_length=80)
    segment: Optional[str] = Field(default=None, max_length=120)
    topic: Optional[str] = Field(default=None, min_length=1, max_length=80)
    subscribe_quiz: Optional[bool] = None
    subscriber_sink: Optional[Literal["odoo", "mautic", "none", ""]] = None  # "" = back to the global sink
    doi_owner: Optional[Literal["mauxy", "sink"]] = None


class QuizQuestionModel(BaseModel):
    topic: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")  # no '.' (token format)
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    q: str = Field(min_length=1, max_length=500)
    choices: list[str] = Field(min_length=2, max_length=8)
    answer: int  # 0-based index into choices


class MauticInstanceUpdate(BaseModel):
    version: str = Field(max_length=40)
    base_url: Optional[str] = Field(default=None, max_length=300)  # default MAUTIC_BASE_URL


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
    """Lead + subscriber sink reachability (global + sinks pinned by sites); cached HEALTH_CHECK_CACHE_TTL s."""
    global _sink_health
    now = time.monotonic()
    if now - _sink_health["checked_at"] >= HEALTH_CHECK_CACHE_TTL:
        kinds = tuple(s["sink"] for s in _SITES_BY_KEY.values() if s.get("sink"))
        _sink_health = {"checked_at": now, "detail": await sinks.health(kinds)}
    return _sink_health


def _nl_redirect(site: Optional[str], locale: str, outcome: str, reason: str | None = None) -> str:
    """Newsletter redirect after the click (brand.redirect_url: the site's explicit target >
    its website_url > PUBLIC_WEBSITE_BASE_URL). Invalid locale -> the site's default, else EN."""
    b = brand.get(site)
    lang = locale if locale in ("en", "de") else (b.default_locale if b else "en")
    path = f"/{lang}/newsletter/confirmed" if outcome == "confirmed" else f"/{lang}/newsletter/confirm-error"
    return brand.redirect_url(site, "newsletter", lang, outcome, env_base=PUBLIC_WEBSITE_BASE_URL, path=path,
                              params={"reason": reason} if reason else None)


PENDING = {"status": "pending_confirmation"}


@app.get("/api/challenge")
@limiter.limit(CHALLENGE_RATE_LIMIT)
async def challenge(request: Request):
    """Bot-defence question for the calling site's topic + signed token (upstream). The answer
    index never leaves the server; it is verified on (un)subscribe."""
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
    return {"challenge": _sign_challenge(site["topic"], q["id"], exp), "question": q["q"], "choices": q["choices"]}


@app.post("/api/subscribe")
@limiter.limit(SUBSCRIBE_RATE_LIMIT)
async def subscribe(request: Request):
    """
    Newsletter signup. The per-site policy (sites table, db-patches/0004) decides the flow:

      doi_owner 'mauxy' (simplify-erp, default for new sites): double opt-in by mauxy, body =
          SubscribeRequest, nothing is written to any sink here (below).
      doi_owner 'sink' (ludo, upstream behaviour unchanged): body = LegacySubscribeRequest, the
          contact goes into the site's Mautic segment at once, a Mautic campaign runs the opt-in
          (_subscribe_sink_doi).

    Gates first (all independent of the address, so no enumeration): per-site Bearer key (401),
    body validation (422, model per policy), honeypot (silent success), bot-defence quiz (409
    quiz_failed) unless the site has subscribe_quiz off (it relies on the DOI instead).

    Once the input is valid the answer is ALWAYS an immediate 200 {"status":
    "pending_confirmation"} -- same status, body and timing for every address (security
    review 2026-11-08: enumeration via response content, status code and timing). All
    contact-dependent work runs afterwards as a job (_subscribe_job): subscriber-sink lookup (read
    only: Odoo list / blacklist, or deprecated Mautic), skip if already subscribed / blocked /
    throttled, pending row in SQLite with the SHA-256 of a
    signed token ({"p": id}), DOI mail via SMTP. Failures there are logged, never returned.
    503 only for a misconfigured service (independent of the address).
    """
    site = _site_for_request(request)
    if site is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sink_doi = site["doi_owner"] == "sink"
    try:
        body = await request.json()
        payload = (LegacySubscribeRequest if sink_doi else SubscribeRequest).model_validate(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    except ValueError:  # not JSON
        raise RequestValidationError([{"type": "json_invalid", "loc": ("body",), "msg": "invalid JSON"}]) from None
    email = payload.email.lower()
    log = dict(action="subscribe", site=site["site"])
    if payload.company_website:  # honeypot
        await log_action(request, email, "subscribe_honeypot", **log)
        return JSONResponse(PENDING)
    if (site["subscribe_quiz"] or sink_doi) and not _verify_challenge(payload.challenge, payload.answer, site["topic"]):
        await log_action(request, email, "quiz_failed", **log)
        return JSONResponse({"status": "quiz_failed"}, status_code=409)
    if err := policy_error(site["sink"], site["doi_owner"]):
        logger.error("SITE_POLICY_INVALID site=%s %s", site["site"], err)
        await log_action(request, email, "subscribe_error", error_detail="site_policy", **log)
        return JSONResponse({"status": "service_unavailable"}, status_code=503)
    if sink_doi:
        return await _subscribe_sink_doi(request, email, site, log)
    if not _token_signer or not PUBLIC_BASE_URL or not mailer.configured():
        await log_action(request, email, "subscribe_error", error_detail="doi_not_configured", **log)
        return JSONResponse({"status": "service_unavailable"}, status_code=503)
    jobs.spawn(_subscribe_job(request, payload, email, site), name="subscribe")
    return JSONResponse(PENDING)


async def _subscribe_sink_doi(request: Request, email: str, site: dict, log: dict) -> JSONResponse:
    """Upstream newsletter-authority subscribe for doi_owner='sink' sites (ludo), same responses:
    200 pending_confirmation, 503 Mautic unreachable, 502 sub-step failed, 500 unexpected.
    Synchronous on purpose (the server-side caller retries on 503)."""
    import mautic_sink  # deprecated sink, loaded on demand

    result, contact_id, detail = await mautic_sink.add_to_segment_now(email, site["segment"] or site["site"])
    if result == "ok":
        await log_action(request, email, "ok", contact_id=contact_id, **log)
        return JSONResponse(PENDING)
    logger.error("SUBSCRIBE_SINK_DOI_FAILED site=%s result=%s detail=%s", site["site"], result, detail)
    await log_action(request, email, "mautic_unreachable" if result == "unreachable" else "error",
                     contact_id=contact_id, error_detail=detail, **log)
    status, code = {"unreachable": ("service_unavailable", 503), "error": ("error", 502)}.get(result, ("error", 500))
    return JSONResponse({"status": status}, status_code=code)


async def _subscribe_job(request: Request, payload: SubscribeRequest, email: str, site: dict) -> None:
    db: aiosqlite.Connection = request.app.state.db
    log = dict(action="subscribe", site=site["site"])
    try:
        if routing.targets(site["site"], "subscribe"):
            # routed site: do-not-contact across mauxy's opt-out record, every list-holding target and
            # the fallback sink (fail closed: an error -> no DOI mail), then 'subscribed' from the first one
            state = await routing.dnc_status(db, site["site"], email, fallback_kind=site["sink"])
        else:
            state = await sinks.subscriber_sink(site["sink"]).status(email, "newsletter")
    except (sinks.SinkTransient, sinks.SinkPermanent) as exc:
        # not returned to the caller (enumeration); no DOI now, the person can retry later
        logger.error("SUBSCRIBE_SINK_ERROR error=%s", str(exc)[:200])
        await log_action(request, email, "sink_error", error_detail=str(exc)[:200], **log)
        return
    if state == "subscribed":
        await log_action(request, email, "subscribe_already_confirmed", **log)
        return
    if state == "blocked":
        await log_action(request, email, "subscribe_dnc_skip", **log)
        return
    resignup = state == "optout"  # self-service opt-out (routed site): DOI allowed, the click re-consents

    await purge_newsletter_pending(db)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    pid = secrets.token_urlsafe(16)
    token = _token_signer.dumps({"p": pid, "b": site["site"]})  # b: brand of the neutral page only
    # Cap + insert in ONE statement (security review 2026-11-09, throttle race): a separate
    # COUNT followed by an INSERT let N parallel requests all see "below the cap". SQLite takes
    # the write lock at the start of a writing statement, so the sub-select and the insert are
    # atomic (also across connections / processes). rowcount 0 = capped.
    cur = await db.execute(
        "INSERT INTO newsletter_pending (id, token_hash, email, locale, source, page_url, site, segment, sink, "
        "created_at, resignup) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
        "WHERE (SELECT COUNT(*) FROM newsletter_pending WHERE email = ? AND created_at >= ?) < ?",
        (pid, token_hash(token), email, payload.locale, payload.source, payload.page_url,
         site["site"], site["segment"] or "", site["sink"] or "", now_iso(), int(resignup), email, since,
         MAX_PENDING_PER_EMAIL),
    )
    inserted = cur.rowcount == 1
    await db.commit()  # also ends the (empty) implicit transaction when capped
    if not inserted:
        await log_action(request, email, "subscribe_throttled", **log)
        return
    if resignup and not await routing.claim_resignup(db, site["site"], email):
        # abuse guard: one re-signup DOI per site + address per OPTOUT_RESIGNUP_DAYS (on top of the cap above)
        await db.execute("DELETE FROM newsletter_pending WHERE id = ?", (pid,))
        await db.commit()
        await log_action(request, email, "subscribe_resignup_throttled", **log)
        return

    subject, body = NEWSLETTER_DOI[payload.locale]
    body = brand.with_footer(body.format(
        link=f"{PUBLIC_BASE_URL}/api/confirm?token={token}",
        hours=CONFIRM_TOKEN_MAX_AGE // 3600, days=NEWSLETTER_PENDING_DAYS,
    ), site["site"], payload.locale)
    ok = await mailer.send(email, subject, body, audience="doi", key=f"nl-doi-{pid}", sender=brand.sender(site["site"]))
    if not ok:
        await db.execute("DELETE FROM newsletter_pending WHERE id = ?", (pid,))
        await db.commit()
        await log_action(request, email, "subscribe_error", error_detail="mail_failed", **log)
        return
    await log_action(request, email, "subscribe_ok", **log)


async def _pending_for_token(db: aiosqlite.Connection, token: Optional[str]) -> tuple[Optional[aiosqlite.Row], str]:
    """Read-only token check of the newsletter DOI: signature + max_age, pending row exists, stored
    hash equals the presented token (constant time), unused. (row, "") or (None, reason)."""
    if not token or len(token) > 1000:
        return None, "confirm_invalid"
    try:
        data = _token_signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except SignatureExpired:
        return None, "confirm_expired"
    except BadSignature:
        return None, "confirm_invalid"
    pid = data.get("p") if isinstance(data, dict) else None
    row = None
    if isinstance(pid, str):
        async with db.execute("SELECT * FROM newsletter_pending WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
    # Legacy tokens ({email, locale}) and replays both end here: no pending row / hash burnt.
    if row is None or row["used_at"] or not tokens_equal(row["token_hash"], token):
        return None, "confirm_invalid"
    return row, ""


def _confirm_misconfigured() -> bool:
    if _token_signer and PUBLIC_WEBSITE_BASE_URL:
        return False
    logger.error("CONFIRM_MISCONFIGURED signer=%s site=%s", bool(_token_signer), bool(PUBLIC_WEBSITE_BASE_URL))
    return True


@app.get("/api/confirm")
@limiter.limit(CONFIRM_RATE_LIMIT)
async def confirm_page_get(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
    """
    Newsletter DOI link (mail) -- step 1, READ-ONLY (security review 2026-11-09: mail scanners
    prefetch links). Validates the token and renders the confirmation page (confirm_page.py) with a
    "Bestätigen" / "Confirm" button that POSTs the token. Nothing is burnt, logged to action_log or
    written to any sink here. Invalid / expired / used: the uniform neutral page.
    """
    if _confirm_misconfigured():
        return RedirectResponse(_nl_redirect(None, "en", "error", reason="server_misconfigured"), status_code=302)
    row, reason = await _pending_for_token(request.app.state.db, token)
    if row is None:
        logger.info("CONFIRM_PAGE_INVALID reason=%s", reason)
        return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_token_signer, token)))
    site = row["site"]
    return confirm_page.render("newsletter", row["locale"], "/api/confirm", token,
                               [_nl_redirect(site, row["locale"], o) for o in ("confirmed", "error")],
                               brand=brand.get(site))


@app.post("/api/confirm")
@limiter.limit(CONFIRM_RATE_LIMIT)
async def confirm(request: Request):
    """
    Newsletter DOI -- step 2, the human click (form POST, field `token`). Always ends on the website
    (303) except for an invalid token (uniform neutral page) or a foreign Origin/Referer (403).

    Same checks as step 1, then the token is CLAIMED atomically (conditional UPDATE, single use:
    a parallel second POST finds it burnt), then the subscriber sink subscribes (Odoo:
    mailing.contact / mailing.subscription on ODOO_LIST_NEWSLETTER_ID; deprecated Mautic: tag
    newsletter_confirmed, server-side consent time). Blocked (blacklist / DNC) -> stays burnt,
    error page. Sink unreachable -> claim released, the token stays valid, the person can retry.
    """
    db: aiosqlite.Connection = request.app.state.db

    def fail(locale: str, reason: str, site: Optional[str] = None) -> RedirectResponse:
        return RedirectResponse(_nl_redirect(site, locale, "error", reason=reason), status_code=303)

    if _confirm_misconfigured():
        return fail("en", "server_misconfigured")
    if not confirm_page.same_origin(request):
        await log_action(request, "", "confirm_foreign_origin", action="confirm")
        return confirm_page.neutral(403)
    token = await confirm_page.form_token(request)
    row, reason = await _pending_for_token(db, token)
    if row is None:
        await log_action(request, "", reason, error_detail="not_pending_or_used" if token else "no_token",
                         action="confirm")
        return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_token_signer, token)))

    pid, email, locale = row["id"], row["email"], row["locale"]
    log = dict(action="confirm", site=row["site"])
    cur = await db.execute(
        "UPDATE newsletter_pending SET used_at = ?, token_hash = NULL WHERE id = ? AND used_at IS NULL AND token_hash = ?",
        (now_iso(), pid, token_hash(token)))
    claimed = cur.rowcount == 1
    await db.commit()
    if not claimed:  # a parallel POST got it first
        await log_action(request, email, "confirm_invalid", error_detail="not_pending_or_used", **log)
        return confirm_page.neutral(brand=brand.get(row["site"]))

    site = row["site"]
    if to := routing.targets(site, "subscribe"):
        # Routed site: re-check do-not-contact at the click (an opt-out may have happened after the
        # DOI mail). Error -> claim released, nothing sent (fail closed, retry later). Blocked ->
        # token stays burnt, NOBODY gets it, the same confirmed page as a success (uniform).
        try:
            dnc = await routing.dnc_status(db, site, email, fallback_kind=row["sink"] or None, primary=False)
        except (sinks.SinkTransient, sinks.SinkPermanent) as exc:
            await db.execute("UPDATE newsletter_pending SET used_at = NULL, token_hash = ? WHERE id = ?",
                             (token_hash(token), pid))
            await db.commit()
            await log_action(request, email, "confirm_error", error_detail=f"dnc_check: {str(exc)[:180]}", **log)
            return fail(locale, "service_unavailable", site)
        reconfirmed_at = None
        if dnc == "optout":
            # self-service opt-out: only a re-signup link issued AFTER it is fresh consent -> lift
            # (audited, hash only) and re-subscribe; an older link stays blocked
            orow = await routing.optout_row(db, site, email)
            if row["resignup"] and orow and row["created_at"] >= orow["created_at"]:
                reconfirmed_at = await routing.lift_optout(db, site, email, "doi_reconfirm")
            dnc = "none" if reconfirmed_at else "blocked"
        if dnc == "blocked":
            await log_action(request, email, "confirm_dnc_refused", error_detail="routed", **log)
            return RedirectResponse(_nl_redirect(site, locale, "confirmed"), status_code=303)
        # every target gets its own outbox row (retries per target, a down target neither blocks
        # nor repeats the others; the outbox gate re-checks do-not-contact per delivery)
        await routing.fan_out(db, site=site, action="subscribe", event_ref=f"nl:{pid}", to=to, data={
            "email": email, "locale": locale, "source": row["source"], "page_url": row["page_url"],
            "segment": row["segment"] or "", "lists": ["newsletter"], **({"_reconfirmed_at": reconfirmed_at} if reconfirmed_at else {})})
        await log_action(request, email, "confirm_ok",
                         error_detail=f"routed:{len(to)}" + (" reconfirmed" if reconfirmed_at else ""), **log)
        return RedirectResponse(_nl_redirect(site, locale, "confirmed"), status_code=303)

    try:
        result = await sinks.subscriber_sink(row["sink"] or None).subscribe(
            email, "newsletter", lang=locale, source=row["source"], page_url=row["page_url"],
            segment=row["segment"] or None)
    except (sinks.SinkTransient, sinks.SinkPermanent) as exc:
        await db.execute("UPDATE newsletter_pending SET used_at = NULL, token_hash = ? WHERE id = ?",
                         (token_hash(token), pid))
        await db.commit()
        await log_action(request, email, "confirm_error", error_detail=str(exc)[:200], **log)
        return fail(locale, "service_unavailable", site)  # token stays valid, user can retry
    if result == "blocked":
        await log_action(request, email, "confirm_dnc_refused", **log)
        return fail(locale, "unavailable", site)

    await log_action(request, email, "confirm_ok", error_detail=None if result == "ok" else result, **log)
    return RedirectResponse(_nl_redirect(site, locale, "confirmed"), status_code=303)


# -- Routes -----------------------------------------------------------------
@app.get("/health")
async def health():
    """k8s liveness probe -- always 200 and cheap: only says the process / event loop is alive.
    Independent of every downstream (sink, Mautic, SMTP) so an outage never restarts the pod."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    """k8s readiness probe -- local only: the SQLite DB answers. Deliberately NOT tied to a sink
    (upstream tied it to Mautic): every endpoint keeps working while erp.wapsol / Mautic is down
    (DOI pending + outbox in SQLite, retries), so pulling the pod would only make it worse."""
    try:
        async with request.app.state.db.execute("SELECT 1") as cur:
            await cur.fetchone()
    except Exception as exc:
        logger.error("READY_DB_ERROR error=%s", exc.__class__.__name__)
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
    return {"status": "ready"}


@app.get("/health/detail")
async def health_detail(request: Request):
    """Operator view: sink reachability (cached 30 s). Always 200."""
    result = await _check_sinks()
    ok = all(not v.startswith("error") for v in result["detail"].values())
    return {
        "status": "ok" if ok else "degraded",
        **result["detail"],
        "brands": brand.count(),
        "routes": routing.count(),
        "webhooks": f"{len(webhooks.allowed_hosts())} allowed host(s)" if webhooks.allowed_hosts() else "disabled",
        "deliveries_failed": await routing.failed_count(request.app.state.db),
        "auth_linkedin": auth_api.status(),  # 'configured' or the names of missing settings
        "sites": len(_SITES_BY_KEY),
        "quiz": "configured" if CHALLENGE_SECRET and QUIZ_BANK else "disabled",
        "mautic_version": get_mautic_version() or "unknown",
        "cache_age_seconds": round(time.monotonic() - result["checked_at"], 1),
    }


@app.post("/api/unsubscribe")
@limiter.limit(RATE_LIMIT)
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """
    Opt-out via the subscriber sink (Odoo: opt_out on the address' subscriptions of the
    configured lists; deprecated Mautic: DNC list) of the CALLING SITE's sink. Gates: per-site key
    (401), honeypot (silent ok), quiz (409) -- all independent of the address. The quiz is ALWAYS
    required here, also for sites with subscribe_quiz off: an opt-out has no confirmation mail
    that could stand in for it, and the site key of a static site is public.
    Returns 200 {"status":"ok"} for all contact-specific outcomes (prevents enumeration).
    Returns 503 {"status":"service_unavailable"} when the sink cannot be reached at all.
    """
    site = _site_for_request(request)
    if site is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = payload.email.lower()
    log = dict(action="unsubscribe", site=site["site"])
    if payload.company_website:  # honeypot
        await log_action(request, email, "honeypot", **log)
        return JSONResponse({"status": "ok"})
    if not _verify_challenge(payload.challenge, payload.answer, site["topic"]):
        await log_action(request, email, "quiz_failed", **log)
        return JSONResponse({"status": "quiz_failed"}, status_code=409)
    # mauxy's own do-not-contact record (db-patches/0007), whatever the path: webhook-only sites
    # have no other source, and a later routing change must still know this opt-out
    await routing.record_optout(request.app.state.db, site["site"], email)
    to = routing.unsubscribe_targets(site["site"], site["sink"])
    if to is not None:
        # Routed site: the opt-out goes to EVERY receiver that may hold the address (routing.py),
        # each through its own outbox row with retries; failures are mailed to LEAD_NOTIFY_TO.
        await routing.fan_out(request.app.state.db, site=site["site"], action="unsubscribe",
                              event_ref=routing.new_event("unsub"), data={"email": email}, to=to)
        await log_action(request, email, "unsubscribe_queued", error_detail=f"routed:{len(to)}", **log)
        return JSONResponse({"status": "ok"})
    try:
        sink = sinks.subscriber_sink(site["sink"])
        if isinstance(sink, sinks.OdooSubscriberSink):  # provenance of mauxy's Odoo opt-outs (patch 0009)
            result, ref, marks = await sink.unsubscribe_marked(email)
            await routing.record_odoo_marks(request.app.state.db, site["site"], email, marks)
        else:
            result, ref = await sink.unsubscribe(email)
    except sinks.SinkTransient as exc:
        logger.error("UNSUBSCRIBE_SINK_UNREACHABLE error=%s", str(exc)[:200])
        await log_action(request, email, "sink_unreachable", error_detail=str(exc)[:200], **log)
        return JSONResponse({"status": "service_unavailable"}, status_code=503)
    except sinks.SinkPermanent as exc:
        logger.error("UNSUBSCRIBE_SINK_ERROR error=%s", str(exc)[:200])
        await log_action(request, email, "error", error_detail=str(exc)[:200], **log)
        return JSONResponse({"status": "ok"})
    await log_action(request, email, result, contact_id=ref, **log)
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
    """Admin endpoint: query the action log (rows carry action + site). Bearer ADMIN_API_KEY."""
    if (err := _require_admin(request)) is not None:
        return err

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


# -- Admin: site registry, quiz bank, Mautic instance (upstream) ----------------------
# Bearer ADMIN_API_KEY; every write calls reload_config() so the change is live at once.
@app.get("/api/admin/sites")
async def admin_list_sites(request: Request):
    """All sites incl. their Bearer keys (the admin holds the master key anyway)."""
    if (err := _require_admin(request)) is not None:
        return err
    rows = await request.app.state.db.execute_fetchall(
        "SELECT key, site, segment, topic, subscribe_quiz, subscriber_sink, doi_owner, created_at FROM sites ORDER BY site")
    return JSONResponse({"sites": [dict(r) for r in rows], "count": len(rows)})


@app.post("/api/admin/sites")
async def admin_create_site(payload: SiteCreate, request: Request):
    """Onboard a site. 409 if the key exists."""
    if (err := _require_admin(request)) is not None:
        return err
    if err := policy_error(payload.subscriber_sink, payload.doi_owner):
        return JSONResponse({"error": err}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    try:
        await db.execute(
            "INSERT INTO sites (key, site, segment, topic, subscribe_quiz, subscriber_sink, doi_owner, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (payload.key, payload.site, payload.segment, payload.topic, int(payload.subscribe_quiz),
             payload.subscriber_sink, payload.doi_owner, now_iso()))
        await db.commit()
    except aiosqlite.IntegrityError:
        return JSONResponse({"error": "site key already exists"}, status_code=409)
    await reload_config(db)
    logger.info("ADMIN_SITE_CREATED site=%s", payload.site)
    return JSONResponse({"status": "created", "site": payload.site}, status_code=201)


@app.put("/api/admin/sites/{key}")
async def admin_update_site(key: str, payload: SiteUpdate, request: Request):
    """Change site / segment / topic of a key (the key itself is immutable)."""
    if (err := _require_admin(request)) is not None:
        return err
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    if "subscriber_sink" in fields:
        fields["subscriber_sink"] = fields["subscriber_sink"] or None  # "" -> global sink
    if "subscribe_quiz" in fields:
        fields["subscribe_quiz"] = int(fields["subscribe_quiz"])
    current = _SITES_BY_KEY.get(key)
    if current is not None:  # policy of the resulting row
        sink = fields["subscriber_sink"] if "subscriber_sink" in fields else current["sink"]
        if err := policy_error(sink, fields.get("doi_owner", current["doi_owner"])):
            return JSONResponse({"error": err}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    sets = ", ".join(f"{col} = ?" for col in fields)  # columns = fixed model fields, not user input
    cur = await db.execute(f"UPDATE sites SET {sets} WHERE key = ?", [*fields.values(), key])
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "site not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/sites/{key}")
async def admin_delete_site(key: str, request: Request):
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute("DELETE FROM sites WHERE key = ?", (key,))
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "site not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "deleted"})


@app.get("/api/admin/quiz")
async def admin_list_quiz(request: Request, topic: Optional[str] = Query(None)):
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    sql = "SELECT topic, qid, question, choices_json, answer FROM quiz_questions"
    rows = await (db.execute_fetchall(sql + " WHERE topic = ? ORDER BY qid", (topic,)) if topic
                  else db.execute_fetchall(sql + " ORDER BY topic, qid"))
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
    """409 if (topic, id) exists; 400 if answer out of range."""
    if (err := _require_admin(request)) is not None:
        return err
    if not 0 <= payload.answer < len(payload.choices):
        return JSONResponse({"error": "answer index out of range"}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    try:
        await db.execute(
            "INSERT INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES (?,?,?,?,?)",
            (payload.topic, payload.id, payload.q, json.dumps(payload.choices, ensure_ascii=False), payload.answer))
        await db.commit()
    except aiosqlite.IntegrityError:
        return JSONResponse({"error": "question (topic, id) already exists"}, status_code=409)
    await reload_config(db)
    return JSONResponse({"status": "created"}, status_code=201)


@app.put("/api/admin/quiz/{topic}/{qid}")
async def admin_update_quiz(topic: str, qid: str, payload: QuizQuestionModel, request: Request):
    """Replace text / choices / answer; (topic, qid) in the path locates the question."""
    if (err := _require_admin(request)) is not None:
        return err
    if not 0 <= payload.answer < len(payload.choices):
        return JSONResponse({"error": "answer index out of range"}, status_code=400)
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute(
        "UPDATE quiz_questions SET question = ?, choices_json = ?, answer = ? WHERE topic = ? AND qid = ?",
        (payload.q, json.dumps(payload.choices, ensure_ascii=False), payload.answer, topic, qid))
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "question not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "updated"})


@app.delete("/api/admin/quiz/{topic}/{qid}")
async def admin_delete_quiz(topic: str, qid: str, request: Request):
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    cur = await db.execute("DELETE FROM quiz_questions WHERE topic = ? AND qid = ?", (topic, qid))
    await db.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "question not found"}, status_code=404)
    await reload_config(db)
    return JSONResponse({"status": "deleted"})


@app.get("/api/admin/mautic-instance")
async def admin_get_mautic_instance(request: Request):
    if (err := _require_admin(request)) is not None:
        return err
    rows = await request.app.state.db.execute_fetchall(
        "SELECT base_url, version, source, updated_at FROM mautic_instance WHERE id = 1")
    return JSONResponse({"mautic_instance": dict(rows[0]) if rows else None})


@app.put("/api/admin/mautic-instance")
async def admin_set_mautic_instance(payload: MauticInstanceUpdate, request: Request):
    """Set the target Mautic version (manual); the resolver cache updates at once."""
    if (err := _require_admin(request)) is not None:
        return err
    db: aiosqlite.Connection = request.app.state.db
    base_url = payload.base_url if payload.base_url is not None else MAUTIC_BASE_URL
    await db.execute(
        """INSERT INTO mautic_instance (id, base_url, version, source, updated_at)
           VALUES (1, ?, ?, 'manual', ?)
           ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url,
               version=excluded.version, source='manual', updated_at=excluded.updated_at""",
        (base_url, payload.version, now_iso()))
    await db.commit()
    await _refresh_mautic_instance(db)
    logger.info("ADMIN_MAUTIC_VERSION_SET version=%s", payload.version)
    return JSONResponse({"status": "updated", "version": payload.version})


# -- Per-site brand profiles and action routing (db-patches/0006) ---------------------------
app.include_router(brand.build_router(require_admin=_require_admin, on_change=reload_config))
app.include_router(routing.build_router(require_admin=_require_admin, on_change=reload_config))
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
