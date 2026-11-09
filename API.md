# API Reference

Base URL: `https://mauxy.engage.wapsol.de` (set via `DEPLOY_DOMAIN`)

Mauxy is the single newsletter (un)subscribe authority for all sites. It owns the double
opt-in itself (mail via SMTP, nothing reaches a sink before the click) and then delivers to the
subscriber sink (Odoo Email Marketing lists by default; Mautic only as the deprecated
`SUBSCRIBER_SINK=mautic`). Every `/api/subscribe` and `/api/unsubscribe` call needs **two** things:

1. **A per-site key** — `Authorization: Bearer <site-key>`. Each site is configured
   in the `sites` table (seeded from `MAUXY_SITES`) with its own key, `segment` and quiz `topic`. Send it
   over HTTPS (TLS is the MITM defence). Unknown/missing key → `401`.
2. **A bot-defence answer** — fetch a question from `GET /api/challenge`, then echo
   the `challenge` token and the correct `answer` index back. Wrong / expired / issued for
   another site's topic → `409`.

The honeypot field `company_website` must stay empty; if set, the request is
accepted-looking but silently dropped.

Lead CTAs (`POST /api/lead`, `GET /api/lead/confirm`, admin endpoints) and the Selbst-Check are
documented in the README ("CTA leads", "Selbst-Check"). Unsubscribe below works on the active
subscriber sink (Odoo mailing lists by default; Mautic DNC only with the deprecated Mautic sink).

---

## GET /api/challenge

Return a multiple-choice question for the calling site's topic plus a signed,
expiring token. The correct answer is never sent to the client.

**Auth:** `Authorization: Bearer <site-key>`. **Rate limit:** `CHALLENGE_RATE_LIMIT` (default 30/min per IP).

### Response (200)

```json
{
  "challenge": "odoo.editions.1764500000.6f1c…",
  "question": "Which two editions does Odoo come in?",
  "choices": ["Community & Enterprise", "Bronze & Gold", "Lite & Pro", "Home & Away"]
}
```

`answer` (sent later) is the 0-based index into `choices`. Token expires after
`CHALLENGE_TTL` seconds (default 600). `401` if the key is unknown; `503` if the
quiz is unconfigured (`CHALLENGE_SECRET` unset or no questions for the topic).

---

## POST /api/subscribe

Newsletter signup with double opt-in. After the gates the answer is ALWAYS an immediate
`200 {"status":"pending_confirmation"}` (same status, body and timing for every address); the
contact-dependent work runs after the response: read-only sink lookup (skip if already subscribed /
blocked), per-address throttle (`MAX_PENDING_PER_EMAIL` per 24 h), pending row in SQLite (with
site + segment), DOI mail with a signed, single-use link to `GET /api/confirm`.

**Auth:** `Authorization: Bearer <site-key>`. **Rate limit:** `SUBSCRIBE_RATE_LIMIT` (default 60/min per IP).

### Request

| Header        | Value                  |
|---------------|------------------------|
| Content-Type  | application/json       |
| Authorization | Bearer `<site-key>`    |

```json
{
  "email": "user@example.com",
  "locale": "de",
  "source": "footer",
  "consent": true,
  "pageUrl": "https://simplify-erp.de/de/",
  "challenge": "simplify-erp.angebot.1764500000.6f1c…",
  "answer": 0,
  "company_website": ""
}
```

`consent` must be `true`, `locale` `de|en`, `source` `[a-z0-9-]` (max 40), `pageUrl` on an
`ALLOWED_ORIGINS` origin (max 500). `consentedAt` is accepted but never stored (the consent time is
the server time of the click). The segment comes from the site key, never the body.

