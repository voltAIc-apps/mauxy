# API Reference

Base URL: `https://newsletter.example.com` (set via `DEPLOY_DOMAIN`)

---

## POST /api/unsubscribe

Add an email address to the Mautic Do-Not-Contact (DNC) list.

**CORS:** Restricted to configured origins (`ALLOWED_ORIGINS` env var).
**Rate limit:** 5 requests per minute per IP (configurable). Exceeding the limit returns HTTP 429.

### Request

| Header         | Value              |
|----------------|--------------------|
| Content-Type   | application/json   |

```json
{
  "email": "user@example.com"
}
```

`email` must be a valid email address.

### Response

Returns HTTP 200 with the same body regardless of whether the contact was found, already unsubscribed, or doesn't exist -- this prevents email enumeration. Returns HTTP 503 when Mautic is unreachable.

```json
{
  "status": "ok"
}
```

### Response (503 -- Mautic unreachable)

Returned when the proxy cannot reach the Mautic API (connection error or non-200 from Mautic search endpoint). The request was not processed -- the frontend should prompt the user to retry.

```json
{
  "status": "service_unavailable"
}
```

### Examples

**curl**

```bash
curl -X POST https://newsletter.example.com/api/unsubscribe \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com"}'
```

**fetch (browser)**

```js
await fetch("https://newsletter.example.com/api/unsubscribe", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email: "user@example.com" }),
});
```

---

## GET /api/actions

Query the action log. Admin-only endpoint protected by a Bearer token.

Returns HTTP 403 if `ADMIN_API_KEY` is not configured on the server.
Returns HTTP 401 if the token is missing or incorrect.

### Request

| Header          | Value                      |
|-----------------|----------------------------|
| Authorization   | Bearer `<ADMIN_API_KEY>`   |

### Query parameters

| Param    | Type   | Default | Description                           |
|----------|--------|---------|---------------------------------------|
| `email`  | string | --      | Filter by email address               |
| `result` | string | --      | Filter by result (`ok`, `not_found`, `error`) |
| `limit`  | int    | 50      | Rows to return (1-500)                |
| `offset` | int    | 0       | Pagination offset                     |

### Response (200)

```json
{
  "actions": [
    {
      "id": 1,
      "ts": "2026-02-26T12:00:00+00:00",
      "email": "user@example.com",
      "source_origin": "https://your-app.example.com",
      "source_ip": "203.0.113.1",
      "result": "ok",
      "contact_id": "42",
      "error_detail": null
    }
  ],
  "count": 1
}
```

### Example

```bash
curl https://newsletter.example.com/api/actions?limit=10 \
  -H "Authorization: Bearer YOUR_ADMIN_KEY"
```

---

## GET /health

Kubernetes liveness/readiness probe. Always returns HTTP 200.

### Response

```json
{
  "status": "ok",
  "mautic": "reachable"
}
```

`mautic` may also be `"pending"`, `"HTTP <code>"`, or `"connection error: ..."` when Mautic is unreachable.

---

## GET /health/detail

Detailed health status for operator debugging. Always returns HTTP 200.

### Response

```json
{
  "status": "ok",
  "mautic": "reachable",
  "cache_age_seconds": 12.3
}
```

`status` is `"ok"` when Mautic is reachable, `"degraded"` otherwise. The Mautic connectivity result is cached for 30 seconds; `cache_age_seconds` shows how stale the check is.

---

## Error responses

| Status | Cause |
|--------|-------|
| 401    | Missing or invalid Bearer token on `/api/actions` |
| 403    | `ADMIN_API_KEY` not configured (admin endpoint disabled) |
| 422    | Invalid request body (e.g. malformed email on `/api/unsubscribe`) |
| 429    | Rate limit exceeded -- retry after the period resets |
| 503    | Mautic API unreachable -- request was not processed, retry later |
