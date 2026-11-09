"""
Per-site action routing ("hooks", owner requirement 2026-11-09): for each site and action a
configured, ORDERED list of targets; each target = a sink type + its params. Website -> mauxy ->
every target of that site (erp.wapsol, Mautic, the flywheel, other systems).

Actions (ACTIONS below is the one extension point: add an action + its allowed sinks there and
emit it with routing.fan_out at the place where it happens):
  subscribe    newsletter subscription confirmed (DOI click; also the ticked box of a CTA lead)
  unsubscribe  newsletter opt-out (POST /api/unsubscribe)
  lead         CTA lead confirmed (contact, inquiry, webinar, checkout)
  selfcheck    Selbst-Check confirmed (picks mail / call -> CRM lead, radar -> Migration Radar list)

Sinks and params (validated on write, admin CRUD below):
  odoo     erp.wapsol. {"lists": {"newsletter": id, "radar": id}, "team_id": id, "tag_ids": [ids],
           "utm_source": name, "utm_medium": name, "utm_campaign": name} -- all optional; missing
           = env fallback (ODOO_LIST_*_ID, ODOO_LEAD_*, ODOO_UTM_*). lead / selfcheck: at most ONE
           odoo target (one confirmation = at most one crm.lead).
  mautic   DEPRECATED. {"segment": alias} (subscribe; default = the site's registry segment).
  webhook  signed HTTPS POST (webhooks.py). {"url": https URL on WEBHOOK_ALLOWED_HOSTS,
           "secret_ref": name -> env WEBHOOK_SECRET_<NAME>}.

No (enabled) target for (site, action) = the fallback of before 0006: sites.subscriber_sink /
SUBSCRIBER_SINK / LEAD_SINK with ODOO_LIST_*_ID. Routes REPLACE the fallback for that action.

Delivery: fan-out through the outbox `route_delivery`, ONE row per (event, target), each with its
own state / attempts / backoff (lead_sync.backoff, LEAD_SYNC_MAX_ATTEMPTS), resumed after a
restart: a failing target neither blocks nor repeats the others. idem_key = sha256(action | event
| target identity): enqueueing the same event twice is a no-op, a webhook receiver sees the same
Idempotency-Key on every retry. Exception, for the CRM semantics: the ONE odoo target of lead /
selfcheck rides in the existing lead_sync row (crm.lead with its duplicate guard + list
subscriptions + the operator mails), its params stored in lead_sync.target.
Permanent failure / retries exhausted -> mail to LEAD_NOTIFY_TO (action, site, target, address);
POST /api/admin/deliveries/{id}/retry after fixing the cause. payload (contains the address) is
cleared once delivered; rows deleted LEAD_RETENTION_DAYS after done / failed.

Unsubscribe (compliance): reaches EVERY target that may hold the address for that site = explicit
unsubscribe targets + all subscribe targets + list-holding odoo / mautic targets of lead /
selfcheck + all webhooks of the site + the site's fallback subscriber sink (subscribers from
before the routes). The odoo target opts out of the union of all those list ids. A site without
any route keeps the synchronous fallback unsubscribe (and its 503 when the sink is down).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

import jobs
import lead_sync
import mailer
import sinks
import webhooks

logger = logging.getLogger("mauxy.routing")

ACTIONS: dict[str, frozenset] = {
    "subscribe": frozenset({"odoo", "mautic", "webhook"}),
    "unsubscribe": frozenset({"odoo", "mautic", "webhook"}),
    "lead": frozenset({"odoo", "webhook"}),
    "selfcheck": frozenset({"odoo", "mautic", "webhook"}),
}
SINKS = ("odoo", "mautic", "webhook")
MAX_TARGETS = 10
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
UTM_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


@dataclass(frozen=True)
class Target:
    position: int
    sink: str
    params: dict = field(default_factory=dict)
    enabled: bool = True

    def identity(self) -> str:
        """What makes two targets the same receiver (idempotency key, de-duplication)."""
        if self.sink == "webhook":
            return f"webhook:{self.params.get('url', '')}"
        if self.sink == "mautic":
            return f"mautic:{self.params.get('segment', '')}"
        return "odoo"


_ROUTES: dict[tuple[str, str], list[Target]] = {}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------------------- cache


async def load(db: aiosqlite.Connection) -> None:
    global _ROUTES
    routes: dict[tuple[str, str], list[Target]] = {}
    async with db.execute("SELECT site, action, position, sink, params, enabled FROM site_route "
                          "ORDER BY site, action, position") as cur:
        for site, action, position, sink, params, enabled in await cur.fetchall():
            try:
                p = json.loads(params or "{}")
            except json.JSONDecodeError:
                logger.error("ROUTE_BAD_PARAMS site=%s action=%s position=%s -- skipped", site, action, position)
                continue
            routes.setdefault((site, action), []).append(Target(position, sink, p, bool(enabled)))
    _ROUTES = routes


def targets(site: Optional[str], action: str) -> list[Target]:
    """Enabled targets of (site, action), in order. [] = fallback sinks."""
    return [t for t in _ROUTES.get((site or "", action), []) if t.enabled]


def all_rows(site: Optional[str] = None) -> list[dict]:
    return [{"site": s, "action": a, "position": t.position, "sink": t.sink, "params": t.params, "enabled": t.enabled}
            for (s, a), ts in sorted(_ROUTES.items()) if site is None or s == site for t in ts]


def count() -> int:
    return sum(len(v) for v in _ROUTES.values())


# ------------------------------------------------------------------------------- validation


class RouteInvalid(ValueError):
    pass


def _pos_int(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2 ** 31:
        raise RouteInvalid(f"{what}: positive integer")
    return v


def check_params(action: str, sink: str, params) -> dict:
    """Normalised params of one target. Raises RouteInvalid (message safe to return)."""
    if not isinstance(params, dict):
        raise RouteInvalid("params: object")
    if sink == "odoo":
        allowed = {"lists", "team_id", "tag_ids", "utm_source", "utm_medium", "utm_campaign"}
        if extra := set(params) - allowed:
            raise RouteInvalid(f"odoo params: unknown {sorted(extra)}")
        out: dict = {}
        if "lists" in params:
            lists = params["lists"]
            if not isinstance(lists, dict) or set(lists) - set(sinks.LIST_KEYS):
                raise RouteInvalid(f"odoo lists: keys {list(sinks.LIST_KEYS)}")
            out["lists"] = {k: _pos_int(v, f"lists.{k}") for k, v in lists.items()}
        if "team_id" in params:
            out["team_id"] = _pos_int(params["team_id"], "team_id")
        if "tag_ids" in params:
            tags = params["tag_ids"]
            if not isinstance(tags, list) or len(tags) > 20:
                raise RouteInvalid("tag_ids: list of <= 20 ids")
            out["tag_ids"] = [_pos_int(t, "tag_ids") for t in tags]
        for k in ("utm_source", "utm_medium", "utm_campaign"):
            if k in params:
                if not isinstance(params[k], str) or not UTM_RE.match(params[k]):
                    raise RouteInvalid(f"{k}: 1..80 characters")
                out[k] = params[k]
        if action in ("subscribe", "unsubscribe") and set(out) - {"lists"}:
            raise RouteInvalid(f"odoo params for {action}: only lists")
        return out
    if sink == "mautic":
        if extra := set(params) - {"segment"}:
            raise RouteInvalid(f"mautic params: unknown {sorted(extra)}")
        if "segment" in params and not (isinstance(params["segment"], str) and SEGMENT_RE.match(params["segment"])):
            raise RouteInvalid("segment: [A-Za-z0-9_-]{1,80}")
        return {k: params[k] for k in ("segment",) if k in params}
    if sink == "webhook":
        if set(params) != {"url", "secret_ref"}:
            raise RouteInvalid("webhook params: exactly url and secret_ref")
        try:
            webhooks.check_url(params["url"])
            webhooks.secret_for(params["secret_ref"])
        except webhooks.WebhookConfigError as exc:
            raise RouteInvalid(str(exc)) from None
        return {"url": params["url"], "secret_ref": params["secret_ref"]}
    raise RouteInvalid(f"unknown sink {sink!r}")


def check_targets(action: str, raw: list) -> list[Target]:
    if action not in ACTIONS:
        raise RouteInvalid(f"unknown action {action!r} (known: {sorted(ACTIONS)})")
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_TARGETS:
        raise RouteInvalid(f"targets: 1..{MAX_TARGETS}")
    out: list[Target] = []
    for i, t in enumerate(raw, start=1):
        if not isinstance(t, dict) or set(t) - {"sink", "params", "enabled"} or "sink" not in t:
            raise RouteInvalid(f"target {i}: {{sink, params?, enabled?}}")
        sink = t["sink"]
        if sink not in ACTIONS[action]:
            raise RouteInvalid(f"target {i}: sink {sink!r} not allowed for {action} (allowed: {sorted(ACTIONS[action])})")
        enabled = t.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RouteInvalid(f"target {i}: enabled must be a boolean")
        out.append(Target(i, sink, check_params(action, sink, t.get("params", {})), enabled))
    ids = [t.identity() for t in out]
    if len(ids) != len(set(ids)):
        raise RouteInvalid("the same receiver twice")
    if action in ("lead", "selfcheck") and sum(t.sink == "odoo" for t in out) > 1:
        raise RouteInvalid(f"{action}: at most one odoo target (one confirmation = at most one crm.lead)")
    return out


# ------------------------------------------------------------------------------- effective targets


def odoo_target(site: Optional[str], action: str) -> Optional[Target]:
    return next((t for t in targets(site, action) if t.sink == "odoo"), None)


def crm_target(site: Optional[str], action: str) -> Optional[dict]:
    """lead_sync.target of a routed lead / selfcheck: its odoo target, else 'none' (no CRM write;
    the lead then goes to LEAD_NOTIFY_TO by mail). None = not routed (fallback LEAD_SINK)."""
    if not targets(site, action):
        return None
    t = odoo_target(site, action)
    return {"sink": "odoo", "params": t.params} if t else {"sink": "none", "params": {}}


def _env_list_ids() -> list[int]:
    return [i for i in (sinks._env_int("ODOO_LIST_NEWSLETTER_ID"), sinks._env_int("ODOO_LIST_RADAR_ID")) if i]


def unsubscribe_targets(site: Optional[str], fallback_kind: Optional[str]) -> Optional[list[Target]]:
    """Every receiver that may hold the address for this site (module doc). None = the site has
    no route at all -> synchronous fallback unsubscribe (behaviour before 0006)."""
    groups = {a: targets(site, a) for a in ACTIONS}
    if not any(groups.values()):
        return None
    kind = (fallback_kind or sinks.default_subscriber_kind()).strip().lower()
    lists: set[int] = set()
    want_odoo = kind == "odoo"
    mautic_segments: set[str] = set()
    hooks: dict[str, Target] = {}
    if kind == "odoo":
        lists.update(_env_list_ids())
    if kind == "mautic":
        mautic_segments.add("")
    for action, ts in groups.items():
        for t in ts:
            if t.sink == "odoo":
                want_odoo = True
                lists.update(t.params.get("lists", {}).values())
            elif t.sink == "mautic":
                mautic_segments.add("")  # Mautic DNC is per contact, one call covers every segment
            elif t.sink == "webhook":
                hooks.setdefault(t.identity(), t)
    out: list[Target] = []
    if want_odoo:
        out.append(Target(len(out) + 1, "odoo", {"list_ids": sorted(lists or set(_env_list_ids()))}))
    if mautic_segments:
        out.append(Target(len(out) + 1, "mautic", {}))
    for t in hooks.values():
        out.append(Target(len(out) + 1, "webhook", dict(t.params)))
    return out


async def status(site: Optional[str], email: str) -> str:
    """Read-only pre-check before a newsletter DOI mail on a routed site: the first list-holding
    target ('subscribed' | 'blocked' | 'none'); webhooks cannot answer -> 'none'."""
    for t in targets(site, "subscribe"):
        if t.sink == "odoo":
            return await sinks.subscriber_sink("odoo").status(
                email, "newsletter", list_id=t.params.get("lists", {}).get("newsletter"))
        if t.sink == "mautic":
            return await sinks.subscriber_sink("mautic").status(email, "newsletter")
    return "none"


# ------------------------------------------------------------------------------- outbox


def idem_key(action: str, event_ref: str, target: Target) -> str:
    return hashlib.sha256(f"{action}|{event_ref}|{target.identity()}".encode()).hexdigest()


def new_event(prefix: str) -> str:
    return f"{prefix}:{secrets.token_hex(8)}"


_running: set[int] = set()


async def fan_out(db: aiosqlite.Connection, *, site: str, action: str, event_ref: str, data: dict,
                  to: list[Target]) -> list[int]:
    """One outbox row per target (INSERT OR IGNORE on the idempotency key), then start each."""
    ids = []
    for t in to:
        if t.sink == "odoo" and action in ("lead", "selfcheck"):
            continue  # rides in lead_sync (crm.lead semantics), see crm_target
        cur = await db.execute(
            "INSERT OR IGNORE INTO route_delivery (idem_key, event_ref, site, action, position, sink, params, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (idem_key(action, event_ref, t), event_ref, site, action, t.position, t.sink,
             json.dumps(t.params, sort_keys=True), json.dumps(data, ensure_ascii=False), iso(now())))
        if cur.rowcount:
            ids.append(cur.lastrowid)
        else:
            logger.info("ROUTE_DUPLICATE_IGNORED action=%s event=%s sink=%s", action, event_ref, t.sink)
    await db.commit()
    for did in ids:
        start(db, did)
    if ids:
        logger.info("ROUTE_FANOUT site=%s action=%s event=%s targets=%d", site, action, event_ref, len(ids))
    return ids


def start(db: aiosqlite.Connection, did: int) -> None:
    if did in _running:
        return
    _running.add(did)
    jobs.spawn(_run_guarded(db, did), name=f"route:{did}")


async def _run_guarded(db: aiosqlite.Connection, did: int) -> None:
    try:
        await run(db, did)
    finally:
        _running.discard(did)


async def _row(db: aiosqlite.Connection, did: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM route_delivery WHERE id = ?", (did,)) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def _execute(row: dict) -> str:
    """One attempt at one target. Returns the result word. Raises SinkTransient / SinkPermanent."""
    action, sink, site = row["action"], row["sink"], row["site"]
    params, data = json.loads(row["params"]), json.loads(row["payload"] or "{}")
    email = data.get("email", "")
    if sink == "webhook":
        status = await webhooks.deliver(params, idem_key=row["idem_key"], action=action, site=site,
                                        data=data, occurred_at=row["created_at"])
        return f"delivered ({status})"
    common = dict(lang=data.get("locale") or data.get("lang") or "de", source=data.get("source") or action,
                  page_url=data.get("page_url") or "", name=data.get("name"), company=data.get("company"))
    if sink == "odoo":
        s = sinks.subscriber_sink("odoo")
        if action == "subscribe":
            return await s.subscribe(email, "newsletter", **common,
                                     list_id=params.get("lists", {}).get("newsletter"))
        if action == "unsubscribe":
            result, _ref = await s.unsubscribe(email, list_ids=params.get("list_ids"))
            return result
    if sink == "mautic":
        s = sinks.subscriber_sink("mautic")
        if action == "subscribe":
            return await s.subscribe(email, "newsletter", **common, segment=params.get("segment") or data.get("segment") or None)
        if action == "unsubscribe":
            result, _ref = await s.unsubscribe(email)
            if result == "error":
                raise sinks.SinkTransient("mautic dnc failed")
            return result
        if action == "selfcheck":
            if "radar" not in (data.get("picks") or []):
                return "skipped (no radar pick)"
            return await s.subscribe(email, "radar", **common)
    raise sinks.SinkPermanent(f"{sink} cannot handle {action}")


async def run(db: aiosqlite.Connection, did: int) -> None:
    while True:
        row = await _row(db, did)
        if row is None or row["state"] != "queued":
            return
        if row["next_attempt_at"]:
            wait = (datetime.fromisoformat(row["next_attempt_at"]) - now()).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)
        try:
            result = await _execute(row)
        except sinks.SinkTransient as exc:
            attempts = row["attempts"] + 1
            if attempts < lead_sync.MAX_ATTEMPTS:
                nxt = now() + timedelta(seconds=lead_sync.backoff(attempts))
                await db.execute("UPDATE route_delivery SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                                 (attempts, str(exc)[:300], iso(nxt), did))
                await db.commit()
                logger.warning("ROUTE_RETRY id=%s sink=%s attempt=%d error=%s", did, row["sink"], attempts, str(exc)[:200])
                continue
            await _finish(db, did, "failed", attempts=attempts, error=f"retries exhausted: {exc}")
            return
        except sinks.SinkPermanent as exc:
            await _finish(db, did, "failed", attempts=row["attempts"] + 1, error=str(exc))
            return
        except Exception as exc:  # a bug in one target must not kill the job silently
            await _finish(db, did, "failed", attempts=row["attempts"] + 1, error=f"unexpected: {exc.__class__.__name__}")
            return
        await _finish(db, did, "done", attempts=row["attempts"] + 1, error=None, result=str(result)[:80])
        return


async def _finish(db: aiosqlite.Connection, did: int, outcome: str, *, attempts: int, error: Optional[str],
                  result: Optional[str] = None) -> None:
    await db.execute("UPDATE route_delivery SET state = ?, attempts = ?, last_error = ?, result = ?, done_at = ?, "
                     "next_attempt_at = NULL WHERE id = ?",
                     (outcome, attempts, error[:300] if error else None, result, iso(now()), did))
    if outcome == "done":
        await db.execute("UPDATE route_delivery SET payload = NULL WHERE id = ?", (did,))
    await db.commit()
    row = await _row(db, did)
    if outcome == "failed":
        logger.error("ROUTE_FAILED id=%s site=%s action=%s sink=%s error=%s", did, row["site"], row["action"],
                     row["sink"], (error or "")[:200])
        if await mail_failure(row):
            await db.execute("UPDATE route_delivery SET failure_mailed = 1 WHERE id = ?", (did,))
            await db.commit()
    else:
        logger.info("ROUTE_DONE id=%s site=%s action=%s sink=%s result=%s", did, row["site"], row["action"],
                    row["sink"], result)


def _target_label(row: dict) -> str:
    params = json.loads(row["params"])
    if row["sink"] == "webhook":
        return f"webhook {params.get('url')}"  # no secret: only the env reference is stored
    if row["sink"] == "odoo":
        return f"odoo {json.dumps(params, sort_keys=True)}"
    return f"mautic {params.get('segment') or ''}".strip()


async def mail_failure(row: dict) -> bool:
    if not lead_sync.NOTIFY_TO:
        logger.error("LEAD_NOTIFY_TO not set -- failed delivery %s reaches nobody", row["id"])
        return False
    data = json.loads(row["payload"] or "{}")
    subject = f"mauxy: Weiterleitung FEHLGESCHLAGEN ({row['site']} / {row['action']} -> {row['sink']})"
    lines = [
        "Eine Weiterleitung an ein Zielsystem ist endgültig fehlgeschlagen.",
        f"Site: {row['site']}", f"Aktion: {row['action']}", f"Ziel: {_target_label(row)}",
        f"Fehler: {row['last_error']}", f"Versuche: {row['attempts']}",
        f"E-Mail: {data.get('email', '-')}",
        "",
        "Bitte manuell nachtragen" + (" (Abmeldung: rechtlich nötig)" if row["action"] == "unsubscribe" else "")
        + f" oder nach Behebung erneut anstoßen: POST /api/admin/deliveries/{row['id']}/retry (Admin-Key).",
    ]
    return await mailer.send(lead_sync.NOTIFY_TO, subject, "\n".join(lines), audience="internal",
                             key=f"route-failed-{row['id']}-{row['attempts']}")


async def resume(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT id FROM route_delivery WHERE state = 'queued'") as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for did in ids:
        start(db, did)
    if ids:
        logger.info("ROUTE_RESUMED count=%d", len(ids))
    return len(ids)


async def purge(db: aiosqlite.Connection) -> None:
    cutoff = iso(now() - timedelta(days=lead_sync.RETENTION_DAYS))
    await db.execute("DELETE FROM route_delivery WHERE state IN ('done', 'failed') AND COALESCE(done_at, created_at) < ?",
                     (cutoff,))
    await db.commit()


async def requeue(db: aiosqlite.Connection, did: int) -> bool:
    row = await _row(db, did)
    if not row or row["state"] != "failed" or not row["payload"]:
        return False
    await db.execute("UPDATE route_delivery SET state = 'queued', attempts = 0, next_attempt_at = NULL, done_at = NULL "
                     "WHERE id = ?", (did,))
    await db.commit()
    start(db, did)
    return True


async def failed_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) FROM route_delivery WHERE state = 'failed'") as cur:
        (n,) = await cur.fetchone()
    return n


# ------------------------------------------------------------------------------- admin router


def build_router(*, require_admin: Callable[[Request], Optional[JSONResponse]],
                 on_change: Callable[[aiosqlite.Connection], object]) -> APIRouter:
    router = APIRouter()
    site_ok = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

    def bad(site: str, action: Optional[str] = None) -> Optional[JSONResponse]:
        if not site_ok.match(site):
            return JSONResponse({"error": "invalid site name"}, status_code=422)
        if action is not None and action not in ACTIONS:
            return JSONResponse({"error": f"unknown action (known: {sorted(ACTIONS)})"}, status_code=422)
        return None

    @router.get("/api/admin/routes")
    async def admin_list_routes(request: Request, site: Optional[str] = Query(None, max_length=80)):
        if (err := require_admin(request)) is not None:
            return err
        rows = all_rows(site)
        return JSONResponse({"routes": rows, "count": len(rows), "actions": {a: sorted(s) for a, s in ACTIONS.items()},
                             "webhook_allowed_hosts": sorted(webhooks.allowed_hosts())})

    @router.put("/api/admin/routes/{site}/{action}")
    async def admin_put_routes(site: str, action: str, request: Request):
        """Replace the ordered target list of (site, action)."""
        if (err := require_admin(request) or bad(site, action)) is not None:
            return err
        try:
            body = await request.json()
            ts = check_targets(action, body.get("targets") if isinstance(body, dict) else None)
        except RouteInvalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=422)
        db: aiosqlite.Connection = request.app.state.db
        try:
            await db.execute("DELETE FROM site_route WHERE site = ? AND action = ?", (site, action))
            for t in ts:
                await db.execute("INSERT INTO site_route (site, action, position, sink, params, enabled, updated_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (site, action, t.position, t.sink, json.dumps(t.params, sort_keys=True), int(t.enabled),
                                  iso(now())))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        await on_change(db)
        logger.info("ADMIN_ROUTES_SET site=%s action=%s targets=%s", site, action, ",".join(t.sink for t in ts))
        return JSONResponse({"status": "updated", "routes": all_rows(site)})

    @router.delete("/api/admin/routes/{site}/{action}")
    async def admin_delete_routes(site: str, action: str, request: Request):
        """Remove the routes of (site, action): back to the fallback sinks."""
        if (err := require_admin(request) or bad(site, action)) is not None:
            return err
        db: aiosqlite.Connection = request.app.state.db
        cur = await db.execute("DELETE FROM site_route WHERE site = ? AND action = ?", (site, action))
        await db.commit()
        if cur.rowcount == 0:
            return JSONResponse({"error": "no routes"}, status_code=404)
        await on_change(db)
        return JSONResponse({"status": "deleted"})

    @router.get("/api/admin/deliveries")
    async def admin_list_deliveries(request: Request,
                                    state: Optional[str] = Query(None, pattern="^(queued|done|failed)$"),
                                    site: Optional[str] = Query(None, max_length=80),
                                    action: Optional[str] = Query(None, max_length=40),
                                    limit: int = Query(100, ge=1, le=500)):
        if (err := require_admin(request)) is not None:
            return err
        sql = ("SELECT id, event_ref, site, action, position, sink, params, state, result, attempts, next_attempt_at, "
               "last_error, failure_mailed, created_at, done_at FROM route_delivery WHERE 1 = 1")
        args: list = []
        for col, val in (("state", state), ("site", site), ("action", action)):
            if val:
                sql += f" AND {col} = ?"  # fixed column names
                args.append(val)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(sql, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["params"] = json.loads(r["params"])
        return JSONResponse({"deliveries": rows, "count": len(rows)})

    @router.post("/api/admin/deliveries/{did}/retry")
    async def admin_retry_delivery(did: int, request: Request):
        if (err := require_admin(request)) is not None:
            return err
        ok = await requeue(request.app.state.db, did)
        return JSONResponse({"requeued": ok}, status_code=200 if ok else 409)

    return router
