# Mauxy

The single newsletter (un)subscribe authority for all sites (ludo, simplify-erp, re-cloud, ...) and
the website's interface to other systems (owner decision 2026-11-08, flywheel SPEC-021): public
consent service of simplify-erp.de for newsletter, Migration Radar, Selbst-Check and every lead CTA
(contact form, app inquiry, webinar registration, shop checkout), plus the visitor **LinkedIn
login** (sign-in only, replaces app2li; decision 2026-11-08). Confirmed data goes to **sinks**:
`crm.lead` and Email Marketing lists in erp.wapsol (Odoo 18, default). Mautic is a deprecated sink
(still used by ludo) until it is shut down. Credentials stay server-side.

Guards in front of every newsletter (un)subscribe (upstream "newsletter authority"):

1. **Per-site key** — `Authorization: Bearer <site-key>`. Each calling site has its own key,
   `segment`, quiz `topic` and **policy**, held in SQLite (`sites`, seeded from `MAUXY_SITES`,
   admin CRUD). Sent over HTTPS (TLS = the MITM defence). Segment and sink come from the key,
   never from the body. No key -> `401`, for every site (no unauthenticated path).
2. **Bot-defence quiz** — `GET /api/challenge`, send back `challenge` + `answer`. Wrong / expired /
   other site's topic -> `409`. Honeypot `company_website` silently drops bots.

Per-site policy (`db-patches/0004_site_policy.sql`):

| Field | Values | Meaning |
|---|---|---|
| `subscribe_quiz` | 1 (default) / 0 | 0 = subscribe without quiz, the site relies on mauxy's double opt-in. Unsubscribe ALWAYS needs the quiz. |
| `subscriber_sink` | `odoo` / `mautic` / `none` / NULL | NULL = global `SUBSCRIBER_SINK` |
| `doi_owner` | `mauxy` (default) / `sink` | `mauxy`: DOI mail from mauxy, nothing reaches a sink before the click. `sink`: upstream behaviour, contact into the Mautic segment at once, a Mautic campaign sends the opt-in (Mautic sink only). |

Prod after patch 0004: `simplify-erp` = quiz off on subscribe, Odoo, mauxy DOI; every other existing
site (ludo, ...) = quiz on, Mautic, `doi_owner=sink` (exactly the upstream behaviour).

**Key design property (mauxy-DOI sites):** every contact-specific request gets the same answer
(`pending_confirmation` / `ok`), immediately, whatever the address -- no enumeration through
content, status or timing. Nothing is written to any sink before the confirmation link is clicked.

| Area | Endpoints | Code |
|---|---|---|
| Newsletter | `GET /api/challenge`, `POST /api/subscribe`, `GET /api/confirm`, `POST /api/unsubscribe` | `main.py` |
| Admin | `/api/actions`, `/api/admin/sites`, `/api/admin/quiz`, `/api/admin/mautic-instance` | `main.py` |
| Selbst-Check | `/api/selfcheck*` | `selfcheck_api.py` |
| CTA leads | `POST /api/lead`, `GET /api/lead/confirm`, admin `/api/lead/submissions`, `/api/lead/sync/{id}/retry` | `leads_api.py` |
| Delivery after DOI | outbox with retries | `lead_sync.py` |
| Sinks | Odoo lead + subscriber sink, deprecated Mautic sink, per-site subscriber sink | `sinks.py`, `mautic_sink.py` |
| Odoo | the one Odoo client (JSON-RPC, allowlist) | `odoo_client.py` |
| LinkedIn login | `GET /api/auth/linkedin/start`, `GET /api/auth/linkedin/callback`, `POST /api/auth/session` | `auth_api.py` |
| Schema | `db-patches/`, `db_migrate.py`, `scripts/sqlite_db.py` | |

---

## For Developers -- Integrating

See [API Reference](API.md) for full endpoint documentation. The flow is the same
for subscribe and unsubscribe: **challenge → answer** (exception: subscribe on a site with
`subscribe_quiz=0`, see "Website contract"). The example is the upstream (ludo) style.