### Responses

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"status":"pending_confirmation"}` | Every address-specific outcome (new, already subscribed, blocked, throttled, sink down) and the honeypot |
| 401 | `{"error":"unauthorized"}` | Missing/unknown site key |
| 409 | `{"status":"quiz_failed"}` | Wrong/expired bot-defence answer — re-fetch a challenge and retry |
| 422 | validation detail | Invalid payload (incl. `consent` not `true`, foreign `pageUrl`) |
| 429 | rate limit | |
| 503 | `{"status":"service_unavailable"}` | DOI not configured (no signer / `PUBLIC_BASE_URL` / mailer) — independent of the address |

### Example

```bash
curl -X POST https://mauxy.engage.wapsol.de/api/subscribe \
  -H "Authorization: Bearer YOUR_SITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","locale":"de","source":"footer","consent":true,"pageUrl":"https://simplify-erp.de/de/","challenge":"<token>","answer":0}'
```

## GET /api/confirm?token=…

Link in the DOI mail. Always a 302 to `PUBLIC_WEBSITE_BASE_URL/{lang}/newsletter/confirmed` or
`…/newsletter/confirm-error?reason=invalid_or_expired|unavailable|service_unavailable|server_misconfigured`.
Signature + max age + unused pending row (SHA-256, constant time); then the subscriber sink subscribes
(Odoo: `mailing.contact` / `mailing.subscription` on `ODOO_LIST_NEWSLETTER_ID`; Mautic: tag + the site's
segment). Sink down → token stays valid (retry); blocked → token burnt.

---

## POST /api/unsubscribe

Opt-out via the subscriber sink (Odoo: `opt_out` on the address' subscriptions of the configured
lists; deprecated Mautic: DNC list).

**Auth:** `Authorization: Bearer <site-key>`. **Rate limit:** `RATE_LIMIT` (default 5/min per IP).

### Request

```json
{
  "email": "user@example.com",
  "challenge": "odoo.editions.1764500000.6f1c…",
  "answer": 0,
  "company_website": ""
}
```

### Responses

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"status":"ok"}` | Applied — or contact not found. Same body either way (no enumeration leak). |
| 401 | `{"error":"unauthorized"}` | Missing/unknown site key |
| 409 | `{"status":"quiz_failed"}` | Wrong/expired bot-defence answer (email-independent → still no leak) |
| 503 | `{"status":"service_unavailable"}` | Subscriber sink unreachable |

---

## GET /api/actions

Query the action log. Admin-only, `Authorization: Bearer <ADMIN_API_KEY>`. `403` if
`ADMIN_API_KEY` unset, `401` if wrong. Query params: `email`, `result`, `limit`
(1–500, default 50), `offset`. Each row carries `action`
(`subscribe`|`confirm`|`unsubscribe`) and `site`. `result` values include `subscribe_ok`,
`subscribe_already_confirmed`, `subscribe_dnc_skip`, `subscribe_throttled`, `subscribe_honeypot`,
`confirm_ok`, `confirm_invalid`, `ok`, `not_found`, `quiz_failed`, `honeypot`, `sink_error`,
`sink_unreachable`, `error`.

---

## Admin: runtime config CRUD

The site registry, the quiz bank and the target Mautic version live in SQLite and are
managed at runtime — no rebuild/redeploy. All endpoints require
`Authorization: Bearer <ADMIN_API_KEY>` (`403` if unset, `401` if wrong). Every write
reloads the in-memory caches, so changes are live on the next request. `MAUXY_SITES` seeds
the site keys and `db-patches/0002_seed_quiz.sql` seeds the quiz on first boot; the DB is
authoritative after.

### Sites — `/api/admin/sites`
- `GET` — list all sites (`{key, site, segment, topic, created_at}`).
- `POST` `{key, site, segment, topic}` — onboard a site (key >= 16 chars). `201` / `409` if key exists.
- `PUT /api/admin/sites/{key}` `{site?, segment?, topic?}` — update; `404` if unknown.
- `DELETE /api/admin/sites/{key}` — remove; `404` if unknown.

> Site Bearer keys are stored in **plaintext** in SQLite (deliberate; TLS + RBAC on the
> PVC are the controls). `GET` returns them so an admin can manage them.

