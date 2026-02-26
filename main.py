"""
Mautic DNC (Do Not Contact) proxy microservice.
Accepts unsubscribe requests from the SPA frontend and adds contacts
to Mautic's DNC list via Basic Auth -- credentials never exposed to browser.
"""
import os
import logging

from fastapi import FastAPI, Request
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
MAUTIC_BASE_URL = os.environ.get("MAUTIC_BASE_URL", "https://engage.wapsol.de")
MAUTIC_USERNAME = os.environ.get("MAUTIC_USERNAME", "")
MAUTIC_PASSWORD = os.environ.get("MAUTIC_PASSWORD", "")
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5/minute")

# CORS origins (comma-separated)
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://simplify-erp.de,https://www.simplify-erp.de",
)
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# -- App setup --------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Mautic Unsubscribe Proxy", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# -- Models -----------------------------------------------------------------
class UnsubscribeRequest(BaseModel):
    email: EmailStr


# -- Routes -----------------------------------------------------------------
@app.get("/health")
async def health():
    """k8s liveness / readiness probe."""
    return {"status": "ok"}


@app.post("/api/unsubscribe")
@limiter.limit(RATE_LIMIT)
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """
    Add email to Mautic DNC list.
    Always returns {"status": "ok"} to prevent email enumeration.
    """
    email = payload.email.lower()
    auth = (MAUTIC_USERNAME, MAUTIC_PASSWORD)
    logger.info("UNSUBSCRIBE_START email=%s", email)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Find contact by exact email match
            search_url = f"{MAUTIC_BASE_URL}/api/contacts"
            resp = await client.get(
                search_url,
                params={
                    "where[0][col]": "email",
                    "where[0][expr]": "eq",
                    "where[0][val]": email,
                },
                auth=auth,
            )

            if resp.status_code != 200:
                logger.warning("UNSUBSCRIBE_SEARCH_FAILED email=%s status=%s", email, resp.status_code)
                return JSONResponse({"status": "ok"})

            contacts = resp.json().get("contacts", {})
            if not contacts:
                logger.warning("UNSUBSCRIBE_NO_CONTACT email=%s", email)
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

            if not dnc_ok:
                logger.error("UNSUBSCRIBE_DNC_FAILED_RETRY_EXHAUSTED email=%s contact_id=%s", email, contact_id)

    except httpx.RequestError as exc:
        logger.error("UNSUBSCRIBE_REQUEST_ERROR email=%s error=%s", email, exc)
    except Exception as exc:
        logger.error("UNSUBSCRIBE_UNEXPECTED_ERROR email=%s error=%s", email, exc)

    # Always 200 -- no enumeration leak
    return JSONResponse({"status": "ok"})
