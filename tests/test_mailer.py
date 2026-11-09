"""
mailer.py -- the one mail path (T-MAIL-0xx). A real SMTP server (aiosmtpd) on the loopback alias
10.0.99.1 with a test CA (trustme): STARTTLS and implicit TLS, refusal without TLS, certificate +
hostname verification, MIME structure (alternative / related / mixed, umlauts, RFC 2231 filenames),
size and type limits, header injection, audience policy, retries, idempotency, per-recipient limit,
List-Unsubscribe, resume, logging hygiene. Nothing is sent to mc1 (SMTP_HOST is always 10.0.99.1).
"""
import email
import email.policy
import socket

import aiosqlite
import pytest
import trustme
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

import mailer
from mailer import Attachment

HOST = "10.0.99.1"
FROM = "Simplify-ERP <noreply@simplify-erp.de>"
PDF = b"%PDF-1.7\n% test report\n" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SECRET_BODY = "Ihr Ergebnis: geheimer Inhalt äöü"
PASSWORD = "smtp-test-password-not-real"


def free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Recorder:
    """aiosmtpd handler: records what arrived; `responses` = DATA answers to give first."""

    def __init__(self, responses=()):
        self.mails = []
        self.responses = list(responses)
        self.data_calls = 0

    async def handle_DATA(self, server, session, envelope):
        self.data_calls += 1
        if self.responses:
            r = self.responses.pop(0)
            if r:
                return r
        self.mails.append({"from": envelope.mail_from, "rcpt": list(envelope.rcpt_tos),
                           "data": envelope.original_content or envelope.content, "tls": session.ssl is not None,
                           "authenticated": session.authenticated})
        return "250 2.0.0 OK"


LOGINS = []


def _auth(server, session, envelope, mechanism, auth_data):
    LOGINS.append((auth_data.login, session.ssl is not None))
    ok = auth_data.login == b"bot@simplify-erp.de" and auth_data.password == PASSWORD.encode()
    return AuthResult(success=ok, handled=False)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    ca = trustme.CA()
    d = tmp_path_factory.mktemp("pki")
    ca_file = d / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    return {"ca": ca, "ca_file": str(ca_file)}


def server_ctx(pki, *names):
    import ssl
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pki["ca"].issue_cert(*(names or (HOST,))).configure_cert(ctx)
    return ctx


