# Mauxy

The website's interface to other systems (owner decision 2026-11-08, flywheel SPEC-021): public
consent service of simplify-erp.de for newsletter, Migration Radar, Selbst-Check and every lead CTA
(contact form, app inquiry, webinar registration, shop checkout). Double opt-in for everything;
confirmed data goes to **sinks**: `crm.lead` and Email Marketing lists in erp.wapsol (Odoo 18,
default). Mautic is a deprecated sink (`SUBSCRIBER_SINK=mautic`) until it is shut down.
Credentials stay server-side.

**Key design property:** every contact-specific request gets the same answer (`pending_confirmation`
/ `ok`), immediately, whatever the address -- no enumeration through content, status or timing.
Nothing is written to any sink before the confirmation link is clicked.

| Area | Endpoints | Code |
|---|---|---|
| Newsletter | `POST /api/subscribe`, `GET /api/confirm`, `POST /api/unsubscribe` | `main.py` |
| Selbst-Check | `/api/selfcheck*` | `selfcheck_api.py` |
| CTA leads | `POST /api/lead`, `GET /api/lead/confirm`, admin `/api/lead/submissions`, `/api/lead/sync/{id}/retry` | `leads_api.py` |
| Delivery after DOI | outbox with retries | `lead_sync.py` |
| Sinks | Odoo lead + subscriber sink, deprecated Mautic sink | `sinks.py`, `mautic_sink.py` |
| Odoo | the one Odoo client (JSON-RPC, allowlist) | `odoo_client.py` |

---

## For Developers -- Integrating the Unsubscribe Endpoint

See [API Reference](API.md) for full endpoint documentation.

### Endpoint

```
POST https://newsletter.example.com/api/unsubscribe
Content-Type: application/json
```

### Request / Response

Send a JSON body with the email address. The response is always the same:

```json
// Request
{"email": "user@example.com"}

// Response (200 on success, 422/429/503 on error)
{"status": "ok"}
```

### JavaScript Example

Drop this into your unsubscribe page or form handler:

```javascript
async function unsubscribe(email) {
  try {
    const resp = await fetch(
      "https://newsletter.example.com/api/unsubscribe",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      }
    );

    if (resp.status === 422) {
      return { success: false, reason: "invalid_email" };
    }

    if (resp.status === 429) {
      return { success: false, reason: "rate_limited" };
    }

    if (resp.status === 503) {
      return { success: false, reason: "service_unavailable" };
    }

    return { success: true };
  } catch (err) {
    // Network error -- the proxy is unreachable
    return { success: false, reason: "network_error" };
  }
}
```

### CORS

Requests are restricted to whitelisted origins. If your domain is not yet allowed, contact the service operator to add it.

### Rate Limiting

The endpoint is rate-limited to **5 requests per minute per IP address**. When exceeded, the proxy returns HTTP `429 Too Many Requests`. Your frontend should handle this gracefully (e.g. show "Please try again in a minute").

### Error Handling

| Scenario | What you see | What to do |
|---|---|---|
| **Invalid email format** | HTTP 422 | Show validation error |
| **Rate limit exceeded** | HTTP 429 | Ask the user to wait and retry |
| **Mautic unreachable** | HTTP 503 | Show "try again later" message |
| **Network failure** | `fetch` throws | Show a generic error message |

All other outcomes (email found, not found, Mautic errors) are intentionally masked as `200 {"status": "ok"}`. Do not try to infer the result from the response.

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

### Build

```bash
docker build -t your-registry/mauxy:latest .
docker push your-registry/mauxy:latest
```

### Configure

Copy `.env.example` to `.env` and fill in all values, including the `DEPLOY_*` variables for k8s manifests.

### Render & Apply k8s Manifests

The k8s manifests in `k8s/` contain `${VAR}` placeholders. Use `scripts/deploy.py` to render them:

```bash
# Preview rendered manifests
python scripts/deploy.py --dry-run

# Render to k8s/rendered/
python scripts/deploy.py

# Render and apply in one step
python scripts/deploy.py --apply
```

Create the credentials secret separately (values are not in .env):

