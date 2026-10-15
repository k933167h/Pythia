"""API access control: authentication, scopes, rate limits, request limits, headers.

Pure ASGI middleware (not BaseHTTPMiddleware) so Server-Sent Events stream untouched.

Modes
-----
* local-only (default — no tokens configured): only loopback clients are served.
  The Osiris UI proxies server-side from localhost, so the dashboard keeps working.
* token: set PYTHIA_ADMIN_TOKEN and/or PYTHIA_READ_TOKEN.
    - read  scope: GET/HEAD on any data endpoint
    - admin scope: everything, including POST/DELETE (model switch, webhooks, alerts…)
  Send `Authorization: Bearer <token>` (or `X-API-Key: <token>`).

Maps to OWASP LLM06/ASI03 (identity & privilege abuse), LLM10 (unbounded
consumption via rate limits) and the MCP security best practice that local HTTP
servers require a token or stay on loopback (DNS-rebinding defence).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import time
from typing import Optional

from . import observability as obs
from .config import CONFIG

PUBLIC_PATHS = {"/health", "/healthz", "/readyz"}
# POSTs that only *read* engine state (they cost LLM time, so they are rate-limited,
# but a read token — e.g. the MCP bridge's — may call them)
READ_SAFE_POSTS = {"/chat", "/whatif"}
# LLM-costly endpoints get a per-client rate limit
COSTLY = {("POST", "/chat"), ("POST", "/whatif"), ("POST", "/predict"), ("POST", "/brief/run"),
          ("POST", "/scorecard/resolve")}
MAX_BODY = 256 * 1024
# host names a browser may legitimately use for a loopback engine (DNS-rebinding guard)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host in ("testclient", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _match(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def scope_for(token: str) -> Optional[str]:
    if not token:
        return None
    if _match(token, CONFIG.admin_token):
        return "admin"
    if _match(token, CONFIG.read_token):
        return "read"
    return None


class _Bucket:
    def __init__(self, rate_per_min: int) -> None:
        self.rate = max(1, rate_per_min)
        self.state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self.state.get(key, (float(self.rate), now))
        tokens = min(float(self.rate), tokens + (now - last) * self.rate / 60.0)
        if tokens < 1.0:
            self.state[key] = (tokens, now)
            return False
        self.state[key] = (tokens - 1.0, now)
        if len(self.state) > 5000:                       # bound memory
            self.state.pop(next(iter(self.state)))
        return True


class SecurityMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self.bucket = _Bucket(CONFIG.llm_rate_per_min)

    async def _deny(self, send, status: int, msg: str, extra_headers: list | None = None) -> None:
        body = json.dumps({"detail": msg}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers += extra_headers or []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or (None, None))[0]

        if method == "OPTIONS":                          # CORS preflight → CORSMiddleware
            return await self.app(scope, receive, send)

        try:
            if int(headers.get("content-length", "0") or 0) > MAX_BODY:
                return await self._deny(send, 413, "request body too large")
        except ValueError:
            return await self._deny(send, 400, "bad content-length")
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            # buffer (bounded) so chunked uploads can't dodge the size cap, then replay
            chunks, size = [], 0
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                chunk = msg.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    return await self._deny(send, 413, "request body too large")
                chunks.append(chunk)
                if not msg.get("more_body"):
                    break
            body = b"".join(chunks)
            sent = False

            async def _replay():               # hand the buffered body to the app once
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}
            recv = _replay
        else:
            recv = receive

        token = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or headers.get("x-api-key", "").strip()
        token_mode = bool(CONFIG.admin_token or CONFIG.read_token)
        role = scope_for(token)

        if path not in PUBLIC_PATHS:
            if token_mode:
                if role is None:
                    obs.inc("pythia_auth_failures_total", reason="missing_or_bad_token")
                    _audit_denied(path, method, client, "missing_or_bad_token")
                    return await self._deny(send, 401, "authentication required",
                                            [(b"www-authenticate", b'Bearer realm="pythia"')])
                if method not in ("GET", "HEAD") and role != "admin" and path not in READ_SAFE_POSTS:
                    obs.inc("pythia_auth_failures_total", reason="insufficient_scope")
                    _audit_denied(path, method, client, "insufficient_scope")
                    return await self._deny(send, 403, "admin scope required for changes")
            else:
                if not _is_loopback(client):
                    obs.inc("pythia_auth_failures_total", reason="non_loopback_local_mode")
                    _audit_denied(path, method, client, "non_loopback_local_mode")
                    return await self._deny(send, 403, "engine is in local-only mode; "
                                            "set PYTHIA_ADMIN_TOKEN/PYTHIA_READ_TOKEN to allow remote clients")
                host = _host_only(headers.get("host", ""))
                if host and host not in _LOCAL_HOSTS:
                    # a loopback socket but a foreign Host header = DNS rebinding attempt
                    obs.inc("pythia_auth_failures_total", reason="host_header")
                    return await self._deny(send, 403, "unexpected Host header")

        # behind the Osiris proxy every request arrives from loopback; trust the proxy's
        # X-Forwarded-For (only from loopback) so limits and audit are per end user
        end_user = client
        if _is_loopback(client) and headers.get("x-forwarded-for"):
            end_user = headers["x-forwarded-for"].split(",")[0].strip()[:64] or client
        scope.setdefault("state", {})["end_user"] = end_user
        if (method, path) in COSTLY and not self.bucket.allow(f"{end_user}:{token[:8]}"):
            obs.inc("pythia_rate_limited_total", path=path)
            return await self._deny(send, 429, "rate limit exceeded — slow down",
                                    [(b"retry-after", b"10")])

        scope.setdefault("state", {})["role"] = role or ("local" if not token_mode else None)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                hs += [(b"x-content-type-options", b"nosniff"),
                       (b"x-frame-options", b"DENY"),
                       (b"referrer-policy", b"no-referrer")]
                if path in ("/predictions", "/agent/view", "/chat", "/whatif", "/brief", "/state"):
                    hs.append((b"x-ai-generated", b"true"))
                message = {**message, "headers": hs}
            await send(message)

        return await self.app(scope, recv, send_wrapper)


def _host_only(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):                      # [::1] or [::1]:8088
        return h[: h.find("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def actor(request) -> str:
    """Who made this change, for the audit log (never the token itself)."""
    role = getattr(request.state, "role", None) if hasattr(request, "state") else None
    who = getattr(request.state, "end_user", None) if hasattr(request, "state") else None
    client = who or (request.client.host if getattr(request, "client", None) else "?")
    return f"{role or 'anon'}@{client}"


_last_denied: dict[str, float] = {}


def _audit_denied(path: str, method: str, client: Optional[str], reason: str) -> None:
    # throttle: one audit line per client+reason per minute (a scanner shouldn't flood it)
    key = f"{client}:{reason}"
    now = time.monotonic()
    if now - _last_denied.get(key, float("-inf")) < 60:
        return
    _last_denied[key] = now
    if len(_last_denied) > 2000:                 # bound memory under a wide scan
        for k in list(_last_denied)[:1000]:
            _last_denied.pop(k, None)
    from .audit import AUDIT
    AUDIT.record("access_denied", actor=str(client), path=path, method=method, reason=reason)
