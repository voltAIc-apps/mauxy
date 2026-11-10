"""
Selbst-Check endpoints (simplify-erp.de /:lang/selbst-check, flywheel SPEC-020).

  POST /api/selfcheck            submission: e-mail + salutation + name + optional phone + picks
                                 (mail/call/radar) + answers. name required unless
                                 SELFCHECK_NAME_REQUIRED=false (rollout transition, db-patches/0010);
                                 salutation (ms / mr / none) optional: missing -> guessed from the first name
                                 required unless SELFCHECK_SALUTATION_REQUIRED=false (db-patches/0012).
                                 Validated against the vendored rules, stored minimally, ONE
                                 double opt-in mail. Uniform 200 for every contact-specific outcome.
  GET  /api/selfcheck/confirm    DOI mail link, READ-ONLY: confirmation page with a "Bestätigen"
                                 button (confirm_page.py; mail scanners prefetch links). Invalid /
                                 expired / used: uniform neutral page.
  POST /api/selfcheck/confirm    form field token, the human click (atomic single-use claim):
                                 result mail (if picked), then the outbox (lead_sync):
                                 Migration Radar via the subscriber sink (if picked, not blacklisted),
                                 crm.lead in erp.wapsol (picks mail / call; SPEC-021), notification
                                 to the operator (always). 303 back to the website (?bestaetigt=1;
                                 misconfigured: =fehler).
                                 POST /api/lead with kind "selfcheck" is an alias of POST /api/selfcheck.
  POST /api/selfcheck/event      anonymous counter (started/completed/exited_early/submitted):
                                 stores day + event + lang only. No IP (global rate limit key,
                                 access log filtered), no cookie, no identifier.
  GET  /api/selfcheck/submissions  admin (Bearer ADMIN_API_KEY): confirmed submissions, for the
                                 indicative quote and the flywheel follow-up (G3 import).
  GET  /api/selfcheck/stats        admin: counters per day.

Nothing is written to any sink (Odoo / Mautic) before the confirmation. Retention: unconfirmed submissions are
deleted after SELFCHECK_PENDING_DAYS (7), confirmed ones after SELFCHECK_RETENTION_DAYS (90).
Rules + engine are vendored from flywheel (vendor/selfcheck, scripts/sync_selfcheck.py there).

Per site (db-patches/0006): site from the Origin (brand.site_for_origin), stored with the submission;
brand = mail sender, the vendored signature swapped for the site's mail footer (simplify-erp seed =
the same signature), website base of links / redirects, confirmation page. Routing (routing.py,
action "selfcheck"): the site's odoo target rides in lead_sync (crm.lead + Radar list), every other
target (Mautic Radar, flywheel webhook, ...) gets its own outbox row. The self-check CONTENT stays
the vendored simplify-erp rules.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

import brand
import confirm_page
import jobs
import lead_sync
import mail_html
import mailer
from vendor.selfcheck import selfcheck as engine

logger = logging.getLogger("mauxy.selfcheck")

EVENT_PATH = "/api/selfcheck/event"
RULES = engine.load_rules(os.environ.get("SELFCHECK_RULES_FILE") or None)

_raw = os.environ.get("SELFCHECK_ALLOWED_ORIGINS", "")
SELFCHECK_ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _raw.split(",") if o.strip()]
SELFCHECK_RATE_LIMIT = os.environ.get("SELFCHECK_RATE_LIMIT", "5/minute")
SELFCHECK_EVENT_RATE_LIMIT = os.environ.get("SELFCHECK_EVENT_RATE_LIMIT", "600/minute")
SELFCHECK_PENDING_DAYS = int(os.environ.get("SELFCHECK_PENDING_DAYS", "7"))
SELFCHECK_RETENTION_DAYS = int(os.environ.get("SELFCHECK_RETENTION_DAYS", "90"))
SELFCHECK_NOTIFY_TO = os.environ.get("SELFCHECK_NOTIFY_TO", "")
CONFIRM_TOKEN_SECRET = os.environ.get("CONFIRM_TOKEN_SECRET", "")
CONFIRM_TOKEN_MAX_AGE = int(os.environ.get("CONFIRM_TOKEN_MAX_AGE", "86400"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SELFCHECK_WEBSITE_URL = os.environ.get("SELFCHECK_WEBSITE_URL", RULES["website"]).rstrip("/")
MAX_PENDING_PER_EMAIL = int(os.environ.get("MAX_PENDING_PER_EMAIL", "3"))


def env_flag(raw: Optional[str], default: bool = True) -> bool:
    """'false' / '0' / 'no' / 'off' (any case) -> False, anything else -> True; unset / empty -> default."""
    raw = (raw or "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


# Rollout switch (owner 2026-11-09): name is required (default). Deploy mauxy with false first (the
# live website does not send it yet), then the website, then flip to true. Only a MISSING name is
# tolerated while false; a sent but blank / too long name is always 422.
SELFCHECK_NAME_REQUIRED = env_flag(os.environ.get("SELFCHECK_NAME_REQUIRED"))
# Salutation (db-patches/0012): optional since 2026-11-10 (owner: guess it from the first name instead of
# asking); a MISSING salutation is guessed (guess_salutation). true = require the stated value (old form).
# An unknown value is always 422.
SELFCHECK_SALUTATION_REQUIRED = env_flag(os.environ.get("SELFCHECK_SALUTATION_REQUIRED"), default=False)

if not SELFCHECK_ALLOWED_ORIGINS:
    logger.warning("SELFCHECK_ALLOWED_ORIGINS is not set -- /api/selfcheck rejects every browser origin")
if not SELFCHECK_NOTIFY_TO:
    logger.warning("SELFCHECK_NOTIFY_TO is not set -- confirmed self-checks reach nobody")

# Different salt than the newsletter: a newsletter token can never confirm a self-check.
_signer = URLSafeTimedSerializer(CONFIRM_TOKEN_SECRET, salt="mauxy-selfcheck-confirm") if CONFIRM_TOKEN_SECRET else None

PENDING = {"status": "pending_confirmation"}
QUESTION_IDS = [q["id"] for q in RULES["questions"]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- storage


# schema: db-patches/0003_website_consent.sql (applied by scripts/sqlite_db.py --migrate)
async def purge(db: aiosqlite.Connection) -> None:
    """Retention: unconfirmed after SELFCHECK_PENDING_DAYS, confirmed after SELFCHECK_RETENTION_DAYS."""
    now = _now()
    await db.execute(
        "DELETE FROM selfcheck_submission WHERE status = 'pending' AND created_at < ?",
        (_iso(now - timedelta(days=SELFCHECK_PENDING_DAYS)),),
    )
    await db.execute(
        "DELETE FROM selfcheck_submission WHERE status = 'confirmed' AND confirmed_at < ?",
        (_iso(now - timedelta(days=SELFCHECK_RETENTION_DAYS)),),
    )
    await db.commit()


# --------------------------------------------------------------------------- name / phone

NAME_MAX = 100   # first + last name in one field (website maxLength 100)
PHONE_MAX = 40
PHONE_RE = re.compile(r"^[0-9+()/ -]+$")  # loose: digits, spaces, + ( ) / - only (same as the website)
# invisible direction / line controls (category Cf / Zl / Zp): could reorder the operator mail or CRM text
_INVISIBLE = {"\u200e", "\u200f", "\u2028", "\u2029", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
              "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff"}


def clean_name(v: str) -> str:
    """Single line, no control characters: whitespace runs (incl. tab / newline) -> one space,
    other control (Cc) and direction characters removed, trimmed."""
    v = " ".join(v.split())
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cc" and ch not in _INVISIBLE)
    return " ".join(v.split())


def clean_phone(v: str) -> Optional[str]:
    """Trimmed phone or None when empty; ValueError for a foreign character / no digit / too long."""
    v = v.strip()
    if not v:
        return None
    if len(v) > PHONE_MAX or not PHONE_RE.match(v) or not any(ch.isdigit() for ch in v):
        raise ValueError("phone: digits, spaces, + ( ) / - only, max 40")
    return v


# --------------------------------------------------------------------------- salutation / greeting

# Salutation (owner 2026-11-10, replaces "never inferred" of 2026-11-09): stated by the person when a form
# sends it, else GUESSED from the first name (guess_salutation). "none" = undecidable -> neutral greeting
# with the full name ("Guten Tag Vorname Nachname,").
Salutation = Literal["ms", "mr", "none"]
SALUTATION_LABEL = {"ms": "Ms", "mr": "Mr"}  # operator mail / CRM description (English); else "not given"

# Personal greeting of the result mail (Sie-form, after the DOI click only): replaces the vendored
# neutral opening line ("plain"). ms / mr -> formal greeting with the surname, none -> full name (as
# before). Plain text part: literal; the HTML part escapes it (mail_html.text_html).
GREETING = {
    "de": {"plain": "Guten Tag,", "none": "Guten Tag {name},",
           "ms": "Sehr geehrte Frau {surname},", "mr": "Sehr geehrter Herr {surname},"},
    "en": {"plain": "Hello,", "none": "Hello {name},", "ms": "Dear Ms {surname},", "mr": "Dear Mr {surname},"},
}
# leading academic titles stay in the formal greeting ("Prof. Dr. Max Mustermann" -> "Herr Prof. Dr.
# Mustermann"): Prof / Dr (with or without dot, also Dr.-Ing. / Dr.med.) and h.c. / mult.
_ACADEMIC = re.compile(r"^(?:prof|dr|h\.c|mult)\.?(?:-?[a-zäöü]+\.)*$", re.IGNORECASE)
# lower-case name particles belong to the surname ("Anna von Muster" -> "Frau von Muster")
_PARTICLES = {"von", "vom", "zu", "zum", "zur", "van", "de", "der", "den", "del", "della", "di", "da", "du",
              "la", "le", "ten", "ter"}


def surname(name: str) -> str:
    """Surname for the formal greeting: last whitespace token (+ lower-case particles before it), with the
    leading academic titles kept in front. A single token is used as is."""
    tokens = name.split()
    if len(tokens) <= 1:
        return name.strip()
    titles: list[str] = []
    while len(titles) < len(tokens) - 1 and _ACADEMIC.match(tokens[len(titles)]):
        titles.append(tokens[len(titles)])
    last = [tokens[-1]]
    i = len(tokens) - 2
    while i >= len(titles) + 1 and tokens[i] in _PARTICLES:  # a first name stays (>= one token before)
        last.insert(0, tokens[i])
        i -= 1
    return " ".join(titles + last)


# typed prefix in the name field ("Frau Dr. Muster") counts as stated
_PREFIX = {"frau": "ms", "fr.": "ms", "mrs": "ms", "mrs.": "ms", "ms": "ms", "ms.": "ms",
           "herr": "mr", "hr.": "mr", "mr": "mr", "mr.": "mr"}
_detector = None  # gender_guesser.Detector, loaded on first use (~45k names, about 1 s)


def _gender(first: str) -> str:
    """gender_guesser verdict for one first name: German data first; a name without German data falls back
    to the worldwide list (decisive male / female only there, "mostly_*" abroad stays undecided)."""
    global _detector
    if _detector is None:
        import gender_guesser.detector as gg  # GPLv3 dependency (server side only, not distributed)
        _detector = gg.Detector(case_sensitive=False)
    g = _detector.get_gender(first, "germany")
    if g in ("andy", "unknown"):
        g = _detector.get_gender(first)
        if g.startswith("mostly_"):
            return "unknown"
    return g


def guess_salutation(name: Optional[str]) -> str:
    """ms / mr / none guessed from the first name (owner 2026-11-10: right in the vast majority of cases;
    undecidable -> none = neutral greeting). Typed prefix (Frau / Herr / Mr / Ms) wins; academic titles
    are skipped; a double name ("Hans-Peter") is judged as a whole, else by its first part."""
    tokens = (name or "").split()
    if not tokens:
        return "none"
    pre = _PREFIX.get(tokens[0].lower())
    if pre:
        return pre
    i = 0
    while i < len(tokens) - 1 and _ACADEMIC.match(tokens[i]):
        i += 1
    first = tokens[i].strip(".,")
    if len(tokens) - i < 2 or len(first) < 2:  # a lone token may be a surname -> no guess
        return "none"
    g = _gender(first)
    if g in ("andy", "unknown") and "-" in first:
        g = _gender(first.split("-", 1)[0])
    if g in ("male", "mostly_male"):
        return "mr"
    if g in ("female", "mostly_female"):
        return "ms"
    return "none"


def resolve_salutation(stated: Optional[str], name: Optional[str]) -> str:
    """Stated ms / mr / none wins; missing -> guessed from the name."""
    return stated if stated in ("ms", "mr", "none") else guess_salutation(name)


def greet(text: str, name: Optional[str], lang: str, salutation: Optional[str] = None) -> str:
    """Replace the vendored neutral opening line by the personal greeting. No name or a foreign opening
    -> text unchanged. salutation None / unknown -> "none" (full name, the greeting of before 0012)."""
    g = GREETING[lang]
    if not name or not text.startswith(g["plain"]):
        return text
    key = salutation if salutation in ("ms", "mr") else "none"
    personal = g[key].format(name=name, surname=surname(name))
    return personal + text[len(g["plain"]):]  # plain-text mail: no HTML escaping (name is control-char-stripped on input)


def salutation_label(salutation: Optional[str]) -> str:
    return SALUTATION_LABEL.get(salutation or "", "not given")


# --------------------------------------------------------------------------- models


class StayInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    licence: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    support: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    offer: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_amount"])
    users: Optional[float] = Field(default=None, ge=0, le=RULES["stay_cost"]["max_users"])


class SelfCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    lang: Literal["de", "en"]
    picks: list[Literal["mail", "call", "radar"]] = Field(min_length=1, max_length=3)
    answers: dict[str, str | list[str]] = Field(max_length=len(QUESTION_IDS))
    stay: Optional[StayInputs] = None
    rules_version: str = Field(max_length=40)
    consent: Literal[True]
    # optional (SPEC-021): only used to name the CRM lead; letters, digits, punctuation, no controls
    company: Optional[str] = Field(default=None, min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")
    company_website: Optional[str] = Field(default=None, max_length=200)
    # first + last name (CRM contact_name, result mail greeting); raw cap here, NAME_MAX after cleaning
    name: Optional[str] = Field(default=None, max_length=400)
    phone: Optional[str] = Field(default=None, max_length=200)
    # stated (optional); missing -> guessed from the name at submit (guess_salutation), or 422 if
    # SELFCHECK_SALUTATION_REQUIRED
    salutation: Optional[Salutation] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = clean_name(v)
        if not v:
            raise ValueError("name is blank")
        if len(v) > NAME_MAX:
            raise ValueError(f"name longer than {NAME_MAX} characters")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else clean_phone(v)

    @model_validator(mode="after")
    def _name_required(self):
        if self.name is None and SELFCHECK_NAME_REQUIRED:
            raise ValueError("name is required")
        if self.salutation is None and SELFCHECK_SALUTATION_REQUIRED:
            raise ValueError("salutation is required (ms, mr or none)")
        return self

    @field_validator("picks")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate pick")
        return v

    @field_validator("answers")
    @classmethod
    def _sizes(cls, v: dict) -> dict:
        for val in v.values():
            if isinstance(val, str) and len(val) > 40:
                raise ValueError("answer too long")
            if isinstance(val, list) and (len(val) > 12 or any(len(x) > 40 for x in val)):
                raise ValueError("answer too long")
        return v


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["started", "completed", "exited_early", "submitted"]
    lang: Literal["de", "en"] = "de"


# --------------------------------------------------------------------------- mail texts


def _picks_block(picks: list[str], lang: str) -> str:
    return "\n".join(f"- {t}" for t in engine.pick_titles(RULES, picks, lang))


# Signature the vendored mail texts end with; replaced by the site's footer (brand.replace_signature).
SIGNATURE = {lang: f"Ashant Chalasani\nSimplify-ERP, wapsol GmbH\nhttps://simplify-erp.de/{lang}/impressum"
             for lang in ("de", "en")}


def _site_url(lang: str, site: Optional[str] = None) -> str:
    return f"{brand.base_url(site, SELFCHECK_WEBSITE_URL)}/{lang}{RULES['route']}"


def _row_site(row) -> str:
    return brand.lead_site(row["site"] if "site" in row.keys() else "")


def doi_mail(site: Optional[str], lang: str, picks: list[str], link: str) -> tuple[str, str, Optional[str], list]:
    """(subject, plain text, branded HTML or None, inline attachments) of the DOI mail. Server text only
    (no name, no free text). HTML = the same paragraphs in the brand frame with the brand logo inline, the
    link as a "Bestätigen" button. No brand profile -> plain text only."""
    doi = RULES["mail"]["doi"]
    text = doi["body"][lang].format(picks=_picks_block(picks, lang), link=link, hours=CONFIRM_TOKEN_MAX_AGE // 3600,
                                    retention_pending_days=SELFCHECK_PENDING_DAYS)
    subject = doi["subject"][lang]
    plain = brand.replace_signature(text, site, lang, SIGNATURE[lang])
    b = brand.get(site)
    if b is None:
        return subject, plain, None, []
    core, footer_lines = _split_signature(text, site, lang)
    html_part, atts = mail_html.letter(b, lang, subject=subject, paragraphs=[x for x in core.split("\n\n") if x],
                                 footer_lines=footer_lines, link=link, link_label=confirm_page.BUTTON[lang])
    return subject, plain, html_part, atts


def _split_signature(text: str, site: Optional[str], lang: str) -> tuple[str, list[str]]:
    """(text without the vendored signature, signature lines = the site's footer or that signature):
    the HTML part shows the signature as its own block (mail_html.frame)."""
    sig = SIGNATURE[lang]
    core = text.rstrip()[: -len(sig)].rstrip() if text.rstrip().endswith(sig) else text.rstrip()
    footer = brand.footer(site, lang) or sig
    return core, footer.split("\n")


def result_mail(row: aiosqlite.Row) -> tuple[str, str, Optional[str], list]:
    """(subject, plain text, branded HTML or None, inline images). Plain text = the mail of before the
    HTML part, unchanged; the HTML twin renders the same engine values (mail_html.selfcheck_result).
    No brand profile -> no HTML part (plain text only, as before 0006)."""
    lang = row["lang"]
    site = _row_site(row)
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    prog = engine.progress(RULES, answers)
    m = RULES["mail"]["result"]
    url = _site_url(lang, site)
    head = (m["intro"] if prog["complete"] else m["partial"])[lang].format(
        answered=prog["answered"], total=prog["total"], url=url)
    head = greet(head, row["name"], lang, row["salutation"])
    result = engine.result_text(RULES, answers, lang, stay)
    outro = m["outro"][lang].format(url=url)
    body = "\n\n".join([head, result, outro])
    subject = m["subject"][lang]
    plain = brand.replace_signature(body, site, lang, SIGNATURE[lang])

    b = brand.get(site)
    if b is None:
        return subject, plain, None, []
    labels = {k: v[lang] for k, v in m["labels"].items()}
    # stage-2 sentence(s) exactly as the plain text has them (built inside the engine's result_text)
    stay_lines: list[str] = []
    if engine.stay_cost(RULES, stay):
        lines = result.split("\n")
        if labels["stay"] in lines:
            for line in lines[lines.index(labels["stay"]) + 1:]:
                if not line:
                    break
                stay_lines.append(line)
    greeting, _, intro = head.partition("\n\n")
    outro_core, footer_lines = _split_signature(outro, site, lang)
    parts = {
        "subject": subject, "greeting": greeting, "intro": [x for x in intro.split("\n\n") if x], "url": url,
        "base": brand.base_url(site, SELFCHECK_WEBSITE_URL), "complete": prog["complete"],
        "answered": prog["answered"], "total": prog["total"],
        "precision_word": RULES["precision"]["words"][prog["precision"]["level"]][lang],
        "ev": engine.evaluate(RULES, answers, lang) if prog["complete"] else None, "labels": labels,
        "stay_lines": stay_lines, "answers": engine.answer_lines(RULES, answers, lang),
        "outro": [x for x in outro_core.split("\n\n") if x], "footer_lines": footer_lines,
    }
    html_part, images = mail_html.selfcheck_result(b, lang, parts)
    return subject, plain, html_part, images


# Texts FOR THE OWNER (operator mail, crm.lead) are English (owner decision 2026-11-09): the engine
# renders answers / picks / precision with its "en" labels. Texts TO THE CONTACT (DOI, result mail,
# confirmation page) use the submission's lang.
OPERATOR_LANG = "en"
CHOSEN, NOT_CHOSEN = "chosen", "not chosen"


def _summary(row, radar_result: str, mail_sent: Optional[bool]) -> tuple[str, list[str]]:
    """(short pick titles, English summary lines) shared by the operator mail and the CRM lead."""
    picks = json.loads(row["picks"])
    answers = json.loads(row["answers"])
    prog = engine.progress(RULES, answers)
    prec = RULES["precision"]["words"][prog["precision"]["level"]][OPERATOR_LANG]
    short = ", ".join(engine.pick_titles(RULES, picks, OPERATOR_LANG))
    lines = [
        f"Self-Check #{row['id']} confirmed on {row['confirmed_at'] or _iso(_now())} (language {row['lang']}).",
        f"E-mail: {row['email']}",
    ]
    lines.append(f"Salutation: {salutation_label(row['salutation'])}")
    if row["name"]:
        lines.append(f"Name: {row['name']}")
    if row["phone"]:
        lines.append(f"Phone: {row['phone']}")
    if row["company"]:
        lines.append(f"Company: {row['company']}")
    lines += [
        f"Chosen: {short}",
        f"Indicative quote: {'yes' if 'mail' in picks else 'no'} ({prog['answered']} of {prog['total']} answers, precision {prec})",
        f"Introduction call (Meetly): {'yes, the person picks the slot' if 'call' in picks else 'no'}",
        f"Migration Radar: {radar_result}",
    ]
    if mail_sent is not None:
        state = "sent" if mail_sent else (NOT_CHOSEN if "mail" not in picks else "FAILED, please send it by hand")
        lines.append(f"Result mail to the person ({row['lang']}): {state}")
    return short, lines


def _body(row, lines: list[str]) -> str:
    answers = json.loads(row["answers"])
    stay = json.loads(row["stay"]) if row["stay"] else None
    return "\n".join(lines + ["", engine.result_text(RULES, answers, OPERATOR_LANG, stay), "",
                              f"Rules {row['rules_version']} (server {RULES['version']}). Retention {SELFCHECK_RETENTION_DAYS} days."])


def notify_mail(row, radar_result: str, mail_sent: bool, crm_line: str = "") -> tuple[str, str]:
    """Operator mail (English, for the owner): indicative-quote request + call yes/no + answers + CRM lead."""
    answers = json.loads(row["answers"])
    prog = engine.progress(RULES, answers)
    prec = RULES["precision"]["words"][prog["precision"]["level"]][OPERATOR_LANG]
    short, lines = _summary(row, radar_result, mail_sent)
    if crm_line:
        lines.append(crm_line)
    subject = RULES["mail"]["notify"]["subject"][OPERATOR_LANG].format(picks_short=short, precision=prec)
    return subject, _body(row, lines)


def lead_for(row, ref: str) -> Optional[dict]:
    """CRM lead of a confirmed self-check (SPEC-021): only for picks mail (Richtangebot) / call.
    Radar alone is a subscription, not a lead. Title from submitted fields only. English (owner)."""
    picks = json.loads(row["picks"])
    if not ({"mail", "call"} & set(picks)):
        return None
    answers = json.loads(row["answers"])
    labels = {o["id"]: o["label"][OPERATOR_LANG] for q in RULES["questions"] if q["id"] == "system" for o in q["options"]}
    picked = [labels[x] for x in (answers.get("system") or []) if x in labels]
    # short title: first system + count of the others (the full list is in the description)
    systems = picked[0] + (f" +{len(picked) - 1}" if len(picked) > 1 else "") if picked else ""
    who = row["company"] or row["name"] or row["email"]
    title = f"Self-Check: {systems} – {who}" if systems else f"Self-Check – {who}"
    _, lines = _summary(row, CHOSEN if "radar" in picks else NOT_CHOSEN, None)
    wanted = [t for p, t in zip(("mail", "call"), ("result and indicative quote by mail", "30-minute introduction (Meetly)")) if p in picks]
    lines.insert(2, "Requested: " + "; ".join(wanted))
    if picked:
        lines.insert(3, "Systems: " + " + ".join(picked))  # full list (title shows only the first)
    label = brand.label(_row_site(row), SELFCHECK_WEBSITE_URL)
    desc = _body(row, lines) + f"\n\nSource: {label} / Website / campaign selfcheck\nReference: {ref}"
    return {"ref": ref, "kind": "selfcheck", "campaign": "selfcheck", "title": title, "email": row["email"],
            "contact_name": row["name"], "salutation": row["salutation"], "partner_name": row["company"], "phone": row["phone"], "lang": row["lang"], "description": desc}


async def finish(db: aiosqlite.Connection, sync: dict, outcome: str) -> None:
    """lead_sync hook: store the Radar outcome, then the operator notification (always)."""
    sid = sync["origin_id"]
    async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    picks = json.loads(row["picks"])
    results = json.loads(sync["sub_results"] or "{}")
    if "radar" not in picks:
        radar = NOT_CHOSEN
    else:
        radar = results.get("radar") or f"error:{(sync['last_error'] or 'unknown')[:80]}"
    if sync["odoo_lead_id"]:
        crm = f"CRM lead (erp.wapsol): #{sync['odoo_lead_id']}"
    elif not sync["lead"]:
        crm = "CRM lead: none (Radar only)"
    elif outcome == "failed":
        crm = f"CRM lead: FAILED, please create it by hand ({(sync['last_error'] or '')[:120]})"
    else:
        crm = "CRM lead: not created (LEAD_SINK=none)"
    notify_sent = False
    if SELFCHECK_NOTIFY_TO:
        subject, body = notify_mail(row, radar, bool(row["mail_sent"]), crm)
        notify_sent = await mailer.send(SELFCHECK_NOTIFY_TO, subject, body, reply_to=row["email"],
                                        audience="internal", key=f"sc-notify-{sid}")
    await db.execute("UPDATE selfcheck_submission SET notify_sent = ?, radar_result = ? WHERE id = ?",
                     (int(notify_sent), radar, sid))
    await db.commit()
    logger.info("SELFCHECK_DELIVERED id=%s radar=%s lead=%s notify=%s", sid, radar, sync["odoo_lead_id"], notify_sent)


lead_sync.register("selfcheck", finish)


# --------------------------------------------------------------------------- router


def _global_key(*_args, **_kwargs) -> str:
    """Rate-limit key for the counter endpoint: one global bucket, the client IP is never read."""
    return "selfcheck-event"


def _origin_ok(request: Request) -> bool:
    return request.headers.get("origin", "").rstrip("/") in SELFCHECK_ALLOWED_ORIGINS


def _target(site: Optional[str], lang: str, ok: bool) -> str:
    """After the click: the site's explicit target > its website_url > SELFCHECK_WEBSITE_URL."""
    lang = brand.locale_for(site, lang)  # unknown locale -> the site's default_locale
    return brand.redirect_url(site, "selfcheck", lang, "confirmed" if ok else "error", env_base=SELFCHECK_WEBSITE_URL,
                              path=f"/{lang}{RULES['route']}", params={"bestaetigt": "1" if ok else "fehler"})


def _redirect(lang: str, ok: bool, status: int = 302, site: Optional[str] = None) -> RedirectResponse:
    return RedirectResponse(_target(site, lang, ok), status_code=status)


async def _pending(db: aiosqlite.Connection, token: Optional[str]):
    """Read-only token check (own salt, max_age, pending row, SHA-256 constant time). Row or None."""
    if not _signer or not token or len(token) > 1000:
        return None
    try:
        data = _signer.loads(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    sid = data.get("s") if isinstance(data, dict) else None
    if not isinstance(sid, int):
        return None
    async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    # single use: token hash is cleared on confirm; constant-time compare
    if row is None or row["status"] != "pending" or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], _hash(token)):
        return None
    return row


