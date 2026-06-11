# Mauxy

The single newsletter (un)subscribe authority in front of Mautic -- Mautic
credentials never reach the browser. Double-opt-in + sending live in Mautic.

- **Subscribe** (`POST /api/subscribe`): find/create the contact, add it to the
  calling site's Mautic segment. Mautic sends the opt-in mail → `pending_confirmation`.
- **Unsubscribe** (`POST /api/unsubscribe`): add the contact to the Do-Not-Contact (DNC) list.
- **Challenge** (`GET /api/challenge`): a bot-defence question both flows must answer.

Two guards in front of every (un)subscribe:

1. **Per-site key** — `Authorization: Bearer <site-key>`. Each calling site is
   configured in `MAUXY_SITES` with its own key, `segment` and quiz `topic`. Sent
   over HTTPS (TLS = the MITM defence). The segment is chosen by the key, not the body.
2. **Bot-defence quiz** — fetch a question from `GET /api/challenge`, send back the
   `challenge` token + correct `answer` index. Wrong/expired → `409`. A honeypot field
   (`company_website`) silently drops bots.

**Enumeration-safe:** unsubscribe returns `{"status":"ok"}` whether or not the
contact exists; key/quiz failures are email-independent (`401`/`409`).

---

## For Developers -- Integrating

See [API Reference](API.md) for full endpoint documentation. The flow is the same
for subscribe and unsubscribe: **challenge → answer**.

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

### Configure

Copy `.env.example` to `.env` and fill in every value: the `DEPLOY_*` vars, the
`REGISTRY_*` creds, the Mautic creds, `MAUXY_SITES`, and `CHALLENGE_SECRET`
(`openssl rand -hex 32`). `.env` is gitignored — no secret belongs in the repo.

### Deploy with `scripts/deploy.py`

One tool drives the whole rollout from `.env` (no secrets in git):

```bash
python scripts/deploy.py --dry-run            # preview rendered manifests
python scripts/deploy.py --build              # docker login + build + push DEPLOY_IMAGE
python scripts/deploy.py --secret             # sync the mauxy-credentials Secret
python scripts/deploy.py --apply              # render to k8s/rendered/ + kubectl apply
python scripts/deploy.py --build --secret --apply   # full deploy in one shot
```

`--secret` writes `mauxy-credentials` (Mautic creds, `ALLOWED_ORIGINS`,
`MAUXY_SITES`, `CHALLENGE_SECRET`, …) into `$DEPLOY_NAMESPACE` (default `mauxy`,
its own namespace). The deployment is `envFrom` that Secret, so all runtime config
lives there. Ensure the namespace + `DEPLOY_IMAGE_PULL_SECRET` exist first, and
that DNS for `DEPLOY_DOMAIN` points at the ingress.

### Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL | *(required)* |
| `MAUTIC_USERNAME` | Mautic API basic-auth user | *(required)* |
| `MAUTIC_PASSWORD` | Mautic API basic-auth password | *(required)* |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins | *(required)* |
| `MAUXY_SITES` | JSON array of `{key, site, segment, topic}` — the per-site key registry | `[]` (all calls 401) |
| `CHALLENGE_SECRET` | HMAC secret for signing bot-defence challenge tokens | *(quiz disabled if unset)* |
| `CHALLENGE_TTL` | Challenge token lifetime, seconds | `600` |
| `RATE_LIMIT` | slowapi rate-limit string (unsubscribe) | `5/minute` |
| `ACTION_LOG_DB` | SQLite database path | `/data/actions.db` |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` | *(disabled if unset)* |

Quiz questions live in `quiz_bank.json`, keyed by topic (e.g. `odoo`). Add a topic
there and point a site at it via `MAUXY_SITES`.

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

| Result | Meaning |
|---|---|
| `ok` | Contact was found in Mautic and added to the Do-Not-Contact list. They will no longer receive marketing emails. |
| `not_found` | No contact with that email exists in Mautic. Nothing was changed. This is normal for typos or people who were never subscribed. |
| `error` | Something went wrong when talking to Mautic (e.g. timeout, API error). The `error_detail` field in the log entry has more information. |

---

## Roadmap

Planned features (see [issue #8](https://github.com/voltAIc-apps/mauxy/issues/8)):

- Per-site unsubscribe (segment removal instead of global DNC)
- Single-use challenge tokens (today: short expiry + rate-limit + honeypot)

Done: `POST /api/subscribe` (segment add) · per-site key registry (`MAUXY_SITES`) ·
centralized bot-defence quiz (`GET /api/challenge`, per-topic). GDPR double-opt-in is
owned by Mautic (campaign on the segment).

---

## License

MIT -- see [LICENSE](LICENSE).
