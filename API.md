# API Reference

Base URL: `https://mauxy.engage.wapsol.de` (canonical host; `DEPLOY_DOMAIN`, `PUBLIC_BASE_URL`). The old
`unsubscribe.engage.wapsol.de` is only an alias for old mail links.

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

Lead CTAs (`POST /api/lead`, `GET|POST /api/lead/confirm`, admin endpoints) and the Selbst-Check are
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
site + segment), DOI mail with a signed, single-use link to `GET /api/confirm`. Throttle = ONE
conditional `INSERT ... SELECT ... WHERE (SELECT COUNT(*) ...) < cap` (atomic, parallel requests cannot
pass it; same for leads, Selbst-Check and the mailer's per-recipient limit).

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

## DOI mail links: GET page, POST click (all three flows)

Security review 2026-11-09: mail scanners (Safe Links, Proofpoint, Mimecast) GET every link, some run
JS. So the mail link never confirms by itself (`confirm_page.py`):

| Flow | GET (mail link, read-only) | POST (the click) → 303 |
|---|---|---|
| Newsletter | `/api/confirm?token=` | `/api/confirm` → `<base>/{lang}/newsletter/confirmed` |
| CTA lead | `/api/lead/confirm?token=` | `/api/lead/confirm` → `<base>/{lang}/anfrage/bestaetigt?art=<kind>` |
| Selbst-Check | `/api/selfcheck/confirm?token=` | `/api/selfcheck/confirm` → `<base>/{lang}/selbst-check?bestaetigt=1` |

`<base>` per site (db-patches/0006): the brand profile's explicit `redirects` URL (flow parameter appended)
> its `website_url` > the env URL (`PUBLIC_WEBSITE_BASE_URL`, `LEAD_WEBSITE_URL`, `SELFCHECK_WEBSITE_URL`) =
the behaviour before 0006 for sites without a profile. Page and neutral page carry the site's brand
(README "Per site").

- **GET** validates the token only (signature + max age + unused pending row, SHA-256 constant time;
  no burn, no sink / lead write, no action_log line) and renders a minimal page (de/en by the
  submission's locale) with ONE form: hidden `token`, button "Bestätigen" / "Confirm", `method=post`,
  no JS. Headers: CSP `default-src 'none'; style-src 'sha256-…'; form-action 'self' <website origin>;
  base-uri 'none'; frame-ancestors 'none'`, `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. HEAD → 405.
- **POST** `application/x-www-form-urlencoded`, field `token` (exactly once, body ≤ 2048 bytes). Origin
  (else Referer), when present, must be mauxy's own host (`PUBLIC_BASE_URL` / request Host); foreign or
  `null` → `403` neutral page. Then the token is claimed atomically (conditional UPDATE: a parallel
  second POST loses) and the write happens: newsletter → subscriber sink (Odoo `mailing.contact` /
  `mailing.subscription` on `ODOO_LIST_NEWSLETTER_ID`; Mautic: tag + segment); lead → outbox
  (`crm.lead` + newsletter if ticked); Selbst-Check → result mail + outbox (Radar, lead, operator mail).
- **Invalid / expired / used / foreign token**, GET or POST: one neutral page (`200`, identical bytes
  for every reason and flow, bilingual).
- Newsletter only: sink down → claim released, token stays valid, 303 to
  `…/newsletter/confirm-error?reason=service_unavailable`; blacklisted → burnt,
  `…?reason=unavailable`. Misconfigured (no signer / website URL) → `…?reason=server_misconfigured`
  (leads: `/{lang}/anfrage/fehler`, Selbst-Check: `?bestaetigt=fehler`).
- Website contract unchanged: same confirmed / error pages on the website; only the mail link flow
  gained the click.

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
| 200 | `{"status":"ok"}` | Applied — or contact not found. Same body either way (no enumeration leak). Routed site: queued to every target (outbox, retries) |
| 401 | `{"error":"unauthorized"}` | Missing/unknown site key |
| 409 | `{"status":"quiz_failed"}` | Wrong/expired bot-defence answer (email-independent → still no leak) |
| 503 | `{"status":"service_unavailable"}` | Subscriber sink unreachable (sites WITHOUT routes only) |

A site with routes (`/api/admin/routes`) fans the opt-out out to every receiver that may hold the address
(README "Per site"); the per-target outcome is in `GET /api/admin/deliveries`.

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

### Brands — `/api/admin/brands` (per-site brand profile, db-patches/0006)
- `GET` — all profiles (`brands`, `count`, `default_site`); the logo only as `{mime, bytes, sha256}`.
- `GET /api/admin/brands/{site}` — one profile; `404` if none.
- `PUT /api/admin/brands/{site}` — create (`201`, `display_name` required) or update (`200`). Only the fields
  sent change; `null` clears an optional field; `mail_footer`, `redirects`, `origins`, `logo` replace the
  stored set. `400` (rule violated, message says which), `422` (shape / pattern / unknown field).
  ```json
  {"display_name": "re-cloud", "legal_entity": "wapsol GmbH", "color_primary": "#0b5fff", "color_accent": "#9cc3ff",
   "wordmark": "re-cloud", "website_url": "https://re-cloud.io", "imprint_url": "https://re-cloud.io/{lang}/impressum",
   "privacy_url": "https://re-cloud.io/{lang}/datenschutz", "default_locale": "de",
   "mail_from_name": "re-cloud", "mail_from_address": "hello@re-cloud.io", "mail_reply_to": "info@re-cloud.io",
   "mail_footer": {"de": "re-cloud\nwapsol GmbH", "en": "re-cloud\nwapsol GmbH"},
   "redirects": [{"flow": "newsletter", "locale": "de", "outcome": "confirmed", "url": "https://re-cloud.io/de/danke"}],
   "origins": ["https://re-cloud.io", "https://www.re-cloud.io"],
   "logo": {"mime": "image/png", "data": "<base64, <= 64 KiB>"}}
  ```
  Rules: colours `#RRGGBB`; URLs https (http only on `10.0.99.1`), no user info / quotes, `{lang}` only in
  imprint / privacy / redirects; logo PNG / WebP (magic bytes, size cap; no SVG); `mail_from_address` domain in
  `MAIL_SENDER_DOMAINS`; one-line texts without `<>` / controls; footer <= 1000 chars; origin unique per site.
- `DELETE /api/admin/brands/{site}` — profile + footers + redirects + origins; the site falls back to the
  generic look.
- `GET /api/admin/brands/{site}/preview?flow=newsletter|lead|selfcheck|neutral&lang=de|en` — the page as the
  person sees it (dummy token; a click lands on the neutral page).

### Routes — `/api/admin/routes` (per-site action routing)
- `GET [?site=…]` — `routes` (`{site, action, position, sink, params, enabled}`), `actions` (allowed sinks per
  action), `webhook_allowed_hosts`.
- `PUT /api/admin/routes/{site}/{action}` `{"targets": [{sink, params?, enabled?}, …]}` — replaces the ordered
  list (1..10). Actions `subscribe`, `unsubscribe`, `lead`, `selfcheck`; sinks `odoo` (lead / selfcheck: max
  one), `mautic`, `webhook` (url host in `WEBHOOK_ALLOWED_HOSTS`, `secret_ref` -> env `WEBHOOK_SECRET_<REF>`
  must exist, >= 32 chars). `400` with the reason, `422` unknown action / bad site name.
  ```json
  {"targets": [{"sink": "odoo", "params": {"lists": {"newsletter": 12}}},
               {"sink": "webhook", "params": {"url": "https://flywheel.example/hooks/mauxy", "secret_ref": "flywheel"}}]}
  ```
- `DELETE /api/admin/routes/{site}/{action}` — back to the fallback sinks; `404` if there were none.

### Opt-outs — `/api/admin/optouts` (mauxy's per-site do-not-contact record, patch 0007)
- `POST /api/admin/optouts/{site}/check` `{"email"}` → `{"opted_out": bool}` (address in the body, never in a URL).
- `POST /api/admin/optouts/{site}/lift` `{"email"}` → `{"lifted": true}` / `404`. Only on the person's explicit
  request (re-subscribe); written automatically on every unsubscribe, read by the do-not-contact gate of routed
  sites (README "Per site").

### Deliveries — `/api/admin/deliveries` (fan-out outbox)
- `GET [?state=queued|done|failed&site=…&action=…&limit=100]` — per-target rows (`sink`, `params`, `state`,
  `result` (`dnc_skip` = do-not-contact gate), `attempts`, `last_error`, …; never the payload).
- `POST /api/admin/deliveries/{id}/retry` — failed -> queued (attempts reset); `409` if not failed or purged.

---

## GET /health · GET /ready · GET /health/detail

Kubernetes probes. `/health` is the **liveness** probe — always HTTP 200, `{"status":"ok"}`,
independent of every downstream (sinks, Mautic, SMTP), so an outage never restarts pods.
`/ready` is the **readiness** probe — HTTP 200 `{"status":"ready"}` when the local SQLite DB
answers, else 503. It is deliberately NOT tied to a sink (upstream tied it to Mautic): subscribe,
confirm, leads and login keep working while erp.wapsol / Mautic is down (pending rows + outbox in
SQLite, retries). `/health/detail` (operator, always HTTP 200): `status ok|degraded`, `lead_sink`,
`subscriber_sink` (reachability, cached 30 s), `brands`, `routes` (counts), `webhooks` (`<n> allowed host(s)` |
`disabled`), `deliveries_failed`, `auth_linkedin`, `sites` (count), `quiz` (`configured|disabled`),
`mautic_version` (`unknown` if unset), `cache_age_seconds`. Never secrets.

---

## Errors (all endpoints)

| Status | Cause |
|--------|-------|
| 401    | Missing or invalid Bearer token (site key or admin key) |
| 403    | `ADMIN_API_KEY` not configured (admin endpoint disabled) |
| 409    | Bot-defence quiz failed; admin: key / question exists, delivery not retryable |
| 422    | Invalid request body (e.g. malformed email on `/api/unsubscribe`) |
| 429    | Rate limit exceeded -- retry after the period resets |
| 503    | Service not configured, or subscriber sink unreachable on `/api/unsubscribe` |

---

## LinkedIn login: /api/auth/linkedin/start, /api/auth/linkedin/callback, POST /api/auth/session

Sign-in only (`openid profile email`). See README "LinkedIn login" for the flow. Start and callback on the
same host: redirect URI `https://mauxy.engage.wapsol.de/api/auth/linkedin/callback` (default
`PUBLIC_BASE_URL` + path; must equal the LinkedIn portal entry).

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