```javascript
const BASE = "https://mauxy.engage.wapsol.de";
const KEY = "<your-site-key>";            // server-side secret; static sites embed a per-site key

// 1) Fetch a question, show choices, let the user pick an index.
const ch = await fetch(`${BASE}/api/challenge`, {
  headers: { Authorization: `Bearer ${KEY}` },
}).then((r) => r.json());          // { challenge, question, choices }

// 2) Submit with the chosen answer index.
async function subscribe(email, answerIndex) {
  const resp = await fetch(`${BASE}/api/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
    body: JSON.stringify({ email, challenge: ch.challenge, answer: answerIndex, company_website: "" }),
  });
  if (resp.status === 409) return { ok: false, reason: "quiz_failed" };   // re-ask the question
  if (resp.status === 401) return { ok: false, reason: "unauthorized" };
  if (resp.status === 503) return { ok: false, reason: "service_unavailable" };
  return { ok: true, ...(await resp.json()) };   // { status: "pending_confirmation" }
}
```

### CORS

Browser requests are restricted to whitelisted origins (`ALLOWED_ORIGINS`). If your
domain is not yet allowed, contact the service operator to add it.

### Rate Limiting

Per IP: subscribe `SUBSCRIBE_RATE_LIMIT` (60/min), challenge `CHALLENGE_RATE_LIMIT` (30/min), unsubscribe
`RATE_LIMIT` (5/min). When exceeded, the proxy returns HTTP `429 Too Many Requests`. Your frontend should handle this gracefully (e.g. show "Please try again in a minute").

### Error Handling

| Scenario | What you see | What to do |
|---|---|---|
| **Invalid email format** | HTTP 422 | Show validation error |
| **Rate limit exceeded** | HTTP 429 | Ask the user to wait and retry |
| **Sink unreachable (unsubscribe; ludo subscribe)** | HTTP 503 | Show "try again later" message |
| **Quiz failed** | HTTP 409 | Fetch a new challenge, ask again |
| **Network failure** | `fetch` throws | Show a generic error message |

All other outcomes (email found, not found, sink errors) are intentionally masked as `200` (`ok` / `pending_confirmation`). Do not try to infer the result from the response.

#### 422 Response Body

FastAPI returns a structured validation error on `422`:

```json
{"detail": [{"loc": ["body", "email"], "msg": "value is not a valid email address", "type": "value_error"}]}
```

---

## For Operators -- Deployment & Configuration

### Development

```bash
pip install -r requirements.txt

# Set env vars (see .env.example), then:
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Configure

Copy `.env.example` to `.env` and fill in every value: the `DEPLOY_*` vars, the
`REGISTRY_*` creds, Odoo / SMTP / LinkedIn, the Mautic creds (while ludo uses Mautic), `MAUXY_SITES`,
`CHALLENGE_SECRET` and `CONFIRM_TOKEN_SECRET` (`openssl rand -hex 32`). `.env` is gitignored — no secret belongs in the repo.

### Deploy with `scripts/deploy.py`

One tool drives the whole rollout from `.env` (no secrets in git):

```bash
python scripts/deploy.py --dry-run            # preview rendered manifests
python scripts/deploy.py --build              # docker login + build + push DEPLOY_IMAGE
python scripts/deploy.py --secret             # sync the mauxy-credentials Secret
python scripts/deploy.py --apply              # render to k8s/rendered/ + kubectl apply
python scripts/deploy.py --build --secret --apply   # full deploy in one shot
```

`--secret` writes `mauxy-credentials` (every runtime variable: Odoo, SMTP, LinkedIn, Mautic,
`ALLOWED_ORIGINS`, `MAUXY_SITES`, `CHALLENGE_SECRET`, …) into `$DEPLOY_NAMESPACE` (default `mauxy`,
its own namespace). The deployment is `envFrom` that Secret, so all runtime config
lives there. Ensure the namespace + `DEPLOY_IMAGE_PULL_SECRET` exist first, and
that DNS for `DEPLOY_DOMAIN` points at the ingress.

### Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL | *(required)* |
| `MAUTIC_USERNAME` | Mautic API basic-auth user | *(required)* |
| `MAUTIC_PASSWORD` | Mautic API basic-auth password | *(required)* |
| `MAUTIC_VERSION` | Target Mautic version (manual); seeds the `mautic_instance` row, shown in `/health/detail` | *(unknown if unset)* |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins | *(required)* |
| `RATE_LIMIT` | slowapi rate-limit for unsubscribe | `5/minute` |
| `SUBSCRIBE_RATE_LIMIT` / `CHALLENGE_RATE_LIMIT` | per-IP limits subscribe / challenge | `60/minute` / `30/minute` |
| `CONFIRM_RATE_LIMIT` | slowapi rate-limit for `/api/confirm` | `30/minute` |
| `ACTION_LOG_DB` | SQLite database path | `/data/actions.db` |
| `ADMIN_API_KEY` | Bearer token for `/api/actions`, `/api/admin/*`, lead admin | *(disabled if unset)* |
| `MIGRATE_STRICT` | Refuse to start when the DB is behind `db-patches/` | `true` |
| `MAUXY_SITES` | JSON array `{key, site, segment, topic, subscribe_quiz?, subscriber_sink?, doi_owner?}` — **seeds** an empty `sites` table | `[]` |
| `CHALLENGE_SECRET` / `CHALLENGE_TTL` | HMAC secret of the quiz tokens / lifetime (s) | *(quiz and newsletter (un)subscribe off if unset)* / `600` |
| `LEAD_SINK` / `SUBSCRIBER_SINK` | `odoo` / `none` (subscriber also `mautic`); `SUBSCRIBER_SINK` = fallback for sites without own sink | `odoo` |
| `ODOO_*`, `LEAD_*` | erp.wapsol + CTA leads, see "CTA leads" and `.env.example` | |
| `CONFIRM_TOKEN_SECRET` | HMAC secret signing the double opt-in token | *(required for subscribe)* |
| `CONFIRM_TOKEN_MAX_AGE` | Token lifetime in seconds | `86400` |
| `PUBLIC_BASE_URL` | Public URL of this mauxy (base of the DOI confirm link) | *(required for subscribe)* |
| `PUBLIC_WEBSITE_BASE_URL` | Public URL of the website; confirm redirects here | *(required for subscribe)* |
| `MAIL_MODE` | `smtp` / `console` (dry run) / `memory` (tests) | `smtp` if `SMTP_HOST` set, else `console` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | mailcow SMTP (587 STARTTLS or 465 SSL) | *(required for DOI mails)* |
| `SELFCHECK_*`, `MAUTIC_RADAR_*` | Self-check, see `.env.example` | see `.env.example` |
| `LINKEDIN_*`, `AUTH_*` | LinkedIn login, see "LinkedIn login" | login disabled until set |

### Runtime config (SQLite) & onboarding a site

The site registry, quiz bank and target Mautic version live in the SQLite DB and are
managed at runtime via admin CRUD (Bearer `ADMIN_API_KEY`) — no rebuild/redeploy.
Quiz content is seeded by a SQL patch (`db-patches/0002_seed_quiz.sql`); `MAUXY_SITES`
seeds the site keys (secrets) on first boot. The DB is authoritative afterwards. Each
site maps to its own quiz topic (`ludo`, `simplify-erp`, `re-cloud`; `odoo` is a generic
fallback).

To onboard a new site:

