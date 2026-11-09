"""
DEPRECATED Mautic subscriber sink (SUBSCRIBER_SINK=mautic). Mautic is being retired (owner
decision 2026-11-08); the default subscriber sink is Odoo Email Marketing (sinks.py). Kept so the
switch can be rolled back by env until Mautic is shut down; delete this file with Mautic.

Behaviour is the pre-2026-11-08 one (tests T-SEC-0xx, T-SC-120/122 run against it):
  status      read the contact; tag newsletter_confirmed / radar tag -> subscribed; DNC -> blocked
  subscribe   after DOI only: upsert contact with tags + consent fields; DNC -> blocked;
              with the calling site's `segment` (site registry, upstream newsletter authority):
              also add the contact to that segment (alias, created when missing). mauxy owns the
              DOI, so a Mautic DOI campaign on that segment must be switched off (double mail).
  unsubscribe add the contact to the e-mail DNC list
Credentials (MAUTIC_BASE_URL / MAUTIC_USERNAME / MAUTIC_PASSWORD) from env, never logged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from sinks import SinkPermanent, SinkTransient

logger = logging.getLogger("mauxy.mautic")

MAUTIC_BASE_URL = os.environ.get("MAUTIC_BASE_URL", "")
MAUTIC_USERNAME = os.environ.get("MAUTIC_USERNAME", "")
MAUTIC_PASSWORD = os.environ.get("MAUTIC_PASSWORD", "")
MAUTIC_RADAR_TAG = os.environ.get("MAUTIC_RADAR_TAG", "migration_radar_confirmed")
MAUTIC_RADAR_SEGMENT_ID = os.environ.get("MAUTIC_RADAR_SEGMENT_ID", "")

# list key -> (tag that marks "subscribed", tags removed on subscribe, extra fields)
LIST_TAGS = {"newsletter": "newsletter_confirmed", "radar": MAUTIC_RADAR_TAG}


def _auth() -> tuple[str, str]:
    return (MAUTIC_USERNAME, MAUTIC_PASSWORD)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def search_contact(client: httpx.AsyncClient, email: str) -> tuple[int, dict | None]:
    """Exact-match contact by e-mail. (status, contact or None); contact None on non-200."""
    resp = await client.get(
        f"{MAUTIC_BASE_URL}/api/contacts",
        params={"where[0][col]": "email", "where[0][expr]": "eq", "where[0][val]": email},
        auth=_auth(),
    )
    if resp.status_code != 200:
        return resp.status_code, None
    contacts = resp.json().get("contacts", {}) or {}
    for cid, cdata in contacts.items():
        core = cdata.get("fields", {}).get("core", {})
        if (core.get("email", {}).get("value") or "").lower() == email:
            return 200, {"id": cid, **cdata}
    return 200, None


def contact_is_dnc(contact: dict) -> bool:
    return any(
        (entry.get("channel") == "email") and int(entry.get("reason", 0)) != 0
        for entry in (contact.get("doNotContact") or [])
    )


def contact_has_tag(contact: dict, tag: str) -> bool:
    for t in contact.get("tags") or []:
        if (t.get("tag") if isinstance(t, dict) else t) == tag:
            return True
    return False


async def upsert_contact(client: httpx.AsyncClient, *, existing_id: str | None, email: str,
                         payload_fields: dict, add_tags: list[str],
                         remove_tags: list[str] | None = None) -> tuple[int, dict | None]:
    """Create or patch a contact ('-tag' removes a tag). (status, contact json or None)."""
    body: dict = {"email": email, **payload_fields}
    tags = list(add_tags) + [f"-{t}" for t in (remove_tags or [])]
    if tags:
        body["tags"] = tags
    if existing_id:
        resp = await client.patch(f"{MAUTIC_BASE_URL}/api/contacts/{existing_id}/edit", json=body, auth=_auth())
    else:
        resp = await client.post(f"{MAUTIC_BASE_URL}/api/contacts/new", json=body, auth=_auth())
    if resp.status_code not in (200, 201):
        return resp.status_code, None
    return resp.status_code, resp.json().get("contact")


async def resolve_segment_id(client: httpx.AsyncClient, alias: str) -> tuple[str | None, str | None]:
    """Mautic segment id by alias, created when absent (from upstream main.py, newsletter
    authority). (segment_id, None) or (None, error detail)."""
    resp = await client.get(f"{MAUTIC_BASE_URL}/api/segments", params={"limit": 200}, auth=_auth())
    if resp.status_code != 200:
        return None, f"segments_list_status={resp.status_code}"
    lists = resp.json().get("lists", {})
    items = lists.values() if isinstance(lists, dict) else (lists or [])
    for item in items:
        if (item.get("alias") or "").lower() == alias.lower():
            return str(item.get("id")), None
    created = await client.post(
        f"{MAUTIC_BASE_URL}/api/segments/new",
        json={"name": alias, "alias": alias, "isPublished": True}, auth=_auth())
    if created.status_code in (200, 201):
        new_id = created.json().get("list", {}).get("id")
        logger.info("SEGMENT_CREATED alias=%s id=%s", alias, new_id)
        return str(new_id), None
    return None, f"segment_create_status={created.status_code}"


async def add_to_segment_now(email: str, segment: str) -> tuple[str, Optional[str], Optional[str]]:
    """Upstream newsletter-authority subscribe, unchanged, for sites with doi_owner='sink' (ludo):
    find/create the contact and add it to the site's segment AT ONCE; a Mautic campaign on that
    segment runs the opt-in. Returns (result, contact_id, error_detail), result one of
    ok | unreachable (-> 503) | error (-> 502) | unexpected (-> 500)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            status, contact = await search_contact(client, email)
            if status != 200:
                return "unreachable", None, f"search_status={status}"
            if contact is None:
                created = await client.post(f"{MAUTIC_BASE_URL}/api/contacts/new", json={"email": email}, auth=_auth())
                if created.status_code not in (200, 201):
                    return "error", None, f"contact_create_status={created.status_code}"
                contact_id = str(created.json().get("contact", {}).get("id"))
            else:
                contact_id = str(contact["id"])
            segment_id, err = await resolve_segment_id(client, segment)
            if segment_id is None:
                return "error", contact_id, err
            add = await client.post(f"{MAUTIC_BASE_URL}/api/segments/{segment_id}/contact/{contact_id}/add", auth=_auth())
            if add.status_code in (200, 201):
                return "ok", contact_id, None
            return "error", contact_id, f"segment_add_status={add.status_code}"
    except httpx.RequestError as exc:
        return "unreachable", None, f"httpx_error: {exc.__class__.__name__}"
    except Exception as exc:  # upstream: 500 on anything unexpected
        return "unexpected", None, f"unexpected: {exc.__class__.__name__}"


