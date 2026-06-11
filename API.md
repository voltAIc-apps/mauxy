# API Reference

Base URL: `https://mauxy.engage.wapsol.de` (set via `DEPLOY_DOMAIN`)

Mauxy is the single newsletter (un)subscribe authority in front of Mautic. Every
`/api/subscribe` and `/api/unsubscribe` call needs **two** things:

1. **A per-site key** — `Authorization: Bearer <site-key>`. Each site is configured
   in `MAUXY_SITES` with its own key, Mautic `segment` and quiz `topic`. Send it
   over HTTPS (TLS is the MITM defence). Unknown/missing key → `401`.
2. **A bot-defence answer** — fetch a question from `GET /api/challenge`, then echo
   the `challenge` token and the correct `answer` index back. Wrong/expired → `409`.

The honeypot field `company_website` must stay empty; if set, the request is
accepted-looking but silently dropped.

---

## GET /api/challenge

Return a multiple-choice question for the calling site's topic plus a signed,
expiring token. The correct answer is never sent to the client.

**Auth:** `Authorization: Bearer <site-key>`. **Rate limit:** 30/min per IP.

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

Find/create the Mautic contact and add it to the site's segment. Mautic runs the
double-opt-in campaign, so a new signup returns `pending_confirmation`.

**Auth:** `Authorization: Bearer <site-key>`. **Rate limit:** 60/min per IP.

### Request

| Header        | Value                  |
|---------------|------------------------|
| Content-Type  | application/json       |
| Authorization | Bearer `<site-key>`    |

```json
{
  "email": "user@example.com",
  "challenge": "odoo.editions.1764500000.6f1c…",
  "answer": 0,
  "company_website": ""
}
```

`source`, `consent`, `consentedAt`, `pageUrl`, `locale` are accepted but advisory —
the target segment comes from the site key, never the body.

### Responses

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"status":"pending_confirmation"}` | Added to segment; Mautic will send the opt-in mail. (Also returned for a dropped honeypot.) |
| 401 | `{"error":"unauthorized"}` | Missing/unknown site key |
| 409 | `{"status":"quiz_failed"}` | Wrong/expired bot-defence answer — re-fetch a challenge and retry |
| 502 | `{"status":"error"}` | Mautic reachable but a sub-step (create / segment) failed |
| 503 | `{"status":"service_unavailable"}` | Mautic unreachable — retry later |

### Example

```bash
curl -X POST https://mauxy.engage.wapsol.de/api/subscribe \
  -H "Authorization: Bearer YOUR_SITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","challenge":"<token>","answer":0}'
```

---

## POST /api/unsubscribe

Add an email to the Mautic Do-Not-Contact (DNC) list.

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
| 503 | `{"status":"service_unavailable"}` | Mautic unreachable |

---

## GET /api/actions

Query the action log. Admin-only, `Authorization: Bearer <ADMIN_API_KEY>`. `403` if
`ADMIN_API_KEY` unset, `401` if wrong. Query params: `email`, `result`, `limit`
(1–500, default 50), `offset`. Each row now also carries `action`
(`subscribe`|`unsubscribe`) and `site`. `result` values include `ok`, `not_found`,
`quiz_failed`, `honeypot`, `mautic_unreachable`, `error`.

---

## GET /health · GET /health/detail

Kubernetes probes, always HTTP 200. `/health` → `{"status":"ok","mautic":"reachable"}`
(`mautic` may be `pending` / `HTTP <code>` / `connection error: …`). `/health/detail`
adds `status: ok|degraded` and `cache_age_seconds` (Mautic check cached 30s).
