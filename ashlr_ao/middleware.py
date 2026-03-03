"""
Ashlr AO — HTTP Middleware

Rate limiting, security headers, CORS, request logging, and compression.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    pass

log = logging.getLogger("ashlr")


# ─────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────

class RateLimiter:
    """Token bucket rate limiter, keyed by client IP."""

    def __init__(self) -> None:
        self._buckets: dict[str, dict] = {}  # ip -> {tokens, last_refill}
        self._check_count: int = 0

    def check(self, ip: str, cost: float = 1.0, rate: float = 1.0, burst: float = 10.0) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). Rate is tokens/sec, burst is max tokens."""
        self._check_count += 1
        if self._check_count % 100 == 0:
            cutoff = time.monotonic() - 3600
            self._buckets = {k: v for k, v in self._buckets.items() if v["last_refill"] > cutoff}
        now = time.monotonic()
        bucket = self._buckets.get(ip)
        if not bucket:
            bucket = {"tokens": burst, "last_refill": now}
            self._buckets[ip] = bucket

        # Refill tokens
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(burst, bucket["tokens"] + elapsed * rate)
        bucket["last_refill"] = now

        if bucket["tokens"] >= cost:
            bucket["tokens"] -= cost
            return True, 0.0
        else:
            retry_after = (cost - bucket["tokens"]) / rate
            return False, retry_after

    def cleanup_stale(self, max_age: float = 300.0) -> None:
        """Remove buckets not accessed in max_age seconds."""
        now = time.monotonic()
        stale = [ip for ip, b in self._buckets.items() if now - b["last_refill"] > max_age]
        for ip in stale:
            del self._buckets[ip]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_client_ip(request: web.Request) -> str:
    """Extract client IP from request. Ignores X-Forwarded-For since this is
    a local-first server with no reverse proxy — trusting that header would
    allow trivial rate limit bypass."""
    peername = request.transport.get_extra_info("peername") if request.transport else None
    return peername[0] if peername else "unknown"


# Per-endpoint rate limit tiers: (rate tokens/sec, burst max)
_RATE_LIMIT_TIERS: dict[str, tuple[float, float]] = {
    # Heavy operations — strict limits
    "spawn": (0.5, 3.0),       # 1 spawn per 2s, burst of 3
    "bulk": (0.2, 2.0),        # 1 bulk op per 5s, burst of 2
    "batch-spawn": (0.2, 2.0),
    "fleet-export": (0.1, 1.0), # 1 export per 10s
    # Auth — strict to prevent brute-force
    "auth": (0.3, 3.0),        # 1 attempt per ~3s, burst of 3
    # Moderate operations
    "send": (2.0, 10.0),       # 2 sends/sec, burst of 10
    "restart": (0.5, 3.0),
    "delete": (1.0, 5.0),
    # Light operations — generous limits
    "default": (5.0, 20.0),    # 5 req/sec, burst of 20
}


def _get_rate_tier(path: str, method: str) -> str:
    """Determine rate limit tier from request path and method."""
    if "/auth/" in path:
        return "auth"
    if method == "POST" and "/agents" in path and "bulk" in path:
        return "bulk"
    if method == "POST" and "batch-spawn" in path:
        return "batch-spawn"
    if method == "POST" and path.endswith("/agents"):
        return "spawn"
    if "fleet/export" in path:
        return "fleet-export"
    if "/send" in path or "/message" in path:
        return "send"
    if "/restart" in path:
        return "restart"
    if method == "DELETE":
        return "delete"
    return "default"


# ─────────────────────────────────────────────
# Middleware Functions
# ─────────────────────────────────────────────

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Apply per-endpoint rate limiting to API requests."""
    path = request.path
    # Skip non-API paths (dashboard, static, WebSocket)
    if not path.startswith("/api/"):
        return await handler(request)

    rl: RateLimiter | None = request.app.get("rate_limiter")
    if not rl:
        return await handler(request)

    ip = _get_client_ip(request)
    tier = _get_rate_tier(path, request.method)
    rate, burst = _RATE_LIMIT_TIERS.get(tier, _RATE_LIMIT_TIERS["default"])

    # Use tier-specific bucket key
    bucket_key = f"{ip}:{tier}"
    allowed, retry_after = rl.check(bucket_key, cost=1.0, rate=rate, burst=burst)

    if not allowed:
        return web.json_response(
            {"error": "Rate limit exceeded", "retry_after": round(retry_after, 1)},
            status=429,
            headers={"Retry-After": str(int(retry_after + 1))},
        )

    return await handler(request)


@web.middleware
async def request_logging_middleware(request: web.Request, handler):
    """Log API request method, path, status, duration, user, and body size."""
    if not request.path.startswith("/api/"):
        return await handler(request)
    # Increment request counter on manager (avoids mutating started app state)
    request.app["agent_manager"]._total_api_requests += 1
    start = time.monotonic()
    user = request.get("user")
    user_id = user.id if user else "-"
    body_size = request.content_length or 0
    try:
        response = await handler(request)
        duration_ms = (time.monotonic() - start) * 1000
        if duration_ms > 1000:
            log.warning(f"SLOW {request.method} {request.path} [{user_id}] → {response.status} ({duration_ms:.0f}ms, {body_size}B)")
        else:
            log.debug(f"{request.method} {request.path} [{user_id}] → {response.status} ({duration_ms:.0f}ms)")
        return response
    except web.HTTPException as e:
        duration_ms = (time.monotonic() - start) * 1000
        log.debug(f"{request.method} {request.path} [{user_id}] → {e.status} ({duration_ms:.0f}ms)")
        raise
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        log.warning(f"{request.method} {request.path} [{user_id}] → 500 ({duration_ms:.0f}ms) {e}")
        raise


@web.middleware
async def compression_middleware(request: web.Request, handler):
    """Enable gzip compression for large API responses when client supports it."""
    response = await handler(request)
    # Only compress API JSON responses larger than 1KB
    if (
        request.path.startswith("/api/")
        and "gzip" in request.headers.get("Accept-Encoding", "")
        and isinstance(response, web.Response)
        and response.content_type == "application/json"
        and response.body
        and len(response.body) > 1024
    ):
        import gzip as gzip_mod
        compressed = gzip_mod.compress(response.body, compresslevel=6)
        if len(compressed) < len(response.body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Add security headers including Content-Security-Policy and CORS."""
    # Handle CORS preflight
    allowed_origin = os.environ.get("ASHLR_ALLOWED_ORIGINS", "*")
    if request.method == "OPTIONS":
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "*")
        response.headers["Access-Control-Max-Age"] = "3600"
        if allowed_origin != "*":
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    response = await handler(request)

    # CORS headers
    response.headers["Access-Control-Allow-Origin"] = allowed_origin
    if allowed_origin != "*":
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Expose-Headers"] = "*"

    # CSP: allow inline styles/scripts (required for single-file dashboard)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com https://fonts.googleapis.com; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'"
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response
