"""
Post-confirmation outbox (flywheel SPEC-021): delivers ONE confirmed submission to the sinks.

One row per confirmed submission, UNIQUE(origin, origin_id): a confirmation creates at most one
lead. The row holds what is to be delivered (lead text, subscriptions) and the outcome
(odoo_lead_id, per-list results). Steps are recorded as they succeed, so a retry repeats only the
missing ones; the lead create on a retry first looks for the lead of an earlier attempt (marker
`ref` in the description), so a lost response cannot produce a duplicate.

Retries: SinkTransient -> attempts + 1, exponential backoff (LEAD_SYNC_BACKOFF_SECONDS * 2^n, max
1 h) inside the job (jobs.spawn), up to LEAD_SYNC_MAX_ATTEMPTS. Queued rows are resumed on start
(a pod restart only delays them). SinkPermanent or attempts exhausted -> state failed and a mail
with the whole submission to LEAD_NOTIFY_TO (Ashant), so nothing is lost; admin can re-queue
(POST /api/lead/sync/{id}/retry) after fixing the cause.

Data minimisation: lead text and subscription data are cleared once delivered (the CRM holds
them); rows are deleted LEAD_RETENTION_DAYS after done / failed.

Per-site routing (routing.py): `target` = the site's routed CRM target ({"sink": "odoo", "params":
{lists, team_id, tag_ids, utm_*}} or {"sink": "none"}); NULL = the fallback (LEAD_SINK,
SUBSCRIBER_SINK, ODOO_LIST_*_ID). Every other target of the site gets its own row in
route_delivery (fan-out), so this row stays the one crm.lead + operator-mail path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import aiosqlite

import jobs
import mailer
import sinks

logger = logging.getLogger("mauxy.lead_sync")

MAX_ATTEMPTS = int(os.environ.get("LEAD_SYNC_MAX_ATTEMPTS", "6"))
BACKOFF_SECONDS = float(os.environ.get("LEAD_SYNC_BACKOFF_SECONDS", "60"))
RETENTION_DAYS = int(os.environ.get("LEAD_RETENTION_DAYS", "30"))
NOTIFY_TO = os.environ.get("LEAD_NOTIFY_TO", "") or os.environ.get("SELFCHECK_NOTIFY_TO", "")

# origin -> async finish(db, row: dict, outcome: 'done' | 'failed'); registered by the routers
FinishHook = Callable[[aiosqlite.Connection, dict, str], Awaitable[None]]
_hooks: dict[str, FinishHook] = {}
_running: set[int] = set()


def register(origin: str, hook: FinishHook) -> None:
    _hooks[origin] = hook


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def new_ref() -> str:
    """Marker written into the lead description (hex only: safe inside an ilike search)."""
    return "mauxy-" + secrets.token_hex(12)


# schema: db-patches/0003_website_consent.sql (applied by scripts/sqlite_db.py --migrate)
async def purge(db: aiosqlite.Connection) -> None:
    cutoff = _iso(_now() - timedelta(days=RETENTION_DAYS))
    await db.execute("DELETE FROM lead_sync WHERE state IN ('done', 'failed') AND COALESCE(done_at, created_at) < ?", (cutoff,))
    await db.commit()


async def _row(db: aiosqlite.Connection, sync_id: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM lead_sync WHERE id = ?", (sync_id,)) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def get_for(db: aiosqlite.Connection, origin: str, origin_id: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM lead_sync WHERE origin = ? AND origin_id = ?", (origin, origin_id)) as cur:
        r = await cur.fetchone()
    return dict(r) if r else None


async def enqueue(db: aiosqlite.Connection, *, origin: str, origin_id: int, lead: Optional[dict],
                  subscribe: Optional[dict], target: Optional[dict] = None) -> Optional[int]:
    """Store the delivery for one confirmed submission and start it. A second call for the same
    submission is ignored (UNIQUE) -> None. target = routed CRM target (module doc) or None."""
    cur = await db.execute(
        "INSERT OR IGNORE INTO lead_sync (origin, origin_id, ref, created_at, lead, subscribe, target) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (origin, origin_id, (lead or {}).get("ref") or new_ref(), _iso(_now()),
         json.dumps(lead, ensure_ascii=False) if lead else None,
         json.dumps(subscribe, ensure_ascii=False) if subscribe and subscribe.get("lists") else None,
         json.dumps(target, sort_keys=True) if target else None),
    )
    await db.commit()
    if not cur.rowcount:
        logger.warning("LEAD_SYNC_DUPLICATE origin=%s origin_id=%s", origin, origin_id)
        return None
    sync_id = cur.lastrowid
    start(db, sync_id)
    return sync_id


def start(db: aiosqlite.Connection, sync_id: int) -> None:
    if sync_id in _running:
        return
    _running.add(sync_id)
    jobs.spawn(_run_guarded(db, sync_id), name=f"lead_sync:{sync_id}")


async def _run_guarded(db: aiosqlite.Connection, sync_id: int) -> None:
    try:
        await run(db, sync_id)
    finally:
        _running.discard(sync_id)


async def resume(db: aiosqlite.Connection) -> int:
    """Restart queued deliveries (after a pod restart). Returns the count."""
    async with db.execute("SELECT id FROM lead_sync WHERE state = 'queued'") as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for sid in ids:
        start(db, sid)
    if ids:
        logger.info("LEAD_SYNC_RESUMED count=%d", len(ids))
    return len(ids)


def backoff(attempt: int) -> float:
    return min(BACKOFF_SECONDS * (2 ** max(attempt - 1, 0)), 3600.0)


async def _step(db: aiosqlite.Connection, row: dict) -> None:
    """One delivery attempt; persists each finished step. Raises SinkTransient / SinkPermanent."""
    sub = json.loads(row["subscribe"]) if row["subscribe"] else None
    results = json.loads(row["sub_results"] or "{}")
    target = json.loads(row["target"]) if row.get("target") else None
    params = (target or {}).get("params") or {}
    routed_none = target is not None and target.get("sink") != "odoo"
    if sub:
        sink = None if routed_none else sinks.subscriber_sink("odoo" if target else None)
        for key in sub["lists"]:
            if key in results:
                continue
            if sink is None:  # routed site without odoo target: lists handled by its other targets
                results[key] = "routed"
            else:
                extra = {"list_id": params.get("lists", {}).get(key)} if target else {}
                res = await sink.subscribe(sub["email"], key, lang=sub["lang"], source=sub["source"],
                                           page_url=sub.get("page_url") or "", name=sub.get("name"),
                                           company=sub.get("company"), **extra)
                results[key] = "dnc_skip" if res == "blocked" else res
            await db.execute("UPDATE lead_sync SET sub_results = ? WHERE id = ?", (json.dumps(results), row["id"]))
            await db.commit()
    lead = json.loads(row["lead"]) if row["lead"] else None
    if lead and routed_none and row["lead_sink"] is None:
        await db.execute("UPDATE lead_sync SET lead_sink = 'none' WHERE id = ?", (row["id"],))
        await db.commit()
    elif lead and row["odoo_lead_id"] is None and row["lead_sink"] != "none":
        sink = sinks.odoo_lead_sink() if target else sinks.lead_sink()
        # any earlier attempt may have created the lead before failing (lost response, follower step)
        lead_id = await sink.create_lead(lead, maybe_exists=row["attempts"] > 0 or bool(row["last_error"]),
                                         **({"overrides": params} if target else {}))
        await db.execute("UPDATE lead_sync SET odoo_lead_id = ?, lead_sink = ? WHERE id = ?", (lead_id, sink.name, row["id"]))
        await db.commit()


async def run(db: aiosqlite.Connection, sync_id: int) -> None:
    while True:
        row = await _row(db, sync_id)
        if row is None or row["state"] != "queued":
            return
        if row["next_attempt_at"]:
            wait = (datetime.fromisoformat(row["next_attempt_at"]) - _now()).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)
        try:
            await _step(db, row)
        except sinks.SinkTransient as exc:
            attempts = row["attempts"] + 1
            if attempts < MAX_ATTEMPTS:
                nxt = _now() + timedelta(seconds=backoff(attempts))
                await db.execute("UPDATE lead_sync SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                                 (attempts, str(exc)[:300], _iso(nxt), sync_id))
                await db.commit()
                logger.warning("LEAD_SYNC_RETRY id=%s attempt=%d error=%s", sync_id, attempts, str(exc)[:200])
                continue
            await _finish(db, sync_id, "failed", attempts=attempts, error=f"retries exhausted: {exc}")
            return
        except sinks.SinkPermanent as exc:
            await _finish(db, sync_id, "failed", attempts=row["attempts"] + 1, error=str(exc))
            return
        await _finish(db, sync_id, "done", attempts=row["attempts"] + 1, error=None)
        return


async def _finish(db: aiosqlite.Connection, sync_id: int, outcome: str, *, attempts: int, error: Optional[str]) -> None:
    await db.execute("UPDATE lead_sync SET state = ?, attempts = ?, last_error = ?, done_at = ?, next_attempt_at = NULL WHERE id = ?",
                     (outcome, attempts, error[:300] if error else None, _iso(_now()), sync_id))
    await db.commit()
    row = await _row(db, sync_id)
    if outcome == "failed":
        logger.error("LEAD_SYNC_FAILED id=%s origin=%s error=%s", sync_id, row["origin"], (error or "")[:200])
        if await mail_failure(row):
            await db.execute("UPDATE lead_sync SET failure_mailed = 1 WHERE id = ?", (sync_id,))
            await db.commit()
    else:
        logger.info("LEAD_SYNC_DONE id=%s origin=%s lead=%s subs=%s", sync_id, row["origin"], row["odoo_lead_id"], row["sub_results"])
    hook = _hooks.get(row["origin"])
    if hook:
        try:
            await hook(db, row, outcome)
        except Exception as exc:  # a hook must never break the outbox
            logger.error("LEAD_SYNC_HOOK_FAILED id=%s error=%r", sync_id, exc)
    if outcome == "done":  # delivered: the CRM / list holds the data now
        await db.execute("UPDATE lead_sync SET lead = NULL, subscribe = NULL WHERE id = ?", (sync_id,))
        await db.commit()


def lead_text(row: dict) -> str:
    """Everything needed to enter the lead by hand (failure mail, LEAD_SINK=none). English (operator)."""
    lead = json.loads(row["lead"]) if row["lead"] else {}
    sub = json.loads(row["subscribe"]) if row["subscribe"] else {}
    lines = [lead.get("title", "(no lead)"), ""]
    for label, key in (("E-mail", "email"), ("Name", "contact_name"), ("Company", "partner_name"), ("Phone", "phone"),
                       ("Campaign", "campaign")):
        if lead.get(key):
            lines.append(f"{label}: {lead[key]}")
    if sub.get("lists"):
        lines.append(f"Lists (confirmed): {', '.join(sub['lists'])} -> {row.get('sub_results') or '{}'}")
    lines += ["", lead.get("description", "")]
    return "\n".join(lines).strip()


async def mail_failure(row: dict) -> bool:
    if not NOTIFY_TO:
        logger.error("LEAD_NOTIFY_TO not set -- failed lead %s reaches nobody", row["id"])
        return False
    # operator mail: English (owner decision 2026-11-09, texts for the owner are English)
    subject = f"mauxy: website lead NOT in CRM ({row['origin']} #{row['origin_id']})"
    body = "\n".join([
        "The confirmed website request could not be delivered to erp.wapsol.",
        f"Error: {row['last_error']}",
        f"Attempts: {row['attempts']}. Please create it by hand, or re-queue it after fixing the cause:",
        f"POST /api/lead/sync/{row['id']}/retry (admin key).",
        "",
        lead_text(row),
    ])
    lead = json.loads(row["lead"]) if row["lead"] else {}
    return await mailer.send(NOTIFY_TO, subject, body, reply_to=lead.get("email"), audience="internal",
                             key=f"lead-sync-failed-{row['id']}-{row['attempts']}")


async def requeue(db: aiosqlite.Connection, sync_id: int) -> bool:
    """Admin: failed -> queued (attempts reset). False if not failed or data already purged."""
    row = await _row(db, sync_id)
    if not row or row["state"] != "failed" or not (row["lead"] or row["subscribe"]):
        return False
    await db.execute("UPDATE lead_sync SET state = 'queued', attempts = 0, next_attempt_at = NULL, done_at = NULL WHERE id = ?", (sync_id,))
    await db.commit()
    start(db, sync_id)
    return True
