"""
Website visitor LinkedIn login (owner decision 2026-11-08: mauxy is the single public website
edge; replaces app2li). SIGN-IN ONLY: OpenID Connect scopes `openid profile email`. LinkedIn
posting (member / company page) is NOT part of mauxy: no publishing scopes, no member tokens kept.

  GET  /api/auth/linkedin/start?lang=de&return=/de/kontakt&binding=<random 43+ chars>
         lang + return = where the site continues (same-site path, validated); binding = random
         value the site keeps in sessionStorage (binds the one-time code to that tab).
         Site origin = the Referer's origin if it is in AUTH_ALLOWED_ORIGINS, else the first entry.
         -> 302 to LinkedIn authorize (client_id, redirect_uri, state, scope) + Set-Cookie
            (HttpOnly, Secure, SameSite=Lax, 10 min) that binds the state to this browser.
  GET  /api/auth/linkedin/callback?code=&state=   (= LINKEDIN_REDIRECT_URI)
         state: signed (itsdangerous, own salt), max 10 min, single use (SQLite row burnt first),
         cookie must match (closes login CSRF). Code exchange + userinfo over verified TLS; the
         LinkedIn access token lives only inside this request. Profile parked for 60 s under a
         random one-time code (SHA-256 stored). -> 302 {origin}/{lang}/auth/callback?code=<code>
         Errors -> 302 {origin}/{lang}/auth/callback?error=<code> (no description, no PII).
  POST /api/auth/session {code, binding}   (site, CORS; Origin must be the code's site origin)
         single use (row deleted on first try), 60 s, binding must match.
         -> {status, profile{sub,name,given_name,family_name,email,email_verified}, session,
             expires_at, return}; uniform 400 {"status": "invalid_code"} for every failure.

Why a one-time code + POST, not a URL fragment: the profile (name, e-mail) never appears in a
URL, browser history, Referer, analytics page_location or access log; the code itself is
opaque, 60 s, single use and useless without the tab's binding.

Stored: auth_state (no PII, 10 min), auth_code (profile, max 60 s, deleted on exchange / purge),
one audit line per login in action_log WITHOUT name / e-mail / tokens. Secrets from env only,
never logged. Config problems disable the login (error=unavailable); the rest of mauxy keeps
running (newsletter, leads).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit

import aiosqlite
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("mauxy.auth")

LOOPBACK = ("10.0.99.1", "127.0.0.1")       # plain http only here (local dev / mocked LinkedIn)
START_PATH = "/api/auth/linkedin/start"
CALLBACK_PATH = "/api/auth/linkedin/callback"
SESSION_PATH = "/api/auth/session"
SCOPES = "openid profile email"             # sign-in only; never w_member_social & co.
STATE_TTL = 600                             # login must finish within 10 min
CODE_TTL = 60                               # site exchanges the one-time code within 60 s
HTTP_TIMEOUT = 10.0


def _origins(raw: str) -> list[str]:
    return [o.strip().rstrip("/").lower() for o in (raw or "").split(",") if o.strip()]


# -- config (env, 12-factor) ------------------------------------------------------------------
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LINKEDIN_CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
# default: the callback on mauxy's own public host (must equal the LinkedIn portal entry exactly)
LINKEDIN_REDIRECT_URI = (os.environ.get("LINKEDIN_REDIRECT_URI", "")
                         or (PUBLIC_BASE_URL + CALLBACK_PATH if PUBLIC_BASE_URL else ""))
# overridable only for a local mocked LinkedIn on the loopback alias (checked in problems())
LINKEDIN_AUTH_URL = os.environ.get("LINKEDIN_AUTH_URL", "") or "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = os.environ.get("LINKEDIN_TOKEN_URL", "") or "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = os.environ.get("LINKEDIN_USERINFO_URL", "") or "https://api.linkedin.com/v2/userinfo"
# sites allowed as return targets (multi-site via the allowlist); default = the lead origins
AUTH_ALLOWED_ORIGINS = _origins(os.environ.get("AUTH_ALLOWED_ORIGINS", "")
                                or os.environ.get("LEAD_ALLOWED_ORIGINS", "")
                                or os.environ.get("SELFCHECK_ALLOWED_ORIGINS", ""))
# own key preferred; falls back to CONFIRM_TOKEN_SECRET (own salts below, so tokens never cross)
AUTH_TOKEN_SECRET = os.environ.get("AUTH_TOKEN_SECRET", "") or os.environ.get("CONFIRM_TOKEN_SECRET", "")
AUTH_SESSION_TTL = int(os.environ.get("AUTH_SESSION_TTL", "3600"))       # site session: 1 h
AUTH_RATE_LIMIT = os.environ.get("AUTH_RATE_LIMIT", "20/minute")          # start + callback per IP
AUTH_SESSION_RATE_LIMIT = os.environ.get("AUTH_SESSION_RATE_LIMIT", "30/minute")

_state_signer = URLSafeTimedSerializer(AUTH_TOKEN_SECRET, salt="mauxy-auth-state") if AUTH_TOKEN_SECRET else None
_session_signer = URLSafeTimedSerializer(AUTH_TOKEN_SECRET, salt="mauxy-auth-session") if AUTH_TOKEN_SECRET else None

# Cookie on mauxy's host (start and callback must be the same host). __Host- prefix = Secure,
# Path=/, no Domain: a sibling subdomain cannot plant it (cookie tossing). Plain http only on the
# loopback alias for local dev.
COOKIE_SECURE = LINKEDIN_REDIRECT_URI.startswith("https://")
COOKIE_NAME = "__Host-mauxy_li" if COOKIE_SECURE else "mauxy_li"

BINDING_RE = r"^[A-Za-z0-9_-]{32,128}$"
LANG_RE = re.compile(r"^(de|en)$")
# same-site path: /de or /en + ASCII segments (+ simple query); no scheme, host, '//', '\',
# encoded '/' or '\', dot segments, fragments or control characters
RETURN_RE = re.compile(r"^/(de|en)(/[A-Za-z0-9._~%-]+)*/?(\?[A-Za-z0-9._~%=&+-]*)?$")
ERROR_RE = re.compile(r"^[a-z_]{1,40}$")    # LinkedIn error codes are snake_case


def _secure_url(url: str) -> bool:
    """https, or plain http on the loopback alias (local dev with a mocked LinkedIn)."""
    p = urlsplit(url or "")
    if not p.hostname or p.username or p.password:
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in LOOPBACK)


def problems() -> list[str]:
    """Config that disables the login (names only, never values)."""
    out = []
    if not LINKEDIN_CLIENT_ID or not LINKEDIN_CLIENT_SECRET:
        out.append("LINKEDIN_CLIENT_ID/LINKEDIN_CLIENT_SECRET unset")
    if len(AUTH_TOKEN_SECRET) < 32:
        out.append("AUTH_TOKEN_SECRET (or CONFIRM_TOKEN_SECRET) unset or < 32 chars")
    if not _secure_url(LINKEDIN_REDIRECT_URI) or urlsplit(LINKEDIN_REDIRECT_URI).path != CALLBACK_PATH:
        out.append(f"LINKEDIN_REDIRECT_URI not https://<mauxy host>{CALLBACK_PATH}")
    for name, url in (("LINKEDIN_AUTH_URL", LINKEDIN_AUTH_URL), ("LINKEDIN_TOKEN_URL", LINKEDIN_TOKEN_URL),
                      ("LINKEDIN_USERINFO_URL", LINKEDIN_USERINFO_URL)):
        if not _secure_url(url):
            out.append(f"{name} not https")
    if not AUTH_ALLOWED_ORIGINS:
        out.append("AUTH_ALLOWED_ORIGINS (or LEAD_ALLOWED_ORIGINS) empty")
    elif any(o == "*" or not _secure_url(o) for o in AUTH_ALLOWED_ORIGINS):
        out.append("AUTH_ALLOWED_ORIGINS has '*' or a non-https origin")
    return out


_PROBLEMS = problems()
if _PROBLEMS:
    logger.warning("LINKEDIN_LOGIN_DISABLED %s", "; ".join(_PROBLEMS))


def status() -> str:
    """For /health/detail: 'configured' or 'disabled: <names>' (no values)."""
    return "configured" if not _PROBLEMS else "disabled: " + "; ".join(_PROBLEMS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _same(stored_hash: Optional[str], value: str) -> bool:
    return bool(stored_hash) and bool(value) and hmac.compare_digest(stored_hash, _sha(value))


# -- storage ------------------------------------------------------------------------------------
async def init_db(db: aiosqlite.Connection) -> None:
    # pending logins: no PII (hashes + timestamps only)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS auth_state (
            id           TEXT PRIMARY KEY,
            cookie_hash  TEXT NOT NULL,
            binding_hash TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            used_at      TEXT
        )
    """)
    # finished logins waiting for the site's exchange: profile for max CODE_TTL seconds
    await db.execute("""
        CREATE TABLE IF NOT EXISTS auth_code (
            code_hash    TEXT PRIMARY KEY,
            binding_hash TEXT NOT NULL,
            origin       TEXT NOT NULL,
            ret          TEXT NOT NULL,
            profile      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
    """)


