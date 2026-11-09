"""
Sinks: where mauxy delivers confirmed website data (flywheel SPEC-021).

  lead sink        confirmed CTA (selfcheck, contact, inquiry, webinar, checkout) -> CRM lead
  subscriber sink  confirmed newsletter / Migration Radar subscription -> mailing list

Selected by env (12-factor), one each:
  LEAD_SINK        odoo (default) | none   (none: no CRM write, the notification mail carries the lead)
  SUBSCRIBER_SINK  odoo (default) | mautic (DEPRECATED, Mautic is being retired) | none
                   = the FALLBACK; a calling site may pin its own (sites.subscriber_sink, newsletter
                   authority: e.g. ludo -> mautic, simplify-erp -> odoo), see subscriber_sink(kind)

Contract (every sink):
  - called only AFTER the double opt-in (never on submit)
  - raises SinkTransient (retry may help) or SinkPermanent (it will not); never returns secrets
  - create-only on contacts: existing partners / leads / Mautic contacts are never merged

Odoo specifics (Odoo 18, erp.wapsol.de, dedicated bot user, see README "erp.wapsol setup"):
  lead: crm.lead type 'lead', user_id = ODOO_LEAD_USER_ID (Ashant), Ashant also follower,
        UTM source/medium/campaign mapped to EXISTING records by name (bot cannot create UTM),
        optional team (ODOO_LEAD_TEAM_ID) and tags (ODOO_LEAD_TAG_IDS),
        partner_id only when exactly ONE active partner has that e-mail; then email_from / phone
        are NOT sent (crm.lead's inverse would write them back onto the partner). Otherwise
        contact_name / partner_name / email_from / phone.
  subscriber: mailing.contact + mailing.subscription on ODOO_LIST_NEWSLETTER_ID / ODOO_LIST_RADAR_ID
        (fallback lists) or on the list ids of the site's routed odoo target (routing.py, list_id /
        list_ids arguments); opt-out = mailing.subscription.opt_out; blacklisted addresses
        (mail.blacklist) are skipped.
  per-site lead: a routed odoo target may override team_id, tag_ids, utm_source, utm_medium,
        utm_campaign (create_lead(..., overrides=)); missing keys = the env values above.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Literal, Optional, Protocol

from odoo_client import OdooClient, OdooPermanent, OdooTransient, normalize_email

logger = logging.getLogger("mauxy.sinks")

ListKey = Literal["newsletter", "radar"]
LIST_KEYS: tuple[str, ...] = ("newsletter", "radar")
CAMPAIGNS = ("selfcheck", "contact", "inquiry", "webinar", "checkout")


class SinkTransient(RuntimeError):
    """Temporary failure: retry with backoff."""


class SinkPermanent(RuntimeError):
    """Will not succeed by retrying (config, rights, validation)."""


# ---------------------------------------------------------------------------- protocols


class LeadSink(Protocol):
    name: str

    async def create_lead(self, lead: dict, *, maybe_exists: bool, overrides: Optional[dict] = None) -> Optional[int]:
        """Create ONE lead. maybe_exists=True on a retry: look for the lead of an earlier attempt
        (marker lead['ref']) first, so one confirmation never yields two leads. Returns the CRM id
        (None for the none sink)."""

    async def health(self) -> str: ...


class SubscriberSink(Protocol):
    name: str

    async def status(self, email: str, list_key: str) -> Literal["subscribed", "blocked", "none"]:
        """Read only. 'blocked' = DNC / blacklist (no DOI mail)."""

    async def subscribe(self, email: str, list_key: str, *, lang: str, source: str, page_url: str,
                        name: Optional[str] = None, company: Optional[str] = None,
                        segment: Optional[str] = None) -> Literal["ok", "already", "blocked"]:
        """segment = target of the calling site (per-site key registry, newsletter only).
        Mautic: segment alias (resolved / created). Odoo: segment not used; the list is the
        routed target's list id (list_id=, routing.py) or ODOO_LIST_<KEY>_ID (fallback)."""

    async def unsubscribe(self, email: str) -> tuple[str, Optional[str]]:
        """(result 'ok' | 'not_found' | 'error', reference or None)."""

    async def health(self) -> str: ...