async def _submit_job(db: aiosqlite.Connection, payload: "SelfCheckRequest", clean: dict, site: str) -> None:
    """Contact-dependent part of POST /api/selfcheck (runs after the response is sent)."""
    await purge(db)
    email = payload.email.lower()
    since = _iso(_now() - timedelta(hours=24))
    stay = payload.stay.model_dump(exclude_none=True) if payload.stay else None
    prog = engine.progress(RULES, clean)
    # cap + insert in ONE statement (atomic, no COUNT-then-INSERT race; security review 2026-11-09)
    cur = await db.execute(
        "INSERT INTO selfcheck_submission (created_at, email, lang, picks, answers, stay, rules_version, complete, company, "
        "site, name, phone, salutation) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
        "WHERE (SELECT COUNT(*) FROM selfcheck_submission WHERE email = ? AND created_at >= ?) < ?",
        (_iso(_now()), email, payload.lang, json.dumps(payload.picks), json.dumps(clean, ensure_ascii=False),
         json.dumps(stay) if stay else None, payload.rules_version, int(prog["complete"]),
         " ".join(payload.company.split()) if payload.company else None, site, payload.name, payload.phone,
         resolve_salutation(payload.salutation, payload.name), email, since, MAX_PENDING_PER_EMAIL),
    )
    if cur.rowcount != 1:
        await db.commit()  # end the (empty) implicit transaction
        logger.info("SELFCHECK_THROTTLED")
        return
    sid = cur.lastrowid
    token = _signer.dumps({"s": sid, "b": site})  # b: brand of the neutral page only
    await db.execute("UPDATE selfcheck_submission SET token_hash = ? WHERE id = ?", (_hash(token), sid))
    await db.commit()

    subject, body, html_part, atts = doi_mail(site, payload.lang, payload.picks,
                                              f"{PUBLIC_BASE_URL}/api/selfcheck/confirm?token={token}")
    ok = await mailer.send(email, subject, body, html=html_part, attachments=atts, audience="doi", key=f"sc-doi-{sid}",
                           sender=brand.sender(site))
    if not ok:
        await db.execute("DELETE FROM selfcheck_submission WHERE id = ?", (sid,))
        await db.commit()
        logger.error("SELFCHECK_DOI_MAIL_FAILED id=%s", sid)
        return
    logger.info("SELFCHECK_PENDING id=%s picks=%s complete=%s", sid, ",".join(payload.picks), prog["complete"])


