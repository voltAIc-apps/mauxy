# Mauxy

Privacy-safe email unsubscribe proxy for Mautic. Accepts unsubscribe requests from website frontends and adds contacts to the Do-Not-Contact (DNC) list -- without exposing Mautic credentials to the browser.

**Key design property:** Every contact-specific request returns `{"status": "ok"}` regardless of whether the email exists, was already unsubscribed, or caused an error -- this prevents email enumeration attacks. Returns `503` when Mautic is unreachable so the frontend can prompt the user to retry.

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
| `GET /health` | Kubernetes liveness/readiness probe. Also useful for a quick manual check. | `{"status": "ok", "mautic": "reachable"}` |
| `GET /health/detail` | Richer status with `ok` / `degraded` indicator and cache age. | `{"status": "ok", "mautic": "reachable", "cache_age_seconds": 12.3}` |

If `status` is `degraded`, the proxy cannot reach Mautic. Unsubscribe requests will return `503 {"status": "service_unavailable"}` and the attempt is logged as `mautic_unreachable`.

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
touching Mautic**: the request is kept in SQLite (`newsletter_pending`) and a confirmation link is
mailed via SMTP. Every contact-specific outcome (new, already confirmed, DNC, throttled) answers
`200 {"status": "pending_confirmation"}` -- no enumeration. `consent` must be `true`; the consent
timestamp is the server time of the click; `pageUrl` must be on an `ALLOWED_ORIGINS` origin.

`GET /api/confirm?token=...` checks signature + age, a matching unused pending record (SHA-256,
constant-time) and that the address is not DNC; only then the contact is created/updated in
Mautic (tag `newsletter_confirmed`) and the token is burnt (single use). 302 to
`${PUBLIC_WEBSITE_BASE_URL}/{locale}/newsletter/confirmed` (or `confirm-error?reason=...`).

Security review 2026-11-08 (enumeration, token replay, consent validation, pre-confirmation CRM
writes): fixed, tests T-SEC-001..016. Tokens issued by the earlier flow are refused (users re-subscribe).

### One-time Mautic setup

- Contact fields `newsletter_locale`, `newsletter_source`, `newsletter_consented_at` (date/time),
  `newsletter_page_url`; tags `newsletter_confirmed`, `migration_radar_confirmed`.
- SMTP credentials (mailcow) in the secret; Mautic e-mail templates are no longer used.

## Selbst-Check (simplify-erp.de, flywheel SPEC-020)

| Endpoint | Purpose |
|---|---|
| `POST /api/selfcheck` | `{email, lang, picks[mail,call,radar], answers, stay?, rules_version, consent: true, company_website}`. Origin must be in `SELFCHECK_ALLOWED_ORIGINS` (else 403). Answers validated against the vendored rules (422). One DOI mail listing the picks. Uniform `200 pending_confirmation`. |
| `GET /api/selfcheck/confirm?token=` | Single-use. Result mail (pick `mail`), Migration Radar in Mautic (pick `radar`, not DNC), notification to `SELFCHECK_NOTIFY_TO` (always: indicative-quote request, call yes/no, answers). 302 to `/{lang}/selbst-check?bestaetigt=1` / `=fehler`. |
| `POST /api/selfcheck/event` | `{event: started|completed|exited_early|submitted, lang}` -> 204. Stores day + event + lang only; global rate-limit key (IP never read), access-log lines dropped. |
| `GET /api/selfcheck/submissions` | Admin (Bearer `ADMIN_API_KEY`): confirmed (or pending) submissions. |
| `GET /api/selfcheck/stats` | Admin: counters per day (success metric "completed checks"). |

Rules: `vendor/selfcheck/` is a copy of flywheel `backend/libs/selfcheck.py` + `website/src/data/selfcheck.json`,
refreshed with `python scripts/sync_selfcheck.py --mauxy <this repo>` in flywheel. Retention: pending
7 days, confirmed 90 days (`SELFCHECK_*_DAYS`). Local dry run: `MAIL_MODE=console` prints mails instead of sending.

## Roadmap

Planned features (see [issue #8](https://github.com/voltAIc-apps/mauxy/issues/8)):

- Per-site segment mapping via `sites.yaml`
- Per-site unsubscribe (segment removal instead of global DNC)

---

## License

MIT -- see [LICENSE](LICENSE).