# ---------------------------------------------------------------------------- helpers


def _env_int(name: str) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _env_ints(name: str) -> list[int]:
    return [int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x.isdigit()]


def text_to_html(text: str) -> str:
    """Plain text -> safe HTML for Odoo's Html field (escaped, line breaks kept)."""
    paras = [p for p in text.strip().split("\n\n")]
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paras)


class _wrap:
    """async with _wrap(): OdooTransient -> SinkTransient, OdooPermanent -> SinkPermanent."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, et, ev, tb):
        if ev is None:
            return False
        if isinstance(ev, OdooTransient):
            raise SinkTransient(str(ev)) from None
        if isinstance(ev, OdooPermanent):
            raise SinkPermanent(str(ev)) from None
        return False


# ---------------------------------------------------------------------------- Odoo lead sink


class OdooLeadSink:
    name = "odoo"

    def __init__(self, client: OdooClient):
        self.odoo = client
        self.user_id = _env_int("ODOO_LEAD_USER_ID")
        self.team_id = _env_int("ODOO_LEAD_TEAM_ID")
        self.tag_ids = _env_ints("ODOO_LEAD_TAG_IDS")
        self.utm_source = os.environ.get("ODOO_UTM_SOURCE", "simplify-erp.de")
        self.utm_medium = os.environ.get("ODOO_UTM_MEDIUM", "Website")
        self._utm: dict[tuple[str, str], Optional[int]] = {}
        self._user_partner: Optional[int] = None

    async def _utm_id(self, model: str, name: str) -> Optional[int]:
        """Existing UTM record by exact name (cached). Missing -> None + warning (bot has no create)."""
        key = (model, name)
        if key not in self._utm:
            rows = await self.odoo.search_read(model, [["name", "=", name]], ["id"], limit=1)
            self._utm[key] = rows[0]["id"] if rows else None
            if not rows:
                logger.warning("ODOO_UTM_MISSING model=%s name=%s (create it in Odoo)", model, name)
        return self._utm[key]

    async def _salesperson_partner(self) -> int:
        if self._user_partner is None:
            rows = await self.odoo.execute("res.users", "read", [[self.user_id]], {"fields": ["partner_id"]})
            if not rows or not rows[0].get("partner_id"):
                raise OdooPermanent(f"res.users {self.user_id} not readable (ODOO_LEAD_USER_ID)")
            self._user_partner = int(rows[0]["partner_id"][0])
        return self._user_partner

    async def match_partner(self, email: str) -> Optional[int]:
        """partner id when EXACTLY one active res.partner has this e-mail, else None."""
        norm = normalize_email(email)
        rows = await self.odoo.search_read("res.partner", [["email_normalized", "=", norm]], ["id", "email_normalized"], limit=2)
        rows = [r for r in rows if (r.get("email_normalized") or "").lower() == norm]
        return rows[0]["id"] if len(rows) == 1 else None

    async def _find_existing(self, ref: str) -> Optional[int]:
        rows = await self.odoo.search_read("crm.lead", [["description", "ilike", ref]], ["id"], limit=1, include_archived=True)
        return rows[0]["id"] if rows else None

    async def ensure_follower(self, lead_id: int) -> None:
        """Ashant follows the lead. Odoo usually auto-subscribes the salesperson on create; this
        covers the case where it did not (message_subscribe needs write on the bot's OWN leads)."""
        pid = await self._salesperson_partner()
        rows = await self.odoo.execute("crm.lead", "read", [[lead_id]], {"fields": ["message_partner_ids"]})
        if rows and pid in (rows[0].get("message_partner_ids") or []):
            return
        await self.odoo.execute("crm.lead", "message_subscribe", [[lead_id]], {"partner_ids": [pid]})

    async def build_vals(self, lead: dict, overrides: Optional[dict] = None) -> dict:
        """crm.lead values. overrides = params of the site's routed odoo target (team_id, tag_ids,
        utm_source, utm_medium, utm_campaign); missing keys fall back to the env settings."""
        o = overrides or {}
        team_id, tag_ids = o.get("team_id") or self.team_id, o.get("tag_ids") or self.tag_ids
        desc = lead["description"]
        if lead["ref"] not in desc:  # the retry search needs the marker in the description
            desc += f"\n\nReferenz: {lead['ref']}"
        vals: dict = {
            "name": lead["title"][:200],
            "type": "lead",
            "user_id": self.user_id,
            "description": text_to_html(desc),
        }
        if team_id:
            vals["team_id"] = team_id
        if tag_ids:
            vals["tag_ids"] = [[6, 0, list(tag_ids)]]
        for field, model, name in (("source_id", "utm.source", o.get("utm_source") or self.utm_source),
                                   ("medium_id", "utm.medium", o.get("utm_medium") or self.utm_medium),
                                   ("campaign_id", "utm.campaign", o.get("utm_campaign") or lead["campaign"])):
            rid = await self._utm_id(model, name)
            if rid:
                vals[field] = rid
        partner_id = await self.match_partner(lead["email"])
        if partner_id:
            # Linked: Odoo fills e-mail / phone from the partner. Do NOT send email_from / phone:
            # crm.lead's inverse would write them onto the existing partner (forbidden; the bot has
            # no write on res.partner anyway). The submitted phone is in the description.
            vals["partner_id"] = partner_id
            if lead.get("contact_name"):
                vals["contact_name"] = lead["contact_name"]
        else:
            vals["email_from"] = lead["email"]
            for k in ("contact_name", "partner_name", "phone"):
                if lead.get(k):
                    vals[k] = lead[k]
        return vals

    async def create_lead(self, lead: dict, *, maybe_exists: bool, overrides: Optional[dict] = None) -> Optional[int]:
        if not self.user_id:
            raise SinkPermanent("ODOO_LEAD_USER_ID is not set (salesperson)")
        async with _wrap():
            lead_id = await self._find_existing(lead["ref"]) if maybe_exists else None
            if lead_id is None:
                vals = await self.build_vals(lead, overrides)
                lead_id = int(await self.odoo.execute("crm.lead", "create", [vals]))
                logger.info("ODOO_LEAD_CREATED id=%s campaign=%s partner=%s", lead_id, lead["campaign"], bool(vals.get("partner_id")))
            await self.ensure_follower(lead_id)
            return lead_id

    async def health(self) -> str:
        async with _wrap():
            v = await self.odoo.version()
        return f"reachable ({v.get('server_version', '?')})"


# ---------------------------------------------------------------------------- Odoo subscriber sink


class OdooSubscriberSink:
    name = "odoo"

    def __init__(self, client: OdooClient):
        self.odoo = client
        self.lists = {"newsletter": _env_int("ODOO_LIST_NEWSLETTER_ID"), "radar": _env_int("ODOO_LIST_RADAR_ID")}

    def _list(self, key: str, list_id: Optional[int] = None) -> int:
        """The routed target's list id, else the fallback ODOO_LIST_<KEY>_ID."""
        lid = list_id or self.lists.get(key)
        if not lid:
            raise SinkPermanent(f"ODOO_LIST_{key.upper()}_ID is not set")
        return lid

    async def _blacklisted(self, norm: str) -> bool:
        return bool(await self.odoo.search_read("mail.blacklist", [["email", "=", norm]], ["id"], limit=1))

    async def _subs(self, norm: str, list_id: int) -> list[dict]:
        return await self.odoo.search_read(
            "mailing.subscription",
            [["list_id", "=", list_id], ["contact_id.email_normalized", "=", norm]], ["id", "opt_out", "contact_id"])

    async def status(self, email: str, list_key: str, *, list_id: Optional[int] = None):
        norm = normalize_email(email)
        async with _wrap():
            list_id = self._list(list_key, list_id)
            if await self._blacklisted(norm):
                return "blocked"
            if any(not s["opt_out"] for s in await self._subs(norm, list_id)):
                return "subscribed"
        return "none"

    async def subscribe(self, email, list_key, *, lang, source, page_url, name=None, company=None, segment=None,
                        list_id: Optional[int] = None):
        """After DOI. `segment` (site registry) is not used: one list per list_key (see protocol). Opted out earlier on this list -> the new confirmation re-subscribes (opt_out
        False); blacklist stays authoritative. Existing mailing.contact -> new subscription line
        only; the contact itself is never changed."""
        norm = normalize_email(email)
        async with _wrap():
            list_id = self._list(list_key, list_id)
            if await self._blacklisted(norm):
                return "blocked"
            subs = await self._subs(norm, list_id)
            if any(not s["opt_out"] for s in subs):
                return "already"
            if subs:
                await self.odoo.execute("mailing.subscription", "write", [[subs[0]["id"]], {"opt_out": False}])
                return "ok"
            contacts = await self.odoo.search_read("mailing.contact", [["email_normalized", "=", norm]], ["id"], limit=1)
            if contacts:
                await self.odoo.execute("mailing.subscription", "create", [{"contact_id": contacts[0]["id"], "list_id": list_id}])
            else:
                vals = {"name": (name or email)[:120], "email": norm, "subscription_ids": [[0, 0, {"list_id": list_id}]]}
                if company:
                    vals["company_name"] = company[:160]
                await self.odoo.execute("mailing.contact", "create", [vals])
        logger.info("ODOO_SUBSCRIBED list=%s source=%s", list_key, source)
        return "ok"

    async def dnc(self, email: str, list_ids: list[int]) -> Optional[str]:
        """Do-not-contact for routed sites: 'blacklist' (hard), 'optout' (opted out on any of
        `list_ids`; soft only when mauxy's own unsubscribe wrote it, routing.dnc_status) or None."""
        norm = normalize_email(email)
        async with _wrap():
            if await self._blacklisted(norm):
                return "blacklist"
            if list_ids and await self.odoo.search_read(
                    "mailing.subscription", [["list_id", "in", list_ids], ["contact_id.email_normalized", "=", norm],
                                             ["opt_out", "=", True]], ["id"], limit=1):
                return "optout"
        return None

    async def unsubscribe(self, email: str, *, list_ids: Optional[list[int]] = None):
        """Opt-out on every given list (routed: all lists the site's odoo targets hold), else the
        fallback lists."""
        norm = normalize_email(email)
        list_ids = [lid for lid in (list_ids or self.lists.values()) if lid]
        if not list_ids:
            raise SinkPermanent("no ODOO_LIST_*_ID configured")
        async with _wrap():
            subs = await self.odoo.search_read(
                "mailing.subscription",
                [["list_id", "in", list_ids], ["contact_id.email_normalized", "=", norm], ["opt_out", "=", False]], ["id"])
            if not subs:
                return "not_found", None
            ids = [s["id"] for s in subs]
            await self.odoo.execute("mailing.subscription", "write", [ids, {"opt_out": True}])
        return "ok", ",".join(str(i) for i in ids)

    async def health(self) -> str:
        async with _wrap():
            v = await self.odoo.version()
        return f"reachable ({v.get('server_version', '?')})"


# ---------------------------------------------------------------------------- none sinks


class NoLeadSink:
    """LEAD_SINK=none: no CRM write; lead_sync mails the lead to LEAD_NOTIFY_TO instead."""
    name = "none"

    async def create_lead(self, lead: dict, *, maybe_exists: bool, overrides: Optional[dict] = None) -> Optional[int]:
        return None

    async def health(self) -> str:
        return "disabled"


class NoSubscriberSink:
    name = "none"

    async def status(self, email, list_key):
        return "none"

    async def subscribe(self, email, list_key, **kw):
        raise SinkPermanent("SUBSCRIBER_SINK=none: subscriptions are not stored anywhere")

    async def unsubscribe(self, email):
        return "not_found", None

    async def health(self) -> str:
        return "disabled"


# ---------------------------------------------------------------------------- factory

SUBSCRIBER_KINDS = ("odoo", "mautic", "none")

_client: Optional[OdooClient] = None
_lead: Optional[LeadSink] = None
_odoo_lead: Optional["OdooLeadSink"] = None
_subscribers: dict[str, SubscriberSink] = {}


def odoo() -> OdooClient:
    global _client
    if _client is None:
        _client = OdooClient.from_env()
    return _client


def lead_sink() -> LeadSink:
    global _lead
    if _lead is None:
        kind = os.environ.get("LEAD_SINK", "odoo").strip().lower()
        if kind == "none":
            _lead = NoLeadSink()
        elif kind == "odoo":
            try:
                _lead = OdooLeadSink(odoo())
            except OdooPermanent as exc:
                logger.error("LEAD_SINK=odoo misconfigured: %s", exc)
                raise SinkPermanent(str(exc)) from None
        else:
            raise SinkPermanent(f"unknown LEAD_SINK={kind}")
    return _lead


def odoo_lead_sink() -> "OdooLeadSink":
    """The Odoo lead sink regardless of LEAD_SINK (a site's routed odoo target)."""
    global _odoo_lead
    if _odoo_lead is None:
        try:
            _odoo_lead = OdooLeadSink(odoo())
        except OdooPermanent as exc:
            raise SinkPermanent(str(exc)) from None
    return _odoo_lead


def default_subscriber_kind() -> str:
    """Global SUBSCRIBER_SINK (fallback for sites without their own)."""
    return os.environ.get("SUBSCRIBER_SINK", "odoo").strip().lower()


def subscriber_sink(kind: Optional[str] = None) -> SubscriberSink:
    """Subscriber sink of `kind` (a site's pinned sink) or the global SUBSCRIBER_SINK; cached per kind."""
    kind = (kind or default_subscriber_kind()).strip().lower()
    if kind not in _subscribers:
        if kind == "none":
            _subscribers[kind] = NoSubscriberSink()
        elif kind == "mautic":
            import mautic_sink  # deprecated, only loaded on demand
            logger.warning("SUBSCRIBER_SINK=mautic is DEPRECATED (Mautic is being retired)")
            _subscribers[kind] = mautic_sink.MauticSubscriberSink()
        elif kind == "odoo":
            try:
                _subscribers[kind] = OdooSubscriberSink(odoo())
            except OdooPermanent as exc:
                logger.error("SUBSCRIBER_SINK=odoo misconfigured: %s", exc)
                raise SinkPermanent(str(exc)) from None
        else:
            raise SinkPermanent(f"unknown SUBSCRIBER_SINK={kind}")
    return _subscribers[kind]


def reset() -> None:
    """Forget the cached sinks / client (tests, config reload)."""
    global _client, _lead, _odoo_lead
    _client = _lead = _odoo_lead = None
    _subscribers.clear()


async def health(site_kinds: tuple[str, ...] = ()) -> dict:
    """Per sink: 'reachable (..)' / 'disabled' / 'error: ..'. Never raises. `site_kinds` = sinks
    pinned by sites besides the global one (reported as subscriber_sink_<kind>)."""
    out = {}
    checks = [("lead_sink", lead_sink), ("subscriber_sink", subscriber_sink)]
    checks += [(f"subscriber_sink_{k}", lambda k=k: subscriber_sink(k))
               for k in sorted(set(site_kinds)) if k != default_subscriber_kind()]
    for label, getter in checks:
        try:
            sink = getter()
            out[label] = f"{sink.name}: {await sink.health()}"
        except (SinkTransient, SinkPermanent) as exc:
            out[label] = f"error: {str(exc)[:120]}"
    return out
