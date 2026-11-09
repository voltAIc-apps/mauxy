#!/usr/bin/env python3
"""
READ-ONLY smoke check of mauxy's Odoo setup against erp.wapsol (flywheel SPEC-021).

Run it once the owner has created the bot user and put its credentials into the env / .env
(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY, ODOO_LEAD_USER_ID, ODOO_LIST_NEWSLETTER_ID,
ODOO_LIST_RADAR_ID, optional ODOO_LEAD_TEAM_ID / ODOO_UTM_SOURCE / ODOO_UTM_MEDIUM). It never writes:
every call goes through a local guard that only lets read methods pass (search_read, read,
has_access / check_access_rights). Prints no credentials.

Checks: server version; login of the bot; the bot's groups; the salesperson (Ashant) and his
partner; CRM team; UTM source / medium / campaigns (selfcheck, contact, inquiry, webinar, checkout);
the two mailing lists; the bot's access rights -- the ones mauxy needs (must be True) and the
ones it must NOT have (must be False).

  python scripts/odoo_smoke.py            # reads .env next to this repo if present
  python scripts/odoo_smoke.py --env-file /path/to/.env
Exit code 0 = all good, 1 = something to fix (listed).
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

READ_METHODS = {"search_read", "read", "has_access", "check_access_rights"}
NEEDED = [  # (model, operation) the bot must have
    ("res.partner", "read"), ("res.users", "read"), ("crm.lead", "read"), ("crm.lead", "create"),
    ("crm.lead", "write"),  # needed by message_subscribe, record rule must limit it to the bot's own leads
    ("utm.source", "read"), ("utm.medium", "read"), ("utm.campaign", "read"),
    ("ir.model.data", "read"), ("res.partner.title", "read"),  # salutation -> crm.lead.title (by xml id)
    ("mail.blacklist", "read"), ("mailing.list", "read"),
    ("mailing.contact", "read"), ("mailing.contact", "create"),
    ("mailing.subscription", "read"), ("mailing.subscription", "create"), ("mailing.subscription", "write"),
]
FORBIDDEN = [  # (model, operation) the bot must NOT have
    ("res.partner", "write"), ("res.partner", "create"), ("res.partner", "unlink"),
    ("crm.lead", "unlink"), ("mailing.contact", "write"), ("mailing.contact", "unlink"),
    ("mailing.list", "write"), ("utm.campaign", "create"), ("sale.order", "read"), ("account.move", "read"),
]
CAMPAIGNS = ("selfcheck", "contact", "inquiry", "webinar", "checkout")


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    from odoo_client import OdooClient, OdooError

    odoo = OdooClient.from_env()
    problems: list[str] = []

    async def read(model: str, method: str, args: list, kw: dict | None = None):
        if method not in READ_METHODS:  # local guard: this script never writes
            raise SystemExit(f"refusing {model}.{method}: not a read method")
        uid = await odoo.uid()
        return await odoo._rpc("object", "execute_kw", [odoo.db, uid, odoo._key, model, method, args, kw or {}], f"{model}.{method}")

    async def may(model: str, op: str) -> bool | None:
        try:
            return bool(await read(model, "has_access", [[], op]))  # Odoo 18
        except OdooError:
            try:
                return bool(await read(model, "check_access_rights", [op], {"raise_exception": False}))
            except OdooError as exc:
                print(f"  ? {model}.{op}: {exc}")
                return None

    v = await odoo.version()
    print(f"server: {v.get('server_version')}  url: {odoo.url}  db: {odoo.db}  user: {odoo.user}")
    uid = await odoo.uid()
    me = (await read("res.users", "read", [[uid]], {"fields": ["name", "login", "share", "groups_id"]}))[0]
    print(f"bot: uid {uid} {me['name']!r} share={me['share']} groups={len(me['groups_id'])}")
    try:
        groups = await read("res.groups", "read", [me["groups_id"]], {"fields": ["full_name"]})
        for g in groups:
            print(f"  group: {g['full_name']}")
    except OdooError:
        print("  (groups not readable, fine)")

    lead_user = os.environ.get("ODOO_LEAD_USER_ID", "")
    if lead_user.isdigit():
        rows = await read("res.users", "read", [[int(lead_user)]], {"fields": ["name", "login", "partner_id", "active"]})
        print(f"salesperson ODOO_LEAD_USER_ID={lead_user}: {rows[0] if rows else 'NOT FOUND'}")
        if not rows or not rows[0]["active"]:
            problems.append("ODOO_LEAD_USER_ID not found / inactive")
    else:
        rows = await read("res.users", "search_read", [[["login", "ilike", "ashant"]]], {"fields": ["name", "login"], "limit": 5})
        print(f"ODOO_LEAD_USER_ID not set; users like 'ashant': {rows}")
        problems.append("set ODOO_LEAD_USER_ID")

    teams = await read("crm.team", "search_read", [[]], {"fields": ["name"], "limit": 20})
    print(f"crm teams: {[(t['id'], t['name']) for t in teams]}  (ODOO_LEAD_TEAM_ID={os.environ.get('ODOO_LEAD_TEAM_ID', '')!r})")

    for model, name in (("utm.source", os.environ.get("ODOO_UTM_SOURCE", "simplify-erp.de")),
                        ("utm.medium", os.environ.get("ODOO_UTM_MEDIUM", "Website"))) + tuple(("utm.campaign", c) for c in CAMPAIGNS):
        rows = await read(model, "search_read", [[["name", "=", name]]], {"fields": ["id"], "limit": 1})
        print(f"{model} {name!r}: {rows[0]['id'] if rows else 'MISSING'}")
        if not rows:
            problems.append(f"create {model} {name!r} in Odoo")

    from sinks import TITLE_XMLIDS
    rows = await read("ir.model.data", "search_read", [[["module", "=", "base"], ["model", "=", "res.partner.title"],
                                                       ["name", "in", sorted(TITLE_XMLIDS.values())]]],
                      {"fields": ["name", "res_id"]})
    found = {r["name"]: r["res_id"] for r in rows}
    for sal, xmlid in TITLE_XMLIDS.items():
        print(f"res.partner.title base.{xmlid} ({sal}): {found.get(xmlid, 'MISSING')}")
        if xmlid not in found:
            problems.append(f"base.{xmlid} not readable: leads with salutation {sal!r} get no title")

    lists = await read("mailing.list", "search_read", [[]], {"fields": ["name", "contact_count"], "limit": 50})
    print(f"mailing lists: {[(m['id'], m['name']) for m in lists]}")
    for key in ("NEWSLETTER", "RADAR"):
        lid = os.environ.get(f"ODOO_LIST_{key}_ID", "")
        if not lid.isdigit() or int(lid) not in {m["id"] for m in lists}:
            problems.append(f"ODOO_LIST_{key}_ID={lid!r} is not a readable mailing list")

    print("access rights (needed):")
    for model, op in NEEDED:
        ok = await may(model, op)
        print(f"  {'ok ' if ok else 'NO '} {model}.{op}")
        if not ok:
            problems.append(f"grant {model}.{op}")
    print("access rights (must be absent):")
    for model, op in FORBIDDEN:
        ok = await may(model, op)
        print(f"  {'TOO MUCH' if ok else 'ok      '} {model}.{op}")
        if ok:
            problems.append(f"remove {model}.{op}")

    print("\nresult:", "all good" if not problems else "fix:\n  - " + "\n  - ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    a = ap.parse_args()
    load_env(Path(a.env_file))
    if not (os.environ.get("ODOO_USER") and os.environ.get("ODOO_API_KEY")):
        sys.exit("ODOO_USER / ODOO_API_KEY not set: create the bot user first (README 'erp.wapsol setup')")
    sys.exit(asyncio.run(main()))
