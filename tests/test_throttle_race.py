"""
Per-address caps under concurrency (security review 2026-11-09, finding 2: mail bombing through a
throttle race). Every cap is ONE conditional INSERT ... SELECT ... WHERE (SELECT COUNT(*) ...) < cap
with a rowcount check, so N parallel requests can never pass it more than `cap` times.

  T-SEC-040  newsletter subscribe: N parallel requests -> at most MAX_PENDING_PER_EMAIL rows + DOI mails
  T-SEC-041  CTA lead submit (POST /api/lead): same cap
  T-SEC-042  Selbst-Check submit (POST /api/selfcheck): same cap
  T-SEC-043  mailer per-recipient hourly limit: N parallel send() -> at most the limit
  T-SEC-044  mailer: parallel send() with the same key -> ONE outbox row, ONE delivery
  T-SEC-045  the caps stay per address: another address is not affected
"""
import asyncio
import copy
import json
from pathlib import Path

import aiosqlite
import httpx
import pytest

import jobs
import mailer
import main as mauxy_main
from tests.conftest import WEB_KEY

HERE = Path(__file__).parent
SC_BASE = json.loads((HERE / "cases" / "selfcheck_api_cases.json").read_text(encoding="utf-8"))["base"]
LEAD_BASE = json.loads((HERE / "cases" / "lead_api_cases.json").read_text(encoding="utf-8"))["base"]["contact"]
ORIGIN = {"Origin": "https://site.test"}
N = 12
CAP = mauxy_main.MAX_PENDING_PER_EMAIL


async def _burst(app, requests: list[tuple[str, dict, dict]]) -> list[int]:
    """Fire all requests at once against the running app (lifespan already up via TestClient),
    then run every job they spawned. Returns the status codes."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resps = await asyncio.gather(*(ac.post(url, json=body, headers=headers) for url, body, headers in requests))
    await jobs.drain()
    return [r.status_code for r in resps]


async def _count(app, sql, params=()):
    async with app.state.db.execute(sql, params) as cur:
        (n,) = await cur.fetchone()
    return n


def to(outbox, addr):
    return [m for m in outbox if m["to"] == addr]


def test_t_sec_040_newsletter_parallel_subscribe_capped(client, outbox, odoo):
    body = {"email": "victim@example.com", "locale": "de", "source": "footer", "consent": True,
            "pageUrl": "https://site.test/de/"}
    hdr = {"Authorization": f"Bearer {WEB_KEY}"}  # website site: quiz off, Odoo sink
    codes = client.portal.call(_burst, client.app, [("/api/subscribe", body, hdr)] * N)
    assert codes == [200] * N
    rows = client.portal.call(_count, client.app, "SELECT COUNT(*) FROM newsletter_pending WHERE email = ?",
                              ("victim@example.com",))
    assert rows == CAP
    assert len(to(outbox, "victim@example.com")) == CAP


def test_t_sec_041_lead_parallel_submit_capped(client, outbox, odoo):
    body = {**copy.deepcopy(LEAD_BASE), "email": "victim@example.com"}
    codes = client.portal.call(_burst, client.app, [("/api/lead", body, ORIGIN)] * N)
    assert codes == [200] * N
    rows = client.portal.call(_count, client.app, "SELECT COUNT(*) FROM lead_submission WHERE email = ?",
                              ("victim@example.com",))
    assert rows == CAP
    assert len(to(outbox, "victim@example.com")) == CAP
    assert odoo.calls == []  # nothing before a confirmation


def test_t_sec_042_selfcheck_parallel_submit_capped(client, outbox):
    body = {**copy.deepcopy(SC_BASE), "email": "victim@example.com"}
    codes = client.portal.call(_burst, client.app, [("/api/selfcheck", body, ORIGIN)] * N)
    assert codes == [200] * N
    rows = client.portal.call(_count, client.app, "SELECT COUNT(*) FROM selfcheck_submission WHERE email = ?",
                              ("victim@example.com",))
    assert rows == CAP
    assert len(to(outbox, "victim@example.com")) == CAP


def test_t_sec_045_cap_is_per_address(client, outbox):
    reqs = [("/api/selfcheck", {**copy.deepcopy(SC_BASE), "email": f"user{i % 2}@example.com"}, ORIGIN) for i in range(N)]
    client.portal.call(_burst, client.app, reqs)
    assert len(to(outbox, "user0@example.com")) == CAP and len(to(outbox, "user1@example.com")) == CAP


@pytest.fixture
async def mail_db(monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "memory")
    monkeypatch.setenv("SMTP_FROM", "Simplify-ERP <noreply@simplify-erp.de>")
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await mauxy_main._apply_patches_in_memory(conn)
    mailer.bind(conn)
    mailer.OUTBOX.clear()
    yield conn
    mailer.bind(None)
    mailer.OUTBOX.clear()
    await conn.close()


async def test_t_sec_043_mailer_parallel_sends_capped(mail_db, monkeypatch):
    monkeypatch.setenv("MAIL_RECIPIENT_HOURLY_LIMIT", "4")
    results = await asyncio.gather(*(mailer.send("anna@example.com", "x", "y", audience="doi") for _ in range(N)))
    assert sum(results) == 4
    assert len(mailer.OUTBOX) == 4
    async with mail_db.execute("SELECT COUNT(*) FROM mail_outbox") as cur:
        assert (await cur.fetchone())[0] == 4
    # internal recipients stay exempt, also in parallel
    internal = await asyncio.gather(*(mailer.send("owner@site.test", "x", "y", audience="internal") for _ in range(N)))
    assert all(internal)


async def test_t_sec_044_mailer_parallel_same_key_one_delivery(mail_db):
    results = await asyncio.gather(*(mailer.send("anna@example.com", "x", "y", audience="confirmed", key="sc-result-9")
                                     for _ in range(5)))
    assert all(results)
    assert len(mailer.OUTBOX) == 1
    async with mail_db.execute("SELECT COUNT(*) FROM mail_outbox") as cur:
        assert (await cur.fetchone())[0] == 1
