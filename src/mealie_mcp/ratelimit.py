"""Per-client rate limit for the OAuth endpoints.

In proxy mode the server is its own authorization server in front of the IdP,
so `/register` (Dynamic Client Registration), `/authorize` and `/token` are
reachable by anyone who can reach the host. None of them yields anything
without an interactive login, but an open registration endpoint can be filled
with junk clients and `/token` is where stolen codes or refresh tokens would
be tried. A cheap sliding-window limit per client IP makes both dull.

Configuration (environment):

    MCP_RATELIMIT_PER_MINUTE   requests per IP per minute on the guarded paths
                               (default 30; 0 disables)
    MCP_RATELIMIT_PATHS        comma-separated path suffixes
                               (default "/register,/token,/authorize")
    MCP_TRUST_FORWARDED_FOR    1 = take the client address from proxy headers:
                               `CF-Connecting-IP` first, then the first entry of
                               `X-Forwarded-For`. Set it ONLY when nothing but
                               your own reverse proxy / tunnel can reach the
                               port — otherwise a client picks its own bucket.

Why the header matters behind a tunnel: every request arrives from the tunnel
daemon's address, so a limiter keyed on the socket peer puts all clients in
one shared bucket — legitimate users throttle each other while an attacker
keeps the whole quota.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_PATHS = ("/register", "/token", "/authorize")
_TRUE = ("1", "true", "yes", "on")
#: Cap on tracked clients: without it an attacker rotating addresses turns the
#: bucket table into a memory leak.
MAX_CLIENTS = 4096


def _settings() -> tuple[int, tuple[str, ...], bool]:
    per_minute = int(os.environ.get("MCP_RATELIMIT_PER_MINUTE", "30") or 0)
    raw = os.environ.get("MCP_RATELIMIT_PATHS", "")
    paths = tuple(p.strip() for p in raw.split(",") if p.strip()) or DEFAULT_PATHS
    trust = os.environ.get("MCP_TRUST_FORWARDED_FOR", "").strip().lower() in _TRUE
    return per_minute, paths, trust


class RateLimitMiddleware:
    """Sliding window per client. Memory bounded by evicting idle buckets."""

    def __init__(
        self,
        app: ASGIApp,
        per_minute: int | None = None,
        paths: tuple[str, ...] | None = None,
        trust_forwarded_for: bool | None = None,
    ):
        env_per_minute, env_paths, env_trust = _settings()
        self.app = app
        self.per_minute = env_per_minute if per_minute is None else per_minute
        self.paths = env_paths if paths is None else paths
        self.trust_forwarded_for = env_trust if trust_forwarded_for is None else trust_forwarded_for
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def _client(self, scope: Scope) -> str:
        if self.trust_forwarded_for:
            headers = dict(scope.get("headers", []))
            cf = headers.get(b"cf-connecting-ip")
            if cf:
                return cf.decode("latin-1").strip()
            xff = headers.get(b"x-forwarded-for")
            if xff:
                first = xff.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _guarded(self, path: str) -> bool:
        return any(path == p or path.endswith(p) for p in self.paths)

    def _allow(self, key: str, now: float) -> tuple[bool, int]:
        window = now - 60.0
        with self._lock:
            q = self._hits[key]
            while q and q[0] < window:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, int(q[0] + 60.0 - now) + 1
            q.append(now)
            if now - self._last_sweep > 300 or len(self._hits) > MAX_CLIENTS:
                self._last_sweep = now
                for k in [k for k, v in self._hits.items() if not v or v[-1] < window]:
                    del self._hits[k]
                if len(self._hits) > MAX_CLIENTS:
                    # Still over the cap after evicting idle buckets: drop the
                    # oldest-touched ones rather than grow without bound.
                    for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[
                        : len(self._hits) - MAX_CLIENTS
                    ]:
                        del self._hits[k]
            return True, 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self.per_minute <= 0
            or not self._guarded(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        allowed, retry_after = self._allow(self._client(scope), time.monotonic())
        if allowed:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {
                "error": "rate_limited",
                "error_description": "Too many requests to this endpoint; try again later.",
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)