@pytest.fixture
def smtp(pki, monkeypatch):
    """Start an SMTP server: smtp(mode='starttls'|'ssl'|'plain', responses=[...], names=[...])."""
    started = []

    def start(mode="starttls", responses=(), names=()):
        handler = Recorder(responses)
        port = free_port()
        kw = dict(hostname=HOST, port=port, authenticator=_auth, auth_require_tls=True)
        if mode == "starttls":
            kw.update(tls_context=server_ctx(pki, *names), require_starttls=True)
        elif mode == "ssl":  # implicit TLS: aiosmtpd does not count it for auth_require_tls
            kw.update(ssl_context=server_ctx(pki, *names), server_hostname=HOST, auth_require_tls=False)
        ctl = Controller(handler, **kw)
        ctl.start()
        started.append(ctl)
        monkeypatch.setenv("SMTP_PORT", str(port))
        monkeypatch.setenv("SMTP_SECURITY", "ssl" if mode == "ssl" else "starttls")
        return handler

    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", HOST)
    monkeypatch.setenv("SMTP_USER", "bot@simplify-erp.de")
    monkeypatch.setenv("SMTP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SMTP_FROM", FROM)
    monkeypatch.setenv("SMTP_TIMEOUT", "5")
    monkeypatch.setenv("SMTP_CA_FILE", pki["ca_file"])
    monkeypatch.setenv("MAIL_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("MAIL_MAX_ATTEMPTS", "3")
    yield start
    for ctl in started:
        ctl.stop()


@pytest.fixture
async def db():
    import main
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await main._apply_patches_in_memory(conn)
    mailer.bind(conn)
    yield conn
    mailer.bind(None)
    await conn.close()


async def rows(db, sql="SELECT * FROM mail_outbox"):
    async with db.execute(sql) as cur:
        return [dict(r) for r in await cur.fetchall()]


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


# ---------------------------------------------------------------------------- transport + TLS


async def test_t_mail_001_starttls_verified_auth_after_tls(smtp, db):
    srv = smtp("starttls")
    LOGINS.clear()
    ok = await mailer.send("Anna@Example.com", "Bestätigung für Jörg", SECRET_BODY, audience="confirmed", key="k1")
    assert ok and len(srv.mails) == 1
    m = srv.mails[0]
    assert m["tls"] and m["authenticated"] and LOGINS == [(b"bot@simplify-erp.de", True)]  # AUTH only inside TLS
    assert m["from"] == "noreply@simplify-erp.de" and m["rcpt"] == ["anna@example.com"]
    msg = parse(m["data"])
    assert msg["Subject"] == "Bestätigung für Jörg" and msg["To"] == "anna@example.com"
    assert msg["From"].addresses[0].display_name == "Simplify-ERP"
    assert msg["Message-ID"].endswith("@simplify-erp.de>") and msg["Date"] and msg["Auto-Submitted"] == "auto-generated"
    assert msg.get_body(("plain",)).get_content().strip() == SECRET_BODY
    (row,) = await rows(db)
    assert row["state"] == "sent" and row["mime"] is None and row["attempts"] == 1  # blob dropped after delivery
    assert row["recipient_hash"] == mailer.recipient_hash("anna@example.com")


async def test_t_mail_002_implicit_tls_465_style(smtp, db):
    srv = smtp("ssl")
    LOGINS.clear()
    assert await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert len(srv.mails) == 1 and [login for login, _ in LOGINS] == [b"bot@simplify-erp.de"]


async def test_t_mail_008_ssl_with_untrusted_certificate_refused(smtp, db, monkeypatch):
    srv = smtp("ssl")
    monkeypatch.delenv("SMTP_CA_FILE")
    LOGINS.clear()
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert srv.mails == [] and LOGINS == []


async def test_t_mail_003_refused_without_starttls_never_plaintext(smtp, db):
    srv = smtp("plain")
    LOGINS.clear()
    assert not await mailer.send("anna@example.com", "x", SECRET_BODY, audience="confirmed")
    assert srv.mails == [] and srv.data_calls == 0 and LOGINS == []  # credentials never sent in clear
    (row,) = await rows(db)
    assert row["state"] == "failed" and row["attempts"] == 1 and "starttls" in row["last_error"]  # no retry


async def test_t_mail_004_untrusted_certificate_refused(smtp, db, monkeypatch):
    srv = smtp("starttls")
    monkeypatch.delenv("SMTP_CA_FILE")  # system trust store only: the test CA is unknown
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert srv.mails == []
    assert (await rows(db))[0]["last_error"].startswith("tls")


async def test_t_mail_005_hostname_mismatch_refused(smtp, db):
    srv = smtp("starttls", names=("mail.other.example",))  # valid CA, wrong name for 10.0.99.1
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert srv.mails == []


async def test_t_mail_006_no_plaintext_mode(monkeypatch, db):
    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", HOST)
    monkeypatch.setenv("SMTP_FROM", FROM)
    monkeypatch.setenv("SMTP_SECURITY", "none")
    assert not mailer.configured()
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")


async def test_t_mail_007_connection_refused_is_retried_then_failed(smtp, db, monkeypatch):
    smtp("starttls")
    monkeypatch.setenv("SMTP_PORT", str(free_port()))  # nobody listens
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    (row,) = await rows(db)
    assert row["state"] == "failed" and row["attempts"] == 3


# ---------------------------------------------------------------------------- MIME


async def test_t_mail_010_alternative_related_mixed_umlaut_filenames(smtp, db):
    srv = smtp("starttls")
    html = '<p>Grüße</p><img src="cid:logo">'
    ok = await mailer.send("anna@example.com", "Ihr Selbst-Check", "Grüße", html=html, audience="confirmed", attachments=[
        Attachment("Selbst-Check Übersicht.pdf", PDF, "application/pdf"),
        Attachment("logo.png", PNG, "image/png", inline_cid="logo"),
    ])
    assert ok
    msg = parse(srv.mails[0]["data"])
    assert msg.get_content_type() == "multipart/mixed"
    alt, pdf = msg.get_payload()
    assert alt.get_content_type() == "multipart/alternative"
    plain, related = alt.get_payload()
    assert plain.get_content_type() == "text/plain" and related.get_content_type() == "multipart/related"
    html_part, img = related.get_payload()
    assert html_part.get_content_type() == "text/html" and "Grüße" in html_part.get_content()
    assert img.get_content_type() == "image/png" and img["Content-ID"] == "<logo>"
    assert img.get_content_disposition() == "inline" and img.get_content() == PNG
    assert pdf.get_content_type() == "application/pdf" and pdf.get_content_disposition() == "attachment"
    assert pdf.get_filename() == "Selbst-Check Übersicht.pdf" and pdf.get_content() == PDF
    assert b"filename*=utf-8''" in srv.mails[0]["data"]  # RFC 2231


@pytest.mark.parametrize("att,kw,why", [
    (Attachment("big.pdf", PDF + b"0" * 2_000_000, "application/pdf"), {}, "size"),
    (Attachment("x.html", b"<html>", "text/html"), {}, "type not allowed"),
    (Attachment("x.png", PDF, "image/png"), {}, "magic mismatch"),
    (Attachment("x.pdf", PDF, "application/pdf"), {"audience": "doi"}, "doi mail with attachment"),
    (Attachment("x.png", PNG, "image/png", inline_cid="logo"), {"html": None}, "inline without html"),
    (Attachment("x.png", PNG, "image/png", inline_cid="lo go>"), {"html": "<p/>"}, "bad cid"),
    (Attachment("x.pdf", "not bytes", "application/pdf"), {}, "not bytes"),
], ids=["T-MAIL-011-size", "T-MAIL-012-type", "T-MAIL-013-magic", "T-MAIL-014-doi", "T-MAIL-015-inline",
        "T-MAIL-016-cid", "T-MAIL-017-bytes"])
async def test_t_mail_011_017_attachment_limits(smtp, db, monkeypatch, att, kw, why):
    srv = smtp("starttls")
    monkeypatch.setenv("MAIL_MAX_BYTES", str(1_000_000))
    args = {"audience": "confirmed", **kw}
    assert not await mailer.send("anna@example.com", "x", "y", attachments=[att], **args), why
    assert srv.mails == [] and await rows(db) == []  # refused before anything is stored


async def test_t_mail_018_filename_is_sanitised(smtp, db):
    srv = smtp("starttls")
    await mailer.send("anna@example.com", "x", "y", audience="confirmed",
                      attachments=[Attachment("../../etc/\r\npasswd.pdf", PDF, "application/pdf")])
    (part,) = [p for p in parse(srv.mails[0]["data"]).iter_attachments()]
    assert part.get_filename() == "passwd.pdf"


# ---------------------------------------------------------------------------- injection / policy


async def test_t_mail_020_header_injection(smtp, db, monkeypatch):
    srv = smtp("starttls")
    monkeypatch.setenv("SMTP_REPLY_TO", "info@simplify-erp.de")
    ok = await mailer.send("anna@example.com", "Hallo\r\nBcc: evil@attacker.example", "y",
                           reply_to="x@y.de\r\nBcc: evil@attacker.example", audience="confirmed")
    assert ok
    m = srv.mails[0]
    msg = parse(m["data"])
    assert m["rcpt"] == ["anna@example.com"] and msg["Bcc"] is None
    assert msg["Subject"] == "Hallo Bcc: evil@attacker.example"  # one line, inert text
    assert msg["Reply-To"] == "info@simplify-erp.de"  # injected reply-to dropped, configured default used
    for bad in ("anna@example.com\r\nBcc: evil@attacker.example", "a@b.de, c@d.de", "Anna <a@b.de>", "", "nobody"):
        assert not await mailer.send(bad, "x", "y", audience="confirmed")
    assert len(srv.mails) == 1


async def test_t_mail_021_internal_only_to_configured_addresses(smtp, db):
    srv = smtp("starttls")
    assert not await mailer.send("stranger@example.com", "x", "y", audience="internal")
    assert await mailer.send("owner@site.test", "x", "y", audience="internal")  # SELFCHECK_NOTIFY_TO (conftest)
    assert [m["rcpt"] for m in srv.mails] == [["owner@site.test"]]
    assert not await mailer.send("owner@site.test", "x", "y", audience="nobody")


async def test_t_mail_022_list_unsubscribe(smtp, db):
    srv = smtp("starttls")
    await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    await mailer.send("anna@example.com", "x", "y", audience="confirmed",
                      list_unsubscribe="https://simplify-erp.de/de/newsletter/abmelden?t=abc")
    plain, listed = (parse(m["data"]) for m in srv.mails)
    assert plain["List-Unsubscribe"] is None
    assert listed["List-Unsubscribe"] == "<https://simplify-erp.de/de/newsletter/abmelden?t=abc>"
    assert listed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed", list_unsubscribe="http://x")


# ---------------------------------------------------------------------------- retries / idempotency / limit


async def test_t_mail_030_transient_4xx_retried(smtp, db):
    srv = smtp("starttls", responses=["451 4.3.0 try later", "451 4.3.0 try later"])
    assert await mailer.send("anna@example.com", "x", "y", audience="confirmed", key="retry")
    assert len(srv.mails) == 1 and srv.data_calls == 3
    (row,) = await rows(db)
    assert row["state"] == "sent" and row["attempts"] == 3


async def test_t_mail_031_permanent_5xx_not_retried(smtp, db):
    srv = smtp("starttls", responses=["550 5.1.1 no such user"])
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert srv.data_calls == 1
    (row,) = await rows(db)
    assert row["state"] == "failed" and row["last_error"] == "smtp 550" and row["mime"] is None


async def test_t_mail_032_retries_exhausted(smtp, db):
    srv = smtp("starttls", responses=["451 4.3.0 later"] * 5)
    assert not await mailer.send("anna@example.com", "x", "y", audience="confirmed")
    assert srv.data_calls == 3 and (await rows(db))[0]["attempts"] == 3


async def test_t_mail_033_idempotent_per_message_id(smtp, db):
    srv = smtp("starttls")
    assert await mailer.send("anna@example.com", "x", "y", audience="confirmed", key="sc-result-7")
    assert await mailer.send("anna@example.com", "x", "y", audience="confirmed", key="sc-result-7")
    assert len(srv.mails) == 1 and len(await rows(db)) == 1


async def test_t_mail_034_per_recipient_limit(smtp, db, monkeypatch):
    srv = smtp("starttls")
    monkeypatch.setenv("MAIL_RECIPIENT_HOURLY_LIMIT", "2")
    results = [await mailer.send("anna@example.com", "x", "y", audience="doi") for _ in range(3)]
    assert results == [True, True, False]
    assert await mailer.send("ANNA@example.com", "x", "y", audience="doi") is False  # same address, other case
    for _ in range(3):  # internal recipients are not limited
        assert await mailer.send("owner@site.test", "x", "y", audience="internal")
    assert len(srv.mails) == 5


async def test_t_mail_035_resume_after_restart(smtp, db):
    srv = smtp("starttls")
    msg, _, rcpt = mailer.build("anna@example.com", "x", "y", audience="confirmed", key="queued-before-restart")
    await db.execute("INSERT INTO mail_outbox (message_id, recipient_hash, audience, mime, created_at) VALUES (?,?,?,?,?)",
                     (msg["Message-ID"], mailer.recipient_hash(rcpt), "confirmed", msg.as_bytes(), "2026-11-09T10:00:00+00:00"))
    await db.commit()
    started = []
    assert await mailer.resume(lambda coro, name: started.append(coro)) == 1
    assert await started[0] is True
    assert len(srv.mails) == 1 and (await rows(db))[0]["state"] == "sent"


# ---------------------------------------------------------------------------- logging hygiene / memory mode


async def test_t_mail_040_logs_never_contain_content_or_credentials(smtp, db, caplog):
    caplog.set_level("DEBUG")
    smtp("starttls", responses=["451 4.3.0 later"])
    await mailer.send("anna@example.com", "Vertraulicher Betreff", SECRET_BODY, audience="confirmed",
                      attachments=[Attachment("r.pdf", PDF, "application/pdf")])
    smtp("plain")
    await mailer.send("bob@example.com", "Vertraulicher Betreff", SECRET_BODY, audience="confirmed")
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("mauxy"))  # not the test server's
    for secret in (PASSWORD, SECRET_BODY, "Vertraulicher", "anna@example.com", "bob@example.com", "test report"):
        assert secret not in text
    assert "MAIL_SENT message_id=<" in text and "rcpt=" in text and "MAIL_RETRY" in text and "MAIL_FAILED" in text


async def test_t_mail_041_memory_mode_runs_the_same_policy(monkeypatch, db, outbox):
    monkeypatch.setenv("MAIL_MODE", "memory")
    assert await mailer.send("anna@example.com", "x", "y", audience="doi", key="m1")
    assert not await mailer.send("anna@example.com", "x", "y", audience="doi",
                                 attachments=[Attachment("r.pdf", PDF, "application/pdf")])
    assert [m["to"] for m in outbox] == ["anna@example.com"] and outbox[0]["message_id"].endswith("@site.test>")