async def purge(db: aiosqlite.Connection) -> None:
    now = _now()
    await db.execute("DELETE FROM auth_state WHERE created_at < ?", (_iso(now - timedelta(seconds=STATE_TTL)),))
    await db.execute("DELETE FROM auth_code WHERE created_at < ?", (_iso(now - timedelta(seconds=CODE_TTL)),))
    await db.commit()


async def purger(db: aiosqlite.Connection, every: float = 30.0) -> None:
    """Background loop (lifespan): parked profiles never outlive CODE_TTL by much, even when no
    further login comes in. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(every)
        try:
            await purge(db)
        except Exception as exc:  # never kill the loop
            logger.error("AUTH_PURGE_FAILED error=%s", type(exc).__name__)


async def _audit(db: aiosqlite.Connection, result: str, origin: str = "", detail: Optional[str] = None) -> None:
    """Minimal audit line: result + site origin. No name, e-mail, IP or token."""
    try:
        await db.execute(
            "INSERT INTO action_log (ts, email, source_origin, source_ip, result, contact_id, error_detail) "
            "VALUES (?, '', ?, '', ?, NULL, ?)", (_iso(_now()), origin, result, detail))
        await db.commit()
    except Exception as exc:
        logger.error("AUTH_AUDIT_FAILED error=%s", type(exc).__name__)


# -- helpers --------------------------------------------------------------------------------------
def safe_return(path: Optional[str], lang: str) -> Optional[str]:
    """Same-site return path or None. Empty -> '/{lang}/'. Never a URL: anything with a scheme,
    host, '//', backslash, encoded slash/backslash, dot segment, fragment or the callback itself
    is refused (open redirect)."""
    if not path:
        return f"/{lang}/"
    if len(path) > 300 or not RETURN_RE.match(path):
        return None
    low = path.lower()
    if "%2f" in low or "%5c" in low or "%2e" in low or "%00" in low:
        return None
    segments = path.split("?", 1)[0].split("/")
    if any(s in (".", "..") for s in segments):
        return None
    if len(segments) > 2 and segments[2] == "auth":      # /xx/auth/... (callback loop)
        return None
    return path


def site_origin(request: Request) -> str:
    """The Referer's origin when allowlisted (multi-site), else the first allowed origin. The
    Referer can only choose among allowlisted origins, never add one."""
    ref = request.headers.get("referer", "")
    if ref:
        try:
            p = urlsplit(ref)
            if p.hostname and not p.username and not p.password:
                o = f"{p.scheme}://{p.netloc}".lower()
                if o in AUTH_ALLOWED_ORIGINS:
                    return o
        except ValueError:
            pass
    return AUTH_ALLOWED_ORIGINS[0] if AUTH_ALLOWED_ORIGINS else ""


def _to_site(origin: str, lang: str, **params: str) -> RedirectResponse:
    """302 to the site's callback page; params = code or error (nothing else, no PII)."""
    resp = RedirectResponse(f"{origin}/{lang}/auth/callback?{urlencode(params)}", status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.delete_cookie(COOKIE_NAME, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    return resp


def _clean(value, limit: int) -> str:
    """Single-line text from LinkedIn: str only, control characters removed, length capped."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:limit]


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$")


async def linkedin_profile(code: str) -> tuple[Optional[dict], str]:
    """(profile, '') or (None, reason). Code -> access token -> userinfo, both over verified
    TLS (httpx default context), no redirects followed. The token is never stored or logged."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=True, follow_redirects=False) as client:
            r = await client.post(LINKEDIN_TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": LINKEDIN_REDIRECT_URI,
                "client_id": LINKEDIN_CLIENT_ID, "client_secret": LINKEDIN_CLIENT_SECRET,
            }, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None, f"token_http_{r.status_code}"
            token = (r.json() or {}).get("access_token")
            if not isinstance(token, str) or not token:
                return None, "token_missing"
            u = await client.get(LINKEDIN_USERINFO_URL, headers={"Authorization": f"Bearer {token}",
                                                                 "Accept": "application/json"})
            del token
            if u.status_code != 200:
                return None, f"userinfo_http_{u.status_code}"
            d = u.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"linkedin_{type(exc).__name__}"
    if not isinstance(d, dict):
        return None, "userinfo_shape"
    sub = _clean(d.get("sub"), 100)
    if not sub:
        return None, "userinfo_no_sub"
    email = _clean(d.get("email"), 254).lower()
    verified = d.get("email_verified") is True
    if email and (not EMAIL_RE.match(email) or d.get("email_verified") is False):
        email, verified = "", False                       # unverified / odd address: not used
    return {
        "sub": sub,
        "name": _clean(d.get("name"), 120),
        "given_name": _clean(d.get("given_name"), 80),
        "family_name": _clean(d.get("family_name"), 80),
        "email": email,
        "email_verified": verified,
    }, ""


def issue_session(profile: dict, origin: str) -> str:
    """Short-lived signed session token (AUTH_SESSION_TTL; verify with verify_session)."""
    return _session_signer.dumps({"typ": "session", "aud": origin, "sub": profile["sub"],
                                  "name": profile["name"], "email": profile["email"]})


def verify_session(token: str, origin: Optional[str] = None) -> Optional[dict]:
    """Claims of a valid, unexpired session token (optionally for this site origin), else None."""
    if not _session_signer or not token:
        return None
    try:
        data = _session_signer.loads(token, max_age=AUTH_SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or data.get("typ") != "session":
        return None
    if origin is not None and data.get("aud") != origin.rstrip("/").lower():
        return None
    return data


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    binding: str = Field(pattern=BINDING_RE)


INVALID = {"status": "invalid_request"}
INVALID_CODE = {"status": "invalid_code"}


# -- routes ---------------------------------------------------------------------------------------
def build_router(*, limiter) -> APIRouter:
    router = APIRouter()

    @router.get(START_PATH)
    @limiter.limit(AUTH_RATE_LIMIT)
    async def auth_start(
        request: Request,
        lang: str = Query("de", max_length=2),
        return_: Optional[str] = Query(None, alias="return", max_length=300),
        binding: str = Query("", max_length=128),
    ):
        db: aiosqlite.Connection = request.app.state.db
        if not LANG_RE.match(lang) or not re.match(BINDING_RE, binding):
            return JSONResponse(INVALID, status_code=400)
        ret = safe_return(return_, lang)
        if ret is None:
            await _audit(db, "auth_start_refused", detail="return_path")
            return JSONResponse(INVALID, status_code=400)
        origin = site_origin(request)
        if not origin:
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
        # cookie only reaches the callback when start runs on the redirect_uri's host
        if _PROBLEMS or (request.headers.get("host", "").lower() != urlsplit(LINKEDIN_REDIRECT_URI).netloc.lower()):
            logger.error("LINKEDIN_LOGIN_UNAVAILABLE problems=%s host_ok=%s", len(_PROBLEMS),
                         request.headers.get("host", "").lower() == urlsplit(LINKEDIN_REDIRECT_URI).netloc.lower())
            await _audit(db, "auth_start_error", origin, "unavailable")
            return _to_site(origin, lang, error="unavailable")
        await purge(db)
        sid, cookie = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
        await db.execute("INSERT INTO auth_state (id, cookie_hash, binding_hash, created_at) VALUES (?, ?, ?, ?)",
                         (sid, _sha(cookie), _sha(binding), _iso(_now())))
        await db.commit()
        state = _state_signer.dumps({"i": sid, "o": origin, "l": lang, "r": ret})
        url = LINKEDIN_AUTH_URL + "?" + urlencode({
            "response_type": "code", "client_id": LINKEDIN_CLIENT_ID, "redirect_uri": LINKEDIN_REDIRECT_URI,
            "state": state, "scope": SCOPES,
        })
        resp = RedirectResponse(url, status_code=302)
        resp.headers["Cache-Control"] = "no-store"
        resp.set_cookie(COOKIE_NAME, cookie, max_age=STATE_TTL, path="/", secure=COOKIE_SECURE,
                        httponly=True, samesite="lax")
        return resp

    @router.get(CALLBACK_PATH)
    @limiter.limit(AUTH_RATE_LIMIT)
    async def auth_callback(
        request: Request,
        code: Optional[str] = Query(None, max_length=2000),
        state: Optional[str] = Query(None, max_length=2000),
        error: Optional[str] = Query(None, max_length=200),
    ):
        # error_description from LinkedIn is ignored on purpose (never reflected to the site)
        db: aiosqlite.Connection = request.app.state.db
        await purge(db)
        fallback = AUTH_ALLOWED_ORIGINS[0] if AUTH_ALLOWED_ORIGINS else ""
        if not fallback:
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
        if _PROBLEMS:
            return _to_site(fallback, "de", error="unavailable")

        data, why = None, "state_missing"
        if state and _state_signer:
            try:
                data = _state_signer.loads(state, max_age=STATE_TTL)
            except SignatureExpired:
                why = "state_expired"
            except BadSignature:
                why = "state_bad"
        ok_shape = (isinstance(data, dict) and isinstance(data.get("i"), str) and data.get("o") in AUTH_ALLOWED_ORIGINS
                    and isinstance(data.get("l"), str) and LANG_RE.match(data["l"])
                    and isinstance(data.get("r"), str) and safe_return(data["r"], data["l"]) == data["r"])
        if not ok_shape:
            await _audit(db, "auth_callback_refused", fallback, why if data is None else "state_shape")
            return _to_site(fallback, "de", error="invalid_state")
        origin, lang = data["o"], data["l"]

        # burn first, atomically: only the request whose UPDATE changed the row continues (a
        # replay or a parallel second request changes nothing), whatever happens afterwards
        cur = await db.execute("UPDATE auth_state SET used_at = ? WHERE id = ? AND used_at IS NULL",
                               (_iso(_now()), data["i"]))
        won = cur.rowcount == 1
        await db.commit()
        async with db.execute("SELECT * FROM auth_state WHERE id = ?", (data["i"],)) as cur:
            row = await cur.fetchone()
        if not won or row is None:
            await _audit(db, "auth_callback_refused", origin, "state_replay")
            return _to_site(origin, lang, error="invalid_state")
        if not _same(row["cookie_hash"], request.cookies.get(COOKIE_NAME, "")):
            await _audit(db, "auth_callback_refused", origin, "cookie_mismatch")
            return _to_site(origin, lang, error="invalid_state")
        if error:
            err = error if ERROR_RE.match(error) else "login_failed"
            await _audit(db, "auth_callback_error", origin, err)
            return _to_site(origin, lang, error=err)
        if not code:
            await _audit(db, "auth_callback_error", origin, "code_missing")
            return _to_site(origin, lang, error="login_failed")

        profile, reason = await linkedin_profile(code)
        if not profile:
            logger.warning("LINKEDIN_LOGIN_FAILED reason=%s", reason)
            await _audit(db, "auth_callback_error", origin, reason)
            return _to_site(origin, lang, error="login_failed")
        one_time = secrets.token_urlsafe(32)
        await db.execute(
            "INSERT INTO auth_code (code_hash, binding_hash, origin, ret, profile, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_sha(one_time), row["binding_hash"], origin, data["r"], json.dumps(profile), _iso(_now())))
        await db.commit()
        await _audit(db, "auth_login_ok", origin)
        return _to_site(origin, lang, code=one_time)

    @router.post(SESSION_PATH)
    @limiter.limit(AUTH_SESSION_RATE_LIMIT)
    async def auth_session(payload: SessionRequest, request: Request):
        db: aiosqlite.Connection = request.app.state.db
        if _PROBLEMS:
            return JSONResponse({"status": "service_unavailable"}, status_code=503)
        await purge(db)                                    # expired codes are gone before the lookup
        h = _sha(payload.code)
        async with db.execute("SELECT * FROM auth_code WHERE code_hash = ?", (h,)) as cur:
            row = await cur.fetchone()
        if row is not None:                                # single use: gone after the first try
            await db.execute("DELETE FROM auth_code WHERE code_hash = ?", (h,))
            await db.commit()
        origin = request.headers.get("origin", "").rstrip("/").lower()
        if row is None or origin != row["origin"] or not _same(row["binding_hash"], payload.binding):
            await _audit(db, "auth_session_refused", origin if origin in AUTH_ALLOWED_ORIGINS else "",
                         "unknown_or_used" if row is None else "origin_or_binding")
            return JSONResponse(INVALID_CODE, status_code=400)
        profile = json.loads(row["profile"])
        resp = JSONResponse({
            "status": "ok",
            "profile": profile,
            "session": issue_session(profile, row["origin"]),
            "expires_at": _iso(_now() + timedelta(seconds=AUTH_SESSION_TTL)),
            "return": row["ret"],
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return router