### Quiz — `/api/admin/quiz`
- `GET [?topic=…]` — list questions (`{topic, qid, question, choices, answer}`).
- `POST` `{topic, id, q, choices, answer}` — add. `201` / `409` if `(topic,id)` exists / `400` if `answer` out of range / `422` if `topic` or `id` is not `[a-z0-9-]` (a `.` would break the token format).
- `PUT /api/admin/quiz/{topic}/{qid}` `{topic, id, q, choices, answer}` — replace text/choices/answer; `404` if unknown.
- `DELETE /api/admin/quiz/{topic}/{qid}` — remove; `404` if unknown.

### Mautic instance — `/api/admin/mautic-instance`
- `GET` — `{base_url, version, source, updated_at}` (source `manual`|`unset`).
- `PUT` `{version, base_url?}` — set the version manually (`source=manual`); takes effect immediately.

---

## GET /health · GET /ready · GET /health/detail

Kubernetes probes. `/health` is the **liveness** probe — always HTTP 200, `{"status":"ok"}`,
independent of every downstream (sinks, Mautic, SMTP), so an outage never restarts pods.
`/ready` is the **readiness** probe — HTTP 200 `{"status":"ready"}` when the local SQLite DB
answers, else 503. It is deliberately NOT tied to a sink (upstream tied it to Mautic): subscribe,
confirm, leads and login keep working while erp.wapsol / Mautic is down (pending rows + outbox in
SQLite, retries). `/health/detail` (operator, always HTTP 200): `status ok|degraded`, `lead_sink`,
`subscriber_sink` (reachability, cached 30 s), `auth_linkedin`, `sites` (count), `quiz`
(`configured|disabled`), `mautic_version` (`unknown` if unset), `cache_age_seconds`. Never secrets.

---

## Errors (all endpoints)

| Status | Cause |
|--------|-------|
| 401    | Missing or invalid Bearer token (site key or admin key) |
| 403    | `ADMIN_API_KEY` not configured (admin endpoint disabled) |
| 409    | Bot-defence quiz failed |
| 422    | Invalid request body (e.g. malformed email on `/api/unsubscribe`) |
| 429    | Rate limit exceeded -- retry after the period resets |
| 503    | Service not configured, or subscriber sink unreachable on `/api/unsubscribe` |

---

## LinkedIn login: /api/auth/linkedin/start, /api/auth/linkedin/callback, POST /api/auth/session

Sign-in only (`openid profile email`). See README "LinkedIn login" for the flow.

- `GET /api/auth/linkedin/start?lang=de|en&return=/de/...&binding=<43+ url-safe chars>`: 302 to LinkedIn
  + `Set-Cookie: __Host-mauxy_li` (HttpOnly, Secure, SameSite=Lax, 10 min). 400 `{"status": "invalid_request"}`
  for a bad lang / binding / return path (anything but a same-site path).
- `GET /api/auth/linkedin/callback` (LinkedIn only): always 302 to `{site}/{lang}/auth/callback?code=<one-time>`
  or `?error=<code>` (`invalid_state`, `login_failed`, `unavailable`, LinkedIn codes such as `user_cancelled_login`).
- `POST /api/auth/session` `{"code": "...", "binding": "..."}` from the site (CORS, Origin = the site):
  200 `{"status": "ok", "profile": {sub, name, given_name, family_name, email, email_verified},
  "session": "<token, 1 h>", "expires_at": "<ISO>", "return": "/de/..."}`; every failure 400
  `{"status": "invalid_code"}`; 422 malformed body; 429 rate limit; 503 login not configured.

---

## /api/selfcheck*, /api/lead*

See README "Selbst-Check" and "CTA leads". All contact-specific outcomes of `POST /api/selfcheck` and
`POST /api/lead` return `200 {"status": "pending_confirmation"}`; 422 = invalid payload, 403 = origin not
allowed, 429 = rate limit, 503 = DOI not configured. Sink/mail errors after validation are never
returned (they run after the response, see jobs.py). These endpoints use their Origin allowlists, not
the per-site key / quiz.