1. Author its question set (≈7 questions, easy for that site's audience, hard for a bot)
   and add them under a new topic:
   ```bash
   curl -X POST -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"topic":"newsite","id":"q1","q":"…?","choices":["A","B","C","D"],"answer":0}' \
     https://mauxy.engage.wapsol.de/api/admin/quiz
   ```
2. Register the site pointing at that topic:
   ```bash
   curl -X POST -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"key":"SECRET-KEY","site":"newsite","segment":"newsite","topic":"newsite","subscribe_quiz":true,"subscriber_sink":"odoo","doi_owner":"mauxy"}' \
     https://mauxy.engage.wapsol.de/api/admin/sites
   ```

Both are live on the next request (no restart). See `API.md` for the full admin surface.

> Site Bearer keys are stored in **plaintext** in SQLite (deliberate decision; TLS on the
> wire + RBAC on the PVC are the controls).

### Schema migrations (db-patches/)

The `actions.db` schema is a versioned artifact: each change ships as an integer-numbered
SQL patch in `db-patches/` (`NNNN_name.sql`), committed to git. Deployers apply pending
patches; the app only checks the version at startup and refuses to start if it's behind
(`MIGRATE_STRICT=false` downgrades that to a warning). In k8s an initContainer runs the
apply step before the app container, so first boot creates the schema automatically.

```bash
python scripts/sqlite_db.py --status          # current vs shipped schema version
python scripts/sqlite_db.py --migrate         # apply pending patches (deployer step)
python scripts/sqlite_db.py --generate NAME   # dev: auto-write the next patch from a schema diff
python scripts/sqlite_db.py --new NAME        # dev: scaffold an empty patch (content/DML)
python scripts/sqlite_db.py --pull            # copy the live PVC DB to a local data/actions.db
```

Shipped: 0001 init, 0002 quiz seed, 0003 website consent tables (DOI pending, self-check, leads,
outbox, login state/code), 0004 per-site policy. Tests with `ACTION_LOG_DB=:memory:` apply the patches
in-process. Never edit a deployed patch; add the next one.

Dev loop: change your local DB schema, run `--generate <name>`, review the emitted patch
(additive changes are auto; drops/retypes are flagged for manual editing), commit it. The
deployer applies it on the next rollout.

### Health & Monitoring

Three endpoints; `/health` and `/health/detail` always return HTTP 200.

| Endpoint | Purpose | Example response |
|---|---|---|
| `GET /health` | Kubernetes **liveness** probe (independent of every sink / Mautic / SMTP). | `{"status": "ok"}` |
| `GET /ready` | Kubernetes **readiness** probe: 200 when the local SQLite DB answers, else 503. Deliberately NOT tied to a sink (upstream tied it to Mautic): every endpoint degrades gracefully while erp.wapsol / Mautic is down. | `{"status": "ready"}` |
| `GET /health/detail` | Sink reachability (global + sinks pinned by sites), `ok` / `degraded`, login config, site count, quiz, target Mautic version, cache age (30 s). Always 200. | `{"status": "ok", "lead_sink": "odoo: reachable (18.0+e)", "subscriber_sink": "odoo: reachable (18.0+e)", "subscriber_sink_mautic": "mautic: reachable", "auth_linkedin": "configured", "sites": 2, "quiz": "configured", "mautic_version": "unknown", "cache_age_seconds": 3.1}` |

`degraded`: a sink is unreachable or misconfigured. Unsubscribe then returns `503`; confirmed
leads wait in the outbox and are retried (see "CTA leads").

---

## For Marketers -- Action Log

Every unsubscribe attempt is logged with a timestamp, the email address, the originating IP, and the outcome. You can query this log to audit compliance or debug issues.

### Querying the Action Log

The action log is available at `GET /api/actions` and requires a bearer token:

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://mauxy.engage.wapsol.de/api/actions"
```

Replace `YOUR_ADMIN_TOKEN` with the value of the `ADMIN_API_KEY` environment variable. If the key is not set, the endpoint returns `403 Forbidden`.

### Query Parameters

| Parameter | Type | Description |
|---|---|---|
| `email` | string | Filter by email address |
| `result` | string | Filter by outcome: `ok`, `not_found`, or `error` |
| `limit` | int | Number of records to return (1-500, default 50) |
| `offset` | int | Skip this many records (for pagination) |

### Example Queries

**All recent actions (last 50):**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://mauxy.engage.wapsol.de/api/actions"
```

**Actions for a specific email:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://mauxy.engage.wapsol.de/api/actions?email=user@example.com"
```

**Only successful unsubscribes:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://mauxy.engage.wapsol.de/api/actions?result=ok"
```

**Page 2 of results (records 51-100):**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://mauxy.engage.wapsol.de/api/actions?limit=50&offset=50"
```

### Understanding Results

Rows carry `action` (`subscribe` / `confirm` / `unsubscribe`) and `site`.

| Result | Meaning |
|---|---|
| `ok` | unsubscribe: opted out in the site's sink (Odoo subscription / Mautic DNC); ludo subscribe: added to the Mautic segment |
| `not_found` | unsubscribe: no subscription for that address. Nothing changed. |
| `subscribe_ok` / `subscribe_already_confirmed` / `subscribe_dnc_skip` / `subscribe_throttled` | DOI mail sent / no mail (already, blocked, > `MAX_PENDING_PER_EMAIL`) |
| `confirm_ok` / `confirm_invalid` / `confirm_expired` / `confirm_error` | DOI click outcome |
| `quiz_failed` / `honeypot` / `subscribe_honeypot` | gate rejected (no sink touched) |
| `sink_error` / `sink_unreachable` / `mautic_unreachable` / `error` | sink problem; `error_detail` has more |

---

## Newsletter Signup (Double Opt-In)

Applies to `doi_owner=mauxy` sites (simplify-erp). `doi_owner=sink` sites (ludo) keep the upstream
flow: contact into the Mautic segment at once, Mautic's campaign sends the opt-in; responses
`200 pending_confirmation` / `401` / `409` / `502` / `503` as before.

`POST /api/subscribe` (payload: `website/src/config/api.ts`) starts a double opt-in **without
writing to any sink** (the job only reads: already subscribed / blacklisted -> no mail): the request is kept in SQLite (`newsletter_pending`) and a confirmation link is
mailed via SMTP. Every valid request (new, already confirmed, DNC, throttled, Mautic down) gets the same immediate
`200 {"status": "pending_confirmation"}`; the contact-dependent work runs after the response as a job
(`jobs.py`), so neither content, status code nor timing reveal anything (T-SEC-017/018). `consent` must be `true`; the consent
timestamp is the server time of the click; `pageUrl` must be on an `ALLOWED_ORIGINS` origin.

`GET /api/confirm?token=...` checks signature + age and a matching unused pending record (SHA-256,
constant-time); then the subscriber sink subscribes (Odoo: `mailing.contact` / `mailing.subscription`
on `ODOO_LIST_NEWSLETTER_ID`; deprecated Mautic: tag `newsletter_confirmed`) and the token is burnt
(single use). Blacklisted / DNC -> burnt, error page; sink down -> token stays valid. 302 to
`${PUBLIC_WEBSITE_BASE_URL}/{locale}/newsletter/confirmed` (or `confirm-error?reason=...`).

Security review 2026-11-08 (enumeration, token replay, consent validation, pre-confirmation CRM
writes): fixed, tests T-SEC-001..018. Tokens issued by the earlier flow are refused (users re-subscribe).

### Website contract (simplify-erp.de, after this merge)

The site entry `simplify-erp` exists in `sites` (patch 0004: `subscribe_quiz=0`, `subscriber_sink=odoo`,
`doi_owner=mauxy`). Its key is a **public identifier** (it ships in the browser bundle, e.g.
`VITE_MAUXY_SITE_KEY`); the protection is the DOI + rate limits, not the key's secrecy.

- Subscribe: `POST /api/subscribe`, headers `Content-Type: application/json` and
  `Authorization: Bearer <simplify-erp site key>`, body unchanged
  `{"email", "locale": "de"|"en", "source": "[a-z0-9-]{1,40}", "consent": true, "pageUrl": "<page on an ALLOWED_ORIGINS origin>", "company_website": ""}`
  (`consentedAt` optional, ignored). No `challenge` / `answer` needed. Answers: `200 pending_confirmation`
  (always, for every address), `401` (key missing / wrong), `422`, `429`, `503`.
- Unsubscribe: quiz REQUIRED (no confirmation mail stands in for it). First `GET /api/challenge` with the
  same Bearer header -> `{challenge, question, choices}`; show question + choices; then `POST /api/unsubscribe`
  with the Bearer header and `{"email", "challenge", "answer": <0-based index>, "company_website": ""}`.
  Answers: `200 {"status":"ok"}` (always, whether subscribed or not), `401`, `409 quiz_failed` (fetch a new
  challenge), `422`, `429`, `503` (sink down). Newsletter mails from Odoo carry Odoo's own unsubscribe link
  (no quiz) -- the website form is the second way.
- CORS: the site origin in `ALLOWED_ORIGINS` (GET + POST, headers `Content-Type`, `Authorization`).
- Lead CTAs, Selbst-Check and LinkedIn login are unchanged (Origin allowlists, no site key).

### One-time Mautic setup (DEPRECATED, only for sites on the Mautic sink)

- Contact fields `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (date/time),
  `newsletter_page_url`; tags `newsletter_confirmed`, `migration_radar_confirmed`.
- SMTP credentials (mailcow) in the secret; Mautic e-mail templates are no longer used.

## Selbst-Check (simplify-erp.de, flywheel SPEC-020)

| Endpoint | Purpose |
|---|---|
| `POST /api/selfcheck` | `{email, lang, picks[mail,call,radar], answers, stay?, rules_version, consent: true, company_website}`. Origin must be in `SELFCHECK_ALLOWED_ORIGINS` (else 403). Answers validated against the vendored rules (422). One DOI mail listing the picks. Immediate uniform `200 pending_confirmation`; storage + DOI mail run as a job after the response. |
| `GET /api/selfcheck/confirm?token=` | Single-use. Result mail (pick `mail`), then the outbox: Migration Radar via the subscriber sink (pick `radar`, not blacklisted), `crm.lead` (picks `mail` / `call`; Radar alone = no lead), notification to `SELFCHECK_NOTIFY_TO` (always: quote request, call yes/no, answers, CRM lead id). 302 to `/{lang}/selbst-check?bestaetigt=1` / `=fehler`. Optional field `company` names the lead. |
| `POST /api/selfcheck/event` | `{event: started|completed|exited_early|submitted, lang}` -> 204. Stores day + event + lang only; global rate-limit key (IP never read), access-log lines dropped. |
| `GET /api/selfcheck/submissions` | Admin (Bearer `ADMIN_API_KEY`): confirmed (or pending) submissions. |
| `GET /api/selfcheck/stats` | Admin: counters per day (success metric "completed checks"). |

Rules: `vendor/selfcheck/` is a copy of flywheel `backend/libs/selfcheck.py` + `website/src/data/selfcheck.json`,
refreshed with `python scripts/sync_selfcheck.py --mauxy <this repo>` in flywheel. Retention: pending
7 days, confirmed 90 days (`SELFCHECK_*_DAYS`). Local dry run: `MAIL_MODE=console` prints mails instead of sending.

## CTA leads (flywheel SPEC-021)

`POST /api/lead` with `kind` = `contact` | `inquiry` | `webinar` | `checkout` | `selfcheck`
(selfcheck = alias of `POST /api/selfcheck`). Per-kind schema, extra fields refused, lengths
limited, no control characters; honeypot `company_website`; Origin must be in
`LEAD_ALLOWED_ORIGINS`; `newsletter: true` = the ticked newsletter box, confirmed by the same DOI.

| kind | required | optional |
|---|---|---|
| contact | email, lang, name, company, message (<= 4000) | phone, newsletter, page_url |
| inquiry | email, lang, subject_id, subject_title, subject_type (app/page/webinar/card/offer) | name, company, phone, message (<= 2000) |
| webinar | email, lang, name, company, webinar_id, webinar_title, date (ISO with zone, -1 d .. +2 y) | newsletter, page_url |
| checkout | email, lang, name, company, product_id, product_title, price_net, order_ref `ORD-...`, billing {street, postal_code, city, country ISO-2}, terms: true | phone, bundle, technology, services[<=20], vat_id, currency EUR |

Flow: immediate `200 {"status":"pending_confirmation"}`; storage + ONE DOI mail (server-side text
only) run as a job; throttle `MAX_PENDING_PER_EMAIL` / 24 h, silent. `GET /api/lead/confirm?token=`
(own salt, single use) burns the token and queues the delivery, 302 to
`/{lang}/anfrage/bestaetigt?art=<kind>` (error `/{lang}/anfrage/fehler`).

Delivery (`lead_sync.py`, table `lead_sync`, UNIQUE per submission = at most one lead):
subscriptions first, then the lead; each finished step is stored. `SinkTransient` -> retry with
backoff (`LEAD_SYNC_BACKOFF_SECONDS` x 2^n, max 1 h, `LEAD_SYNC_MAX_ATTEMPTS`), resumed after a
restart. A retry first looks for the lead of an earlier attempt (marker `Referenz: mauxy-<hex>` in
the description), so a lost response never yields a duplicate. Permanent failure / retries
exhausted -> mail with the whole submission to `LEAD_NOTIFY_TO`; after fixing the cause:
`POST /api/lead/sync/{id}/retry` (admin). Delivered rows keep only ids (`odoo_lead_id`, list
results); submissions are deleted after `LEAD_RETENTION_DAYS` (30), unconfirmed after 7 days.

### What is written to erp.wapsol (and nothing else)

- `crm.lead` create: `name` ("Website: Kontaktanfrage – Muster GmbH", "Website: Selbst-Check SAP ECC / R/3 – Muster GmbH", ...
  from submitted fields only), `type=lead`, `user_id=ODOO_LEAD_USER_ID`, `description` (plain text,
  HTML-escaped: submitted content, Selbst-Check answers + result text from the shared rules engine,
  what was requested, UTM line, marker), `source_id` / `medium_id` / `campaign_id` (existing UTM records
  by name: `ODOO_UTM_SOURCE`, `ODOO_UTM_MEDIUM`, campaign = kind; missing -> left empty, warning),
  optional `team_id`, `tag_ids`. Exactly one active `res.partner` with that e-mail -> `partner_id`
  (+ `contact_name`); then NO `email_from` / `phone` (crm.lead's inverse would write them onto the
  partner). Otherwise `contact_name`, `partner_name`, `email_from`, `phone`.
- `crm.lead.message_subscribe` (Ashant's partner) only if Odoo did not already auto-follow him.
- `mailing.contact` create (name, email, company_name + one subscription line) or
  `mailing.subscription` create (existing contact joins the list; the contact is not changed);
  `mailing.subscription.write({'opt_out': ...})`: opt-out on unsubscribe, re-subscribe after a new
  DOI. Blacklisted addresses (`mail.blacklist`) are never (re)subscribed.
- Never: write / unlink / merge on `res.partner` or existing leads, UTM create. Enforced twice:
  `odoo_client.ALLOWED` / `WRITE_FIELDS` (client) and the bot's Odoo group (server).

### erp.wapsol setup (owner, one-time)

1. Group `Website Bot (mauxy)` (Settings > Technical > Groups), no implied groups. Access rights:
   `res.partner` read; `res.users` read (default); `crm.lead` read + create + write; `utm.source`,
   `utm.medium`, `utm.campaign` read; `mail.blacklist` read; `mailing.list` read;
   `mailing.contact` read + create; `mailing.subscription` read + create + write. No unlink anywhere.
2. Record rules for the group: `crm.lead` read + write `[('create_uid', '=', user.id)]` (only its own
   leads; write is needed by Odoo's message_subscribe, mauxy never calls write on leads);
   `mailing.subscription` read/create/write `[('list_id', 'in', [<newsletter id>, <radar id>])]`;
   `mailing.contact` read/create `[('list_ids', 'in', [<ids>])]` (or no rule if contacts of other
   lists must be found to avoid duplicates -- then read only).
3. User `website-bot` (internal user, NOT portal, no other groups; e-mail notifications off), group
   from 1, API key (Preferences > Account Security > New API key, name "mauxy"). If Odoo requires
   a sales group for crm.lead creation, add "Sales / User: Own Documents Only" and keep rule 2.
4. Email Marketing: lists "Newsletter" and "Migration Radar" -> ids into `ODOO_LIST_*_ID`.
   Unsubscribe link in mailings = Odoo's own (opt-out lands on the same `mailing.subscription`).
5. UTM: source `simplify-erp.de`, medium `Website` (exists by default), campaigns `selfcheck`,
   `contact`, `inquiry`, `webinar`, `checkout`.
6. Ashant's `res.users` id -> `ODOO_LEAD_USER_ID`; optional CRM team -> `ODOO_LEAD_TEAM_ID`.
7. Secret `mauxy-credentials` (namespace of mauxy, see `k8s/secret.yaml.example`): `ODOO_URL`, `ODOO_DB`,
   `ODOO_USER`, `ODOO_API_KEY`, `ODOO_LEAD_USER_ID`, `ODOO_LIST_NEWSLETTER_ID`, `ODOO_LIST_RADAR_ID`,
   `LEAD_ALLOWED_ORIGINS`, `LEAD_NOTIFY_TO` (+ existing SMTP / token / origin keys). Never in git.
8. Check, read only: `python scripts/odoo_smoke.py --env-file <file with the bot credentials>`
   (version, bot login + groups, Ashant, teams, UTM, lists, needed rights True, forbidden rights False).

Deploy order: mauxy first (new endpoints, Odoo sinks), then the website.

### Local run (fake Odoo, mails on the console)

`PYTHONPATH=. tests/venv/bin/python tests/scripts/run_local.py` -> `http://10.0.99.1:8020`,
`GET /__fake_odoo` shows what was written. Refuses any `ODOO_URL` other than the fake.

### Security notes

- MITM: Odoo and SMTP only over TLS with certificate verification (`odoo_client` refuses http);
  the browser reaches mauxy over HTTPS (ingress). Risk low while TLS is verified; a compromised
  CA / proxy with a trusted cert could read the API key and lead data -> keep the bot's rights
  minimal (rules above) so a stolen key can only create leads / list entries, never read sales,
  invoices or change partners. Rotate the key on suspicion (Odoo API keys page).
- Personal data: pending submissions in mauxy SQLite (7 d), confirmed ones 30 d (then only in
  Odoo, the system of record; deletion requests there). No IP / user agent stored for leads.
- Scale: one replica, deployment strategy `Recreate` (SQLite on ONE RWO Longhorn volume,
  in-process outbox; a rolling surge pod could not mount the volume). More replicas need a shared DB
  and a leader for the outbox.

## LinkedIn login (website visitors, sign-in only)

Replaces app2li (retired 2026-11-08). OpenID Connect scopes `openid profile email` only.
LinkedIn **posting** (as Ashant or the company page) is NOT part of mauxy and belongs to the internal
flywheel side: no publishing scopes, no member tokens in mauxy.

```
site login()  --GET /api/auth/linkedin/start?lang&return&binding-->  mauxy
   mauxy: return path checked (same-site path), state row (hashes only), signed state (10 min),
          Set-Cookie __Host-mauxy_li (HttpOnly, Secure, SameSite=Lax, 10 min)  --302-->  LinkedIn
LinkedIn  --302 /api/auth/linkedin/callback?code&state-->  mauxy
   mauxy: state signature + age, single use (burnt first), cookie == state, code -> token ->
          userinfo over verified TLS (token dropped), profile parked 60 s under a one-time code
          --302-->  {site}/{lang}/auth/callback?code=<one-time>   (errors: ?error=<code>)
site  --POST /api/auth/session {code, binding}  (Origin = that site)-->  mauxy
   mauxy: single use, 60 s, binding == the one sent at start -> {profile, session (1 h), return}
```

- **Return target**: only a path `/de/...` or `/en/...` on an allowed site (`AUTH_ALLOWED_ORIGINS`;
  multi-site: the Referer picks among the allowlisted origins, else the first). Full URLs, `//`,
  backslashes, encoded slashes, dot segments, `user@host` and the callback itself: 400.
- **Login CSRF**: the state is bound to the browser by the cookie; the one-time code to the tab by
  the `binding` (sessionStorage on the site). A code injected via a link or read from a log is useless.
- **No PII in URLs**: the site only ever sees `code` or `error`; `error_description` is never
  reflected. Profile = `sub`, `name`, `given_name`, `family_name`, `email` (only when LinkedIn says `email_verified` is exactly true; also a signed session claim),
  `email_verified`. No picture, no company (LinkedIn OIDC has none).
- **Stored**: `auth_state` (hashes, 10 min), `auth_code` (profile, max 60 s, deleted on exchange,
  purged every 30 s), one audit line per login in `action_log` (result + site origin; no name,
  e-mail, IP or token).
- **Session token**: itsdangerous, own salt, `AUTH_SESSION_TTL` (1 h), `aud` = site origin.
  `auth_api.verify_session()` verifies it. The site keeps it in sessionStorage only.
- **Config guard**: missing / unsafe settings disable only the login (`error=unavailable`,
  `/health/detail` -> `auth_linkedin: disabled: <names>`); newsletter and leads keep running.
- **Host**: start and callback must be the same host (the cookie). Prod: the host this code runs on,
  `unsubscribe.engage.wapsol.de` (see "Public host" below).

| Variable | Purpose |
|---|---|
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | LinkedIn app of simplify-erp.de (k8s secret only) |
| `LINKEDIN_REDIRECT_URI` | default `PUBLIC_BASE_URL` + `/api/auth/linkedin/callback`; must equal the LinkedIn portal entry exactly |
| `AUTH_ALLOWED_ORIGINS` | sites the login returns to (default `LEAD_ALLOWED_ORIGINS`); https only (http only on 10.0.99.1) |
| `AUTH_TOKEN_SECRET` | >= 32 chars; signs state + session (default `CONFIRM_TOKEN_SECRET`, own salts) |
| `AUTH_SESSION_TTL`, `AUTH_RATE_LIMIT`, `AUTH_SESSION_RATE_LIMIT` | 3600 s, 20/minute (start + callback), 30/minute (session) |
| `LINKEDIN_AUTH_URL`, `LINKEDIN_TOKEN_URL`, `LINKEDIN_USERINFO_URL` | LinkedIn defaults; override only for a local mock on 10.0.99.1 |

LinkedIn developer portal (app of simplify-erp.de, Auth tab): product "Sign In with LinkedIn using
OpenID Connect"; Authorized redirect URL exactly `https://unsubscribe.engage.wapsol.de/api/auth/linkedin/callback`.

**Public host**: this code (repo mautic-unsubscribe) runs as `simplify-web/mautic-unsubscribe-proxy`
behind `unsubscribe.engage.wapsol.de` (`PUBLIC_BASE_URL`, the site's `VITE_MAUXY_URL`).
`mauxy.engage.wapsol.de` (namespace `mauxy`) is a DIFFERENT service (repo voltAIc-apps/mauxy,
multi-site Mautic proxy): do not point the LinkedIn redirect there. Merging the two is an open CRIE item.

Local: `tests/scripts/run_local.py` mounts a mocked LinkedIn at `/__mock_linkedin` (fake person);
tests `tests/test_auth.py` (T-AUTH-0xx, respx).

## Roadmap

Planned features (see [issue #8](https://github.com/voltAIc-apps/mauxy/issues/8)):

- Per-site unsubscribe (segment removal instead of global DNC)
- Single-use challenge tokens (today: short expiry + rate-limit + honeypot; topic-bound since the merge)
- Per-site Odoo lists (today: one `ODOO_LIST_NEWSLETTER_ID` for every site on the Odoo sink)

Done: `POST /api/subscribe` (segment add) · per-site key registry · centralized
bot-defence quiz (`GET /api/challenge`, per-site topics) · SQLite-backed runtime config
(sites + quiz + Mautic version) with admin CRUD (`/api/admin/*`) — no redeploy to onboard
a site or edit a quiz · per-site policy (quiz on subscribe, subscriber sink, DOI owner) · mauxy-owned
double opt-in for `doi_owner=mauxy` sites (Mautic campaign only for `doi_owner=sink`).

---

## License

MIT -- see [LICENSE](LICENSE).
