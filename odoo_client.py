"""
Odoo JSON-RPC client of mauxy (erp.wapsol.de, Odoo 18) -- the ONE place that talks to Odoo.

Sanctioned write path (flywheel SPEC-021, decisions 2026-11-08): website leads and mailing-list
subscribers, AFTER the double opt-in. Everything else stays read-only. Least privilege twice:

  1. Odoo side: dedicated API user (e.g. "website-bot") in its own access group, see README
     "erp.wapsol setup". Odoo refuses anything outside that group's ACLs / record rules.
  2. Client side (here): ALLOWED lists every (model, method) mauxy may call. Anything else raises
     OdooRefused before a request leaves the pod. write is allowed on mailing.subscription only,
     and only with the opt_out flag. No write / unlink on res.partner or crm.lead, ever.

Transport: JSON-RPC over HTTPS (/jsonrpc, service common/object, execute_kw), httpx with TLS
certificate verification (system CA store). Plain http is refused (MITM: the API key travels in
the request body). Odoo 18 supports /jsonrpc; Odoo 19+ deprecates it in favour of the JSON-2 API
(revisit on upgrade).

Credentials from env only (12-factor; same names as the flywheel's read client, but mauxy has its
OWN Odoo user): ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY. The key is never logged, never part of
an exception text (errors carry model.method + Odoo's exception class only).

Errors:
  OdooTransient  network, timeout, HTTP 5xx/429, DB concurrency -> worth a retry
  OdooPermanent  config, login refused, AccessError, ValidationError, UserError, HTTP 4xx
  OdooRefused    (subclass of OdooPermanent) blocked by the client-side allowlist
"""
from __future__ import annotations

import itertools
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("mauxy.odoo")

# (model, method) pairs mauxy may call. Reads are narrow too: only what the sinks need.
ALLOWED: frozenset[tuple[str, str]] = frozenset({
    # lead sink
    ("res.partner", "search_read"),        # partner match by e-mail (exactly one -> partner_id)
    ("res.users", "read"),                 # salesperson (Ashant) -> his partner for the follower
    ("crm.lead", "create"),                # the lead (type lead)
    ("crm.lead", "search_read"),           # retry safety: find a lead created by an earlier attempt
    ("crm.lead", "read"),                  # follower check of a lead mauxy just created
    ("crm.lead", "message_subscribe"),     # Ashant as follower
    ("utm.source", "search_read"),         # UTM mapping (no create: map to existing records)
    ("utm.medium", "search_read"),
    ("utm.campaign", "search_read"),
    ("ir.model.data", "search_read"),      # res.partner.title ids by xml id (salutation -> crm.lead.title; no create)
    # subscriber sink (Email Marketing)
    ("mail.blacklist", "search_read"),     # blacklisted -> never (re)subscribe, no DOI mail
    ("mailing.list", "search_read"),       # smoke check only
    ("mailing.contact", "search_read"),
    ("mailing.contact", "create"),         # new contact + subscription to ONE list
    ("mailing.subscription", "search_read"),
    ("mailing.subscription", "create"),    # existing contact joins the list
    ("mailing.subscription", "write"),     # opt_out flag only (see WRITE_FIELDS)
})
# write: model -> the only fields that may be written
WRITE_FIELDS: dict[str, frozenset[str]] = {"mailing.subscription": frozenset({"opt_out"})}

# Odoo exception classes that will not go away by retrying
_PERMANENT_PREFIXES = ("odoo.exceptions.", "builtins.", "werkzeug.exceptions.")
_TRANSIENT_NAMES = ("odoo.exceptions.ConcurrencyError", "psycopg2.", "psycopg.")

# Test / local-dev hook: an httpx transport (e.g. httpx.MockTransport with a fake Odoo).
# Production leaves it None (real network, TLS verified).
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


class OdooError(RuntimeError):
    """Base class. str() never contains credentials."""


class OdooTransient(OdooError):
    """Retry may help (network, timeout, 5xx, concurrency)."""


class OdooPermanent(OdooError):
    """Retry will not help (config, auth, access rights, validation)."""


class OdooRefused(OdooPermanent):
    """Call blocked by the client-side allowlist (ALLOWED / WRITE_FIELDS)."""


