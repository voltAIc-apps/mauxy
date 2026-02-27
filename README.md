# Mauxy

Privacy-safe email unsubscribe proxy for Mautic. Accepts unsubscribe requests from your website frontend and adds contacts to Mautic's Do-Not-Contact (DNC) list -- without exposing any Mautic credentials to the browser.

**Key design property:** Every request returns `{"status": "ok"}` regardless of whether the email exists, was already unsubscribed, or caused an error -- this prevents email enumeration attacks. Returns `503` when Mautic is unreachable so the frontend can prompt the user to retry.

---

## For Developers -- Integrating the Unsubscribe Endpoint

### Endpoint

```
POST https://unsubscribe.example.com/api/unsubscribe
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
      "https://unsubscribe.example.com/api/unsubscribe",
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

Requests are only accepted from whitelisted origins configured via the `ALLOWED_ORIGINS` environment variable. Update this variable and restart the deployment to add new origins.

### Rate Limiting

The endpoint is rate-limited to **5 requests per minute per IP address**. When exceeded, the proxy returns HTTP `429 Too Many Requests`. Your frontend should handle this gracefully (e.g. show "Please try again in a minute").

### Error Handling

You only need to handle two failure cases in your frontend:

| Scenario | What you see | What to do |
|---|---|---|
| **Invalid email format** | HTTP 422 | Show validation error |
| **Rate limit exceeded** | HTTP 429 | Ask the user to wait and retry |
| **Mautic unreachable** | HTTP 503 | Show "try again later" message |
| **Network failure** | `fetch` throws | Show a generic error message |

All other outcomes (email found, not found, Mautic errors) are intentionally masked as `200 {"status": "ok"}`. Do not try to infer the result from the response.

---

## For Marketers -- Action Log

Every unsubscribe attempt is logged with a timestamp, the email address, the originating IP, and the outcome. You can query this log to audit compliance or debug issues.

### Querying the Action Log

The action log is available at `GET /api/actions` and requires a bearer token:

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://unsubscribe.example.com/api/actions"
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
  "https://unsubscribe.example.com/api/actions"
```

**Actions for a specific email:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://unsubscribe.example.com/api/actions?email=user@example.com"
```

**Only successful unsubscribes:**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://unsubscribe.example.com/api/actions?result=ok"
```

**Page 2 of results (records 51-100):**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://unsubscribe.example.com/api/actions?limit=50&offset=50"
```

### Understanding Results

| Result | Meaning |
|---|---|
| `ok` | Contact was found in Mautic and added to the Do-Not-Contact list. They will no longer receive marketing emails. |
| `not_found` | No contact with that email exists in Mautic. Nothing was changed. This is normal for typos or people who were never subscribed. |
| `error` | Something went wrong when talking to Mautic (e.g. timeout, API error). The `error_detail` field in the log entry has more information. |

---

## Health & Monitoring

Two health endpoints are available. Both always return HTTP 200.

| Endpoint | Purpose | Example response |
|---|---|---|
| `GET /health` | Kubernetes liveness/readiness probe. Also useful for a quick manual check. | `{"status": "ok", "mautic": "reachable"}` |
| `GET /health/detail` | Richer status with `ok` / `degraded` indicator and cache age. | `{"status": "ok", "mautic": "reachable", "cache_age_seconds": 12.3}` |

If `status` is `degraded`, the proxy cannot reach Mautic. Unsubscribe requests will return `503 {"status": "service_unavailable"}` and the attempt is logged as `mautic_unreachable`.

---

## Development

```bash
pip install -r requirements.txt

# Set env vars (see .env.example), then:
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Deployment

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

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `MAUTIC_BASE_URL` | Mautic instance URL | *(required)* |
| `MAUTIC_USERNAME` | Mautic API basic-auth user | *(required)* |
| `MAUTIC_PASSWORD` | Mautic API basic-auth password | *(required)* |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins | *(required)* |
| `RATE_LIMIT` | slowapi rate-limit string | `5/minute` |
| `ACTION_LOG_DB` | SQLite database path | `/data/actions.db` |
| `ADMIN_API_KEY` | Bearer token for `/api/actions` | *(disabled if unset)* |

## License

MIT -- see [LICENSE](LICENSE).
