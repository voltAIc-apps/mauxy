"""
Generic signed webhook sink (routing.py target `webhook`): mauxy POSTs one JSON event per
(action, target) to an HTTPS endpoint of another system -- e.g. the flywheel (opt-outs into its
do-not-contact list), later others. Retries come from the fan-out outbox (route_delivery).

Request (one per delivery attempt; the body is identical on every retry, the timestamp is not):

  POST <url>                      Content-Type: application/json
  X-Mauxy-Event: <action>         subscribe | unsubscribe | lead | selfcheck | ...
  X-Mauxy-Site: <site>
  X-Mauxy-Delivery: <id>          = Idempotency-Key: same value on every retry of this delivery
  X-Mauxy-Timestamp: <unix s>
  X-Mauxy-Signature: v1=<hex>     HMAC-SHA256(secret, "<timestamp>.<raw body>")
  {"id": "<idempotency key>", "event": "<action>", "site": "<site>", "occurred_at": "<ISO UTC>",
   "data": {...}}

Receiver contract (README "Webhook receivers"): verify the signature in constant time over the raw
body, refuse timestamps older than 5 minutes, de-duplicate by the id, answer 2xx fast.

Security (SSRF / MITM, owner requirement 2026-11-09):
  - https only; host must be EXACTLY in WEBHOOK_ALLOWED_HOSTS (env, comma-separated; empty = every
    webhook refused); no user info in the URL
  - DNS is resolved by mauxy and EVERY address must be public (ipaddress.is_global: no private,
    loopback, link-local / metadata 169.254.x, CGNAT, multicast, reserved, IPv4-mapped private);
    the connection is then pinned to the checked address (TLS SNI + certificate check still on the
    host name) so a DNS answer cannot change between check and connect (rebinding)
  - redirects are never followed (3xx = permanent failure); proxies from env ignored (trust_env)
  - TLS certificate + host name verified (system / certifi CA store); a TLS failure is permanent
    (possible MITM, never retried into a downgrade). WEBHOOK_CA_FILE = extra trust anchor for a
    local mock receiver / tests only, never in stag / prod
  - timeout WEBHOOK_TIMEOUT (default 10 s); response body never read beyond the status
  - secret per target from env WEBHOOK_SECRET_<REF> (>= 32 chars), never in SQLite, never logged
Errors: sinks.SinkTransient (network, timeout, 408 / 425 / 429 / 5xx -> outbox retries) or
sinks.SinkPermanent (config, SSRF refusal, 3xx, other 4xx -> failed + operator mail).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from sinks import SinkPermanent, SinkTransient

SECRET_MIN = 32
REF_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_BAD = re.compile(r"[\x00-\x20\x7f\"'<>\\`{}|^]")
TRANSIENT_STATUS = {408, 425, 429}

# Test / local hooks: an httpx transport (MockTransport) and a resolver host -> [ip, ...].
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
RESOLVER: Optional[Callable[[str, int], list[str]]] = None


class WebhookConfigError(ValueError):
    """URL / secret refused (admin 400, delivery SinkPermanent)."""


def allowed_hosts() -> set[str]:
    raw = os.environ.get("WEBHOOK_ALLOWED_HOSTS", "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def timeout() -> float:
    return float(os.environ.get("WEBHOOK_TIMEOUT", "") or 10)


def _verify():
    """TLS verification: always on; WEBHOOK_CA_FILE only adds a local test CA."""
    ca = os.environ.get("WEBHOOK_CA_FILE", "").strip()
    if not ca:
        return True
    ctx = ssl.create_default_context(cafile=ca)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _tls_failure(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def check_url(url: str) -> tuple[str, int, str]:
    """(host, port, path+query) of an allowed webhook URL. Raises WebhookConfigError."""
    if not isinstance(url, str) or not 0 < len(url) <= 500 or _BAD.search(url):
        raise WebhookConfigError("url: 1..500 characters, no spaces / quotes / brackets")
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError:
        raise WebhookConfigError("url: not a URL") from None
    if parts.scheme != "https":
        raise WebhookConfigError("url: https only")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WebhookConfigError("url: host required, no user info")
    if parts.fragment:
        raise WebhookConfigError("url: no fragment")
    if host not in allowed_hosts():
        raise WebhookConfigError(f"url: host {host} not in WEBHOOK_ALLOWED_HOSTS")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return host, port, path


def secret_env(ref: str) -> str:
    return f"WEBHOOK_SECRET_{ref.upper()}"


def secret_for(ref: str) -> bytes:
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise WebhookConfigError("secret_ref: [a-z0-9_]{1,40}")
    secret = os.environ.get(secret_env(ref), "")
    if len(secret) < SECRET_MIN:
        raise WebhookConfigError(f"{secret_env(ref)} not set or shorter than {SECRET_MIN} characters")
    return secret.encode()


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: bytes, timestamp: str, body: bytes, signature: str, *, tolerance: int = 300,
           now: Optional[float] = None) -> bool:
    """Reference implementation of the receiver check (also used by the tests)."""
    if not timestamp.isdigit() or abs((now or time.time()) - int(timestamp)) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_loopback \
        and not ip.is_link_local and not ip.is_private and not ip.is_unspecified


async def resolve(host: str, port: int) -> list[str]:
    if RESOLVER is not None:
        return list(RESOLVER(host, port))
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SinkTransient(f"webhook dns: {exc.__class__.__name__}") from None
    return sorted({info[4][0] for info in infos})


def body_for(*, idem_key: str, action: str, site: str, data: dict, occurred_at: str) -> bytes:
    return json.dumps({"id": idem_key, "event": action, "site": site, "occurred_at": occurred_at, "data": data},
                      ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


async def deliver(params: dict, *, idem_key: str, action: str, site: str, data: dict, occurred_at: str) -> int:
    """One signed POST. Returns the HTTP status (2xx). Raises SinkTransient / SinkPermanent."""
    try:
        host, port, path = check_url(params.get("url", ""))
        secret = secret_for(params.get("secret_ref", ""))
    except WebhookConfigError as exc:
        raise SinkPermanent(f"webhook config: {exc}") from None
    addrs = await resolve(host, port)
    if not addrs:
        raise SinkTransient("webhook dns: no address")
    if not all(public_ip(a) for a in addrs):
        raise SinkPermanent("webhook refused: host resolves to a non-public address")  # SSRF
    ip = addrs[0]
    netloc = f"[{ip}]" if ":" in ip else ip
    pinned = f"https://{netloc}:{port}{path}"
    body = body_for(idem_key=idem_key, action=action, site=site, data=data, occurred_at=occurred_at)
    ts = str(int(time.time()))
    headers = {
        "Host": host if port == 443 else f"{host}:{port}",
        "Content-Type": "application/json",
        "User-Agent": "mauxy-webhook/1",
        "X-Mauxy-Event": action,
        "X-Mauxy-Site": site,
        "X-Mauxy-Delivery": idem_key,
        "Idempotency-Key": idem_key,
        "X-Mauxy-Timestamp": ts,
        "X-Mauxy-Signature": sign(secret, ts, body),
    }
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=timeout(), follow_redirects=False,
                                     trust_env=False, verify=_verify()) as client:
            req = client.build_request("POST", pinned, content=body, headers=headers,
                                       extensions={"sni_hostname": host})
            resp = await client.send(req, stream=True)  # status only: the body is never read
            await resp.aclose()
    except httpx.TimeoutException:
        raise SinkTransient("webhook timeout") from None
    except httpx.TransportError as exc:
        if _tls_failure(exc):  # certificate / host name / handshake: possible MITM
            raise SinkPermanent("webhook TLS verification failed") from None
        raise SinkTransient(f"webhook network: {exc.__class__.__name__}") from None
    status = resp.status_code
    if 200 <= status < 300:
        return status
    if 300 <= status < 400:
        raise SinkPermanent(f"webhook HTTP {status} (redirects are not followed)")
    if status in TRANSIENT_STATUS or status >= 500:
        raise SinkTransient(f"webhook HTTP {status}")
    raise SinkPermanent(f"webhook HTTP {status}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