async def submit(payload: SelfCheckRequest, request: Request) -> JSONResponse:
    """POST /api/selfcheck and POST /api/lead {kind: selfcheck}."""
    if not _origin_ok(request):
        return JSONResponse({"status": "forbidden"}, status_code=403)
    if payload.company_website:  # honeypot: looks like success, nothing stored or sent
        return JSONResponse(PENDING)
    clean, errors = engine.validate_answers(RULES, payload.answers)
    if errors:
        return JSONResponse({"status": "invalid", "detail": errors[:5]}, status_code=422)
    if not _signer or not PUBLIC_BASE_URL or not mailer.configured():
        logger.error("SELFCHECK_NOT_CONFIGURED")
        return JSONResponse({"status": "service_unavailable"}, status_code=503)

    # From here on the answer is the same for every address, immediately (no enumeration via
    # content, status code or timing); storage + DOI mail run as a job after the response.
    site = brand.site_for_origin(request.headers.get("origin"))
    jobs.spawn(_submit_job(request.app.state.db, payload, clean, site), name="selfcheck_submit")
    return JSONResponse(PENDING)


def build_router(*, limiter, admin_ok: Callable[[Request], bool]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/selfcheck")
    @limiter.limit(SELFCHECK_RATE_LIMIT)
    async def selfcheck_submit(payload: SelfCheckRequest, request: Request):
        return await submit(payload, request)

    @router.get("/api/selfcheck/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def selfcheck_confirm_page(request: Request, token: str = Query(..., min_length=1, max_length=1000)):
        """DOI link, step 1 READ-ONLY (mail scanners prefetch links): confirmation page with a
        "Bestätigen" button (POST). Invalid / expired / used: uniform neutral page."""
        if not _signer:
            return _redirect("", False, site=brand.DEFAULT_SITE)
        row = await _pending(request.app.state.db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        site = _row_site(row)
        return confirm_page.render("selfcheck", row["lang"], "/api/selfcheck/confirm", token,
                                   [_target(site, row["lang"], True), _target(site, row["lang"], False)],
                                   brand=brand.get(site))

    @router.post("/api/selfcheck/confirm")
    @limiter.limit(os.environ.get("CONFIRM_RATE_LIMIT", "30/minute"))
    async def selfcheck_confirm(request: Request):
        """DOI step 2, the human click: claims the token atomically (single use), result mail, then
        Radar + CRM lead + operator mail via the outbox; 303 back to the website."""
        if not _signer:
            return _redirect("", False, status=303, site=brand.DEFAULT_SITE)
        if not confirm_page.same_origin(request):
            logger.warning("SELFCHECK_CONFIRM_FOREIGN_ORIGIN")
            return confirm_page.neutral(403)
        token = await confirm_page.form_token(request)
        db: aiosqlite.Connection = request.app.state.db
        row = await _pending(db, token)
        if row is None:
            return confirm_page.neutral(brand=brand.get(confirm_page.token_site(_signer, token)))
        sid = row["id"]
        confirmed_at = _iso(_now())
        # burn the token first, conditionally: a parallel second POST cannot trigger a second round of mails
        cur = await db.execute(
            "UPDATE selfcheck_submission SET status = 'confirmed', confirmed_at = ?, token_hash = NULL "
            "WHERE id = ? AND status = 'pending' AND token_hash = ?",
            (confirmed_at, sid, _hash(token)),
        )
        claimed = cur.rowcount == 1
        await db.commit()
        if not claimed:
            return confirm_page.neutral(brand=brand.get(_row_site(row)))
        async with db.execute("SELECT * FROM selfcheck_submission WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
        site = _row_site(row)

        picks = json.loads(row["picks"])
        mail_sent = False
        if "mail" in picks:
            subject, body, html_part, images = result_mail(row)
            # address confirmed by the DOI click: branded HTML + inline logo (later the report PDF) allowed
            mail_sent = await mailer.send(row["email"], subject, body, html=html_part, attachments=images,
                                          audience="confirmed", key=f"sc-result-{sid}", sender=brand.sender(site))
        await db.execute("UPDATE selfcheck_submission SET mail_sent = ? WHERE id = ?", (int(mail_sent), sid))
        await db.commit()

        # Radar subscription + CRM lead + operator mail: outbox with retries (lead_sync, SPEC-021)
        import leads_api  # local: leads_api imports this module (shared route_confirmed)
        ref = lead_sync.new_ref()
        page_url = _site_url(row["lang"], site)
        subscribe = {"email": row["email"], "lang": row["lang"], "source": "selfcheck", "page_url": page_url,
                     "name": row["name"], "company": row["company"], "lists": ["radar"] if "radar" in picks else []}
        await leads_api.route_confirmed(
            db, origin="selfcheck", origin_id=sid, site=site, action="selfcheck", lead=lead_for(row, ref),
            subscribe=subscribe, data={"ref": ref, "email": row["email"], "lang": row["lang"], "picks": picks,
                                       "name": row["name"], "salutation": row["salutation"], "phone": row["phone"],
                                       "company": row["company"],
                                       "source": "selfcheck", "page_url": page_url})
        logger.info("SELFCHECK_CONFIRMED id=%s mail=%s site=%s", sid, mail_sent, site)
        return _redirect(row["lang"], True, status=303, site=site)

    @router.post(EVENT_PATH, status_code=204)
    @limiter.limit(SELFCHECK_EVENT_RATE_LIMIT, key_func=_global_key)
    async def selfcheck_event(payload: EventRequest, request: Request):
        if not _origin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        db: aiosqlite.Connection = request.app.state.db
        await db.execute(
            "INSERT INTO selfcheck_counter (day, event, lang, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(day, event, lang) DO UPDATE SET count = count + 1",
            (_now().date().isoformat(), payload.event, payload.lang),
        )
        await db.commit()
        return Response(status_code=204)

    @router.get("/api/selfcheck/submissions")
    async def selfcheck_submissions(
        request: Request,
        status: Literal["pending", "confirmed"] = Query("confirmed"),
        since: Optional[str] = Query(None, max_length=40),
        limit: int = Query(100, ge=1, le=500),
    ):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sql = ("SELECT s.id, s.created_at, s.confirmed_at, s.email, s.salutation, s.name, s.phone, s.company, s.lang, s.picks, s.answers, s.stay, "
               "s.rules_version, s.complete, s.mail_sent, s.notify_sent, s.radar_result, "
               "l.odoo_lead_id, l.state AS crm_state FROM selfcheck_submission s "
               "LEFT JOIN lead_sync l ON l.origin = 'selfcheck' AND l.origin_id = s.id WHERE s.status = ?")
        params: list = [status]
        if since:
            sql += " AND COALESCE(s.confirmed_at, s.created_at) >= ?"
            params.append(since)
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.append(limit)
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("picks", "answers", "stay"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return JSONResponse({"submissions": out, "count": len(out), "retention_days": SELFCHECK_RETENTION_DAYS})

    @router.get("/api/selfcheck/stats")
    async def selfcheck_stats(request: Request, days: int = Query(30, ge=1, le=366)):
        if not admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        since = (_now().date() - timedelta(days=days - 1)).isoformat()
        db: aiosqlite.Connection = request.app.state.db
        async with db.execute(
            "SELECT day, event, lang, count FROM selfcheck_counter WHERE day >= ? ORDER BY day, event, lang", (since,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        totals: dict[str, int] = {}
        for r in rows:
            totals[r["event"]] = totals.get(r["event"], 0) + r["count"]
        return JSONResponse({"since": since, "totals": totals, "rows": rows})

    return router
