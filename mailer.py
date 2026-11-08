"""
mauxy mailer -- plain-text transactional mail (double opt-in links, self-check result,
operator notification) via SMTP (mailcow mc1.simplify-erp.de, STARTTLS 587 or SSL 465).

MAIL_MODE:
  smtp     send via SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD (default when SMTP_HOST is set)
  console  log the mail to stdout instead of sending (dev / dry run; default without SMTP_HOST)
  memory   append to OUTBOX (tests)

All config from env (12-factor); credentials never logged.
"""
import asyncio
import email.message
import email.utils
import logging
import os
import smtplib
import ssl

logger = logging.getLogger("mauxy.mailer")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
MAIL_MODE = os.environ.get("MAIL_MODE", "smtp" if SMTP_HOST else "console").lower()

# Captured mails in MAIL_MODE=memory (tests only)
OUTBOX: list[dict] = []

if MAIL_MODE == "smtp" and not (SMTP_HOST and SMTP_FROM):
    logger.warning("MAIL_MODE=smtp but SMTP_HOST/SMTP_FROM not set -- mails will fail")
if MAIL_MODE == "console":
    logger.warning("MAIL_MODE=console -- mails are printed to the log, NOT sent (dev / dry run)")


def configured() -> bool:
    """True when a mail can actually leave (or is captured on purpose)."""
    return MAIL_MODE in ("console", "memory") or bool(SMTP_HOST and SMTP_FROM)


def _build(to: str, subject: str, body: str, reply_to: str | None) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = SMTP_FROM or "mauxy@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain=(SMTP_FROM.rsplit("@", 1)[-1].strip(">") or None))
    msg["Auto-Submitted"] = "auto-generated"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg


def _send_smtp(msg: email.message.EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30)
    else:
        srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    try:
        if SMTP_PORT != 465:
            srv.starttls(context=ctx)  # never send credentials or mail in clear text
        if SMTP_USER:
            srv.login(SMTP_USER, SMTP_PASSWORD)
        srv.send_message(msg)
    finally:
        try:
            srv.quit()
        except Exception:
            pass


async def send(to: str, subject: str, body: str, reply_to: str | None = None) -> bool:
    """Send one plain-text mail. Returns True on success, never raises."""
    msg = _build(to, subject, body, reply_to)
    try:
        if MAIL_MODE == "memory":
            OUTBOX.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})
        elif MAIL_MODE == "console":
            logger.info("MAIL_CONSOLE (not sent)\n--- To: %s\n--- Subject: %s\n%s\n---", to, subject, body)
        else:
            await asyncio.to_thread(_send_smtp, msg)
        return True
    except Exception as exc:  # SMTP down, auth error, refused recipient ...
        logger.error("MAIL_SEND_FAILED subject=%r error=%s", subject, exc.__class__.__name__)
        return False