class MauticSubscriberSink:
    name = "mautic"

    async def _contact(self, client: httpx.AsyncClient, email: str) -> dict | None:
        try:
            status, contact = await search_contact(client, email)
        except httpx.RequestError as exc:
            raise SinkTransient(f"mautic search: {exc.__class__.__name__}") from None
        if status != 200:
            raise SinkTransient(f"mautic search_status={status}")
        return contact

    async def status(self, email: str, list_key: str):
        async with httpx.AsyncClient(timeout=15.0) as client:
            contact = await self._contact(client, email)
        if contact and contact_has_tag(contact, LIST_TAGS[list_key]):
            return "subscribed"
        if contact and contact_is_dnc(contact):
            return "blocked"
        return "none"

    async def dnc(self, email: str) -> bool:
        """Do-not-contact (DNC on the e-mail channel), whatever the tags say."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            contact = await self._contact(client, email)
        return bool(contact and contact_is_dnc(contact))

    async def subscribe(self, email, list_key, *, lang, source, page_url, name=None, company=None, segment=None):
        fields = {"newsletter_locale": lang, "newsletter_source": source,
                  "newsletter_consented_at": _now_iso(), "newsletter_page_url": page_url}
        remove: list[str] = []
        if list_key == "newsletter":
            fields = {"confirmation_token": "", **fields}  # clears tokens of the pre-2026-11 flow
            remove = ["newsletter_pending"]
        async with httpx.AsyncClient(timeout=15.0) as client:
            contact = await self._contact(client, email)
            if contact and contact_is_dnc(contact):
                return "blocked"
            try:
                status, upserted = await upsert_contact(
                    client, existing_id=str(contact["id"]) if contact else None, email=email,
                    payload_fields=fields, add_tags=[LIST_TAGS[list_key]], remove_tags=remove)
                if not upserted:
                    raise SinkTransient(f"mautic upsert_status={status}")
                if segment:  # calling site's segment (newsletter, site registry)
                    seg_id, err = await resolve_segment_id(client, segment)
                    if seg_id is None:
                        raise SinkTransient(f"mautic {err}")
                    resp = await client.post(
                        f"{MAUTIC_BASE_URL}/api/segments/{seg_id}/contact/{upserted.get('id')}/add", auth=_auth())
                    if resp.status_code not in (200, 201):
                        raise SinkTransient(f"mautic segment_status={resp.status_code}")
                if list_key == "radar" and MAUTIC_RADAR_SEGMENT_ID:
                    resp = await client.post(
                        f"{MAUTIC_BASE_URL}/api/segments/{MAUTIC_RADAR_SEGMENT_ID}/contact/{upserted.get('id')}/add",
                        auth=_auth())
                    if resp.status_code not in (200, 201):
                        raise SinkTransient(f"mautic segment_status={resp.status_code}")
            except httpx.RequestError as exc:
                raise SinkTransient(f"mautic upsert: {exc.__class__.__name__}") from None
        return "ok"

    async def unsubscribe(self, email: str) -> tuple[str, Optional[str]]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            contact = await self._contact(client, email)  # SinkTransient -> 503 at the endpoint
            if not contact:
                return "not_found", None
            cid = str(contact["id"])
            try:
                for attempt in (1, 2):
                    resp = await client.post(
                        f"{MAUTIC_BASE_URL}/api/contacts/{cid}/dnc/email/add",
                        json={"reason": 1, "comments": "Unsubscribed via website"}, auth=_auth())
                    if resp.status_code in (200, 201):
                        return "ok", cid
                    logger.warning("UNSUBSCRIBE_DNC_FAILED status=%s attempt=%d", resp.status_code, attempt)
            except httpx.RequestError as exc:
                logger.error("UNSUBSCRIBE_DNC_REQUEST_ERROR error=%s", exc.__class__.__name__)
        return "error", cid

    async def health(self) -> str:
        if not MAUTIC_BASE_URL:
            raise SinkPermanent("MAUTIC_BASE_URL is not set")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{MAUTIC_BASE_URL}/api/contacts?limit=1", auth=_auth())
        except httpx.RequestError as exc:
            raise SinkTransient(f"connection error: {exc.__class__.__name__}") from None
        if resp.status_code != 200:
            raise SinkTransient(f"HTTP {resp.status_code}")
        return "reachable"
