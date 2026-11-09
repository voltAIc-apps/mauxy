"""
In-memory fake of the Odoo 18 JSON-RPC API -- just the models and calls mauxy uses.
Used by the tests (respx side_effect / httpx.MockTransport) and by tests/scripts/run_local.py.
NEVER talks to the real erp.wapsol.

Behaviour copied from Odoo where it matters for mauxy:
  - email_normalized computed on create (res.partner, mailing.contact)
  - crm.lead create with user_id auto-subscribes the salesperson's partner (auto_follow=True)
  - errors as Odoo sends them: {"error": {"data": {"name": "odoo.exceptions.AccessError", ...}}}
Test helpers: calls (every execute_kw), fail (queue of injected failures), writes_to(model).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

KEY = "test-key-not-real"
BOT_UID = 99


class FakeOdoo:
    def __init__(self, *, auto_follow: bool = True):
        self.auto_follow = auto_follow
        self.calls: list[tuple[str, str, list, dict]] = []
        self.authenticates = 0
        # queue of failures for (model, method): "http502", "timeout", "AccessError", "ConcurrencyError",
        # "lost" (= perform the call, then answer 502: the response is lost)
        self.fail: dict[tuple[str, str], list[str]] = {}
        self.db: dict[str, dict[int, dict]] = {m: {} for m in (
            "res.partner", "res.users", "crm.lead", "utm.source", "utm.medium", "utm.campaign", "mail.blacklist",
            "mailing.list", "mailing.contact", "mailing.subscription")}
        self._next = 1000
        self._tick = 0  # fake clock of write_date (strictly increasing, deterministic)
        # seed: Ashant (user 7 / partner 70), UTM, lists 1 + 2
        self.add("res.partner", id=70, name="Ashant Chalasani", email="ashant@example.org")
        self.add("res.users", id=7, login="ashant", partner_id=70)
        self.add("utm.source", id=11, name="simplify-erp.de")
        self.add("utm.medium", id=12, name="Website")
        for i, c in enumerate(("selfcheck", "contact", "inquiry", "webinar", "checkout")):
            self.add("utm.campaign", id=20 + i, name=c)
        self.add("mailing.list", id=1, name="Newsletter")
        self.add("mailing.list", id=2, name="Migration Radar")

    # ------------------------------------------------------------------ data helpers

    def stamp(self) -> str:
        """Next write_date, Odoo format (UTC, seconds)."""
        self._tick += 1
        return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self._tick)).strftime("%Y-%m-%d %H:%M:%S")

    def add(self, model: str, **vals) -> int:
        rid = vals.pop("id", None) or self._new_id()
        rec = {"id": rid, "active": True, "write_date": self.stamp(), **vals}
        if "email" in rec and model in ("res.partner", "mailing.contact"):
            rec["email_normalized"] = (rec["email"] or "").strip().lower()
        self.db[model][rid] = rec
        return rid

    def _new_id(self) -> int:
        self._next += 1
        return self._next

    def writes_to(self, model: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == model and c[1] in ("create", "write", "unlink", "message_subscribe")]

    def leads(self) -> list[dict]:
        return list(self.db["crm.lead"].values())

    # ------------------------------------------------------------------ domain

    def _get(self, model: str, rec: dict, path: str) -> Any:
        head, _, rest = path.partition(".")
        val = rec.get(head)
        if rest:
            target = {"contact_id": "mailing.contact", "list_id": "mailing.list"}[head]
            sub = self.db[target].get(val)
            return self._get(target, sub, rest) if sub else None
        return val

    def _match(self, model: str, rec: dict, domain: list) -> bool:
        for leaf in domain:
            field, op, value = leaf
            v = self._get(model, rec, field)
            if op == "=" and v != value:
                return False
            if op == "!=" and v == value:
                return False
            if op == "in" and v not in value:
                return False
            if op == "ilike" and str(value).lower() not in str(v or "").lower():
                return False
        return True

    def search_read(self, model: str, domain: list, fields: list, limit: int = 0, active_test: bool = True) -> list[dict]:
        out = []
        for rec in self.db[model].values():
            if active_test and not rec.get("active", True):
                continue
            if self._match(model, rec, domain):
                out.append(self._project(model, rec, fields))
        return out[:limit] if limit else out

    def _project(self, model: str, rec: dict, fields: list) -> dict:
        row = {"id": rec["id"]}
        for f in fields:
            v = rec.get(f, False)
            if f in ("partner_id", "contact_id", "list_id") and v:
                target = {"partner_id": "res.partner", "contact_id": "mailing.contact", "list_id": "mailing.list"}[f]
                v = [v, (self.db[target].get(v) or {}).get("name", "")]
            row[f] = v
        return row

    # ------------------------------------------------------------------ methods

    def execute(self, model: str, method: str, args: list, kw: dict) -> Any:
        if method == "search_read":
            ctx = kw.get("context") or {}
            return self.search_read(model, args[0], kw.get("fields", []), kw.get("limit", 0), ctx.get("active_test", True))
        if method == "read":
            return [self._project(model, self.db[model][i], kw.get("fields", [])) for i in args[0] if i in self.db[model]]
        if method == "create":
            vals = dict(args[0])
            if model == "crm.lead":
                rid = self.add(model, **vals)
                followers = [self.db["res.users"][vals["user_id"]]["partner_id"]] if self.auto_follow and vals.get("user_id") in self.db["res.users"] else []
                self.db[model][rid]["message_partner_ids"] = followers
                return rid
            if model == "mailing.contact":
                subs = vals.pop("subscription_ids", [])
                rid = self.add(model, **vals)
                for _, _, line in subs:
                    self.add("mailing.subscription", contact_id=rid, list_id=line["list_id"], opt_out=False)
                return rid
            if model == "mailing.subscription":
                return self.add(model, opt_out=False, **vals)
            return self.add(model, **vals)
        if method == "write":
            for i in args[0]:
                self.db[model][i].update({**args[1], "write_date": self.stamp()})
            return True
        if method == "message_subscribe":
            for i in args[0]:
                rec = self.db[model][i]
                rec["message_partner_ids"] = sorted(set(rec.get("message_partner_ids") or []) | set(kw.get("partner_ids", [])))
            return True
        raise KeyError(f"{model}.{method}")

    # ------------------------------------------------------------------ JSON-RPC

    @staticmethod
    def _error(name: str, message: str = "") -> dict:
        return {"code": 200, "message": "Odoo Server Error", "data": {"name": name, "message": message, "debug": ""}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https", "mauxy must call Odoo over https"
        body = json.loads(request.content)
        p = body["params"]
        rid = body.get("id")
        if p["service"] == "common" and p["method"] == "version":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"server_version": "18.0+e"}})
        if p["service"] == "common" and p["method"] == "authenticate":
            self.authenticates += 1
            db, login, key, _ = p["args"]
            ok = login == "website-bot" and key == KEY
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": BOT_UID if ok else False})
        db, uid, key, model, method, args, kw = p["args"]
        if uid != BOT_UID or key != KEY:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": self._error("odoo.exceptions.AccessDenied")})
        queue = self.fail.get((model, method)) or []
        failure: Optional[str] = queue.pop(0) if queue else None
        if failure == "http502":
            return httpx.Response(502)
        if failure == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if failure in ("AccessError", "ValidationError", "ConcurrencyError"):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": self._error(f"odoo.exceptions.{failure}", "fake")})
        self.calls.append((model, method, args, kw))
        result = self.execute(model, method, args, kw)
        if failure == "lost":
            return httpx.Response(502)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})