```bash
kubectl create secret generic mauxy-credentials \
  --from-literal=MAUTIC_BASE_URL=https://mautic.example.com \
  --from-literal=MAUTIC_USERNAME=<user> \
  --from-literal=MAUTIC_PASSWORD=<pass> \
  --from-literal=ADMIN_API_KEY=<key> \
  -n <your-namespace>
```

### Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL | *(required)* |
| `MAUTIC_USERNAME` | Mautic API basic-auth user | *(required)* |
| `MAUTIC_PASSWORD` | Mautic API basic-auth password | *(required)* |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins | *(required)* |
| `RATE_LIMIT` | slowapi rate-limit for subscribe/unsubscribe | `5/minute` |
| `CONFIRM_RATE_LIMIT` | slowapi rate-limit for `/api/confirm` | `30/minute` |
| `ACTION_LOG_DB` | SQLite database path | `/data/actions.db` |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` | *(disabled if unset)* |
| `CONFIRM_TOKEN_SECRET` | HMAC secret signing the double opt-in token | *(required for subscribe)* |
| `CONFIRM_TOKEN_MAX_AGE` | Token lifetime in seconds | `86400` |
| `PUBLIC_BASE_URL` | Public URL of this mauxy (used inside the Mautic email link) | *(required for subscribe)* |
| `PUBLIC_WEBSITE_BASE_URL` | Public URL of the website; confirm redirects here | *(required for subscribe)* |
| `MAIL_MODE` | `smtp` / `console` (dry run) / `memory` (tests) | `smtp` if `SMTP_HOST` set, else `console` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | mailcow SMTP (587 STARTTLS or 465 SSL) | *(required for DOI mails)* |
| `SELFCHECK_*`, `MAUTIC_RADAR_*` | Self-check, see `.env.example` | see `.env.example` |

### Health & Monitoring

Two health endpoints are available. Both always return HTTP 200.

| Endpoint | Purpose | Example response |
|---|---|---|
| `GET /health` | Kubernetes liveness/readiness probe (cheap, no sink call). | `{"status": "ok"}` |
| `GET /health/detail` | Sink reachability, `ok` / `degraded`, cache age (30 s). | `{"status": "ok", "lead_sink": "odoo: reachable (18.0+e)", "subscriber_sink": "odoo: reachable (18.0+e)", "cache_age_seconds": 3.1}` |

`degraded`: a sink is unreachable or misconfigured. Unsubscribe then returns `503`; confirmed
leads wait in the outbox and are retried (see "CTA leads").

---

## For Marketers -- Action Log

Every unsubscribe attempt is logged with a timestamp, the email address, the originating IP, and the outcome. You can query this log to audit compliance or debug issues.

### Querying the Action Log

The action log is available at `GET /api/actions` and requires a bearer token:

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://newsletter.example.com/api/actions"
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
  "https://newsletter.example.com/api/actions"
```

**Actions for a specific email:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://newsletter.example.com/api/actions?email=user@example.com"
```

**Only successful unsubscribes:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://newsletter.example.com/api/actions?result=ok"
```

**Page 2 of results (records 51-100):**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://newsletter.example.com/api/actions?limit=50&offset=50"
```

### Understanding Results

| Result | Meaning |
|---|---|
| `ok` | Contact was found in Mautic and added to the Do-Not-Contact list. They will no longer receive marketing emails. |
| `not_found` | No contact with that email exists in Mautic. Nothing was changed. This is normal for typos or people who were never subscribed. |
| `error` | Something went wrong when talking to Mautic (e.g. timeout, API error). The `error_detail` field in the log entry has more information. |

---

## Newsletter Signup (Double Opt-In)

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

### One-time Mautic setup (DEPRECATED, only with `SUBSCRIBER_SINK=mautic`)

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
7. Secret `mauxy-credentials` (namespace of mauxy, see `k8s/secret.yaml`): `ODOO_URL`, `ODOO_DB`,
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
- Scale: one replica (SQLite on an RWO volume, in-process outbox). More replicas need a shared DB
  and a leader for the outbox.

## Roadmap

Planned features (see [issue #8](https://github.com/voltAIc-apps/mauxy/issues/8)):

- Per-site segment mapping via `sites.yaml`
- Per-site unsubscribe (segment removal instead of global DNC)

---

## License

MIT -- see [LICENSE](LICENSE).