def normalize_email(email: str) -> str:
    """Lower-case, trimmed: matches Odoo's email_normalized for plain addresses."""
    return (email or "").strip().lower()


class OdooClient:
    """Minimal async JSON-RPC client. One instance per process (see sinks.py)."""

    def __init__(self, url: str, db: str, user: str, api_key: str, timeout: float = 20.0):
        if not all((url, db, user, api_key)):
            raise OdooPermanent("ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY must be set")
        if not url.startswith("https://"):
            raise OdooPermanent("ODOO_URL must be https (API key in the request body; MITM)")
        self.url = url.rstrip("/")
        self.db, self.user, self._key = db, user, api_key
        self.timeout = timeout
        self._uid: Optional[int] = None
        self._ids = itertools.count(1)

    @classmethod
    def from_env(cls) -> "OdooClient":
        return cls(
            os.environ.get("ODOO_URL", ""),
            os.environ.get("ODOO_DB", ""),
            os.environ.get("ODOO_USER", ""),
            os.environ.get("ODOO_API_KEY", ""),
            float(os.environ.get("ODOO_TIMEOUT", "20")),
        )

    def __repr__(self) -> str:  # never show the key
        return f"OdooClient(url={self.url!r}, db={self.db!r}, user={self.user!r})"

    # ------------------------------------------------------------------ transport

    async def _rpc(self, service: str, method: str, args: list, what: str) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call", "id": next(self._ids),
                   "params": {"service": service, "method": method, "args": args}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=TRANSPORT, verify=True) as client:
                resp = await client.post(f"{self.url}/jsonrpc", json=payload)
        except httpx.TimeoutException as exc:
            raise OdooTransient(f"{what}: timeout ({exc.__class__.__name__})") from None
        except httpx.TransportError as exc:  # DNS, refused, TLS handshake / certificate failure
            raise OdooTransient(f"{what}: network ({exc.__class__.__name__})") from None
        if resp.status_code >= 500 or resp.status_code == 429:
            raise OdooTransient(f"{what}: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise OdooPermanent(f"{what}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise OdooTransient(f"{what}: invalid JSON") from None
        err = body.get("error")
        if err:
            name = str((err.get("data") or {}).get("name") or "unknown")
            msg = str((err.get("data") or {}).get("message") or err.get("message") or "")[:160]
            text = f"{what}: {name}: {msg}"
            if name.startswith(_TRANSIENT_NAMES):
                raise OdooTransient(text)
            if name.startswith(_PERMANENT_PREFIXES):
                raise OdooPermanent(text)
            raise OdooTransient(text)
        return body.get("result")

    async def version(self) -> dict:
        """Server version (no login). Used by /health and the smoke script."""
        return await self._rpc("common", "version", [], "common.version")

    async def uid(self) -> int:
        if self._uid is None:
            uid = await self._rpc("common", "authenticate", [self.db, self.user, self._key, {}], "common.authenticate")
            if not uid:
                raise OdooPermanent("common.authenticate: login refused (ODOO_USER / ODOO_API_KEY)")
            self._uid = int(uid)
        return self._uid

    # ------------------------------------------------------------------ guarded calls

    def _guard(self, model: str, method: str, args: list) -> None:
        if (model, method) not in ALLOWED:
            raise OdooRefused(f"{model}.{method} not allowed for mauxy")
        if method == "write":
            allowed = WRITE_FIELDS.get(model, frozenset())
            vals = args[1] if len(args) > 1 and isinstance(args[1], dict) else None
            if vals is None or not vals or not set(vals) <= allowed:
                raise OdooRefused(f"{model}.write: only {sorted(allowed)} may be written")

    async def execute(self, model: str, method: str, args: list, kwargs: Optional[dict] = None) -> Any:
        self._guard(model, method, args)
        uid = await self.uid()
        return await self._rpc(
            "object", "execute_kw", [self.db, uid, self._key, model, method, args, kwargs or {}], f"{model}.{method}")

    async def search_read(self, model: str, domain: list, fields: list[str], *, limit: int = 0,
                          include_archived: bool = False) -> list[dict]:
        kw: dict[str, Any] = {"fields": fields, "context": {"active_test": not include_archived}}
        if limit:
            kw["limit"] = limit
        return await self.execute(model, "search_read", [domain], kw)
