"""CLI entrypoint.

Two transports:

- `stdio` (default) — one server process per client, started by the client.
  The trust boundary is the local process; there is no OAuth.
- `http` — one long-lived Streamable-HTTP server for remote clients
  (claude.ai custom connectors, Claude Desktop, Claude Code). Every request
  must carry a verified access token, so this transport refuses to start
  without OAuth unless the operator explicitly opts out for loopback testing.

Environment (HTTP):

    MCP_TRANSPORT=http
    MCP_HTTP_HOST      default 127.0.0.1 — bind wider only when a firewall
                       limits who can reach the port (e.g. your tunnel host)
    MCP_HTTP_PORT      default 8000
    MCP_HTTP_PATH      default /mcp
    MCP_HTTP_STATELESS default 1 (see below)
    MCP_ALLOW_UNAUTHENTICATED_HTTP=1  loopback testing only

plus the MCP_AUTH_* / MCP_OAUTH_* variables in `auth.py`, the
MCP_RATELIMIT_* ones in `ratelimit.py` and MEALIE_* in `client.py`.
"""

from __future__ import annotations

import os
import sys

from mealie_mcp.app import mcp
from mealie_mcp.auth import AuthConfigError, get_auth_settings
from mealie_mcp.client import MealieConfigError, MealieError, get_client

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def resolve_transport(env: dict[str, str] | None = None) -> str:
    src = os.environ if env is None else env
    transport = (src.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("streamable-http", "streamable_http"):
        transport = "http"
    if transport not in ("stdio", "http"):
        raise AuthConfigError(f"MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}.")
    return transport


def check_http_is_authenticated(env: dict[str, str] | None = None) -> None:
    """Refuse to serve HTTP without token verification."""
    src = os.environ if env is None else env
    if get_auth_settings().enabled:
        return
    if _truthy(src.get("MCP_ALLOW_UNAUTHENTICATED_HTTP")):
        return
    raise AuthConfigError(
        "Refusing to start the HTTP transport with MCP_AUTH_MODE=none: every caller "
        "would be anonymous, including for the tools that write. Set "
        "MCP_AUTH_MODE=oauth-proxy (see docs/DEPLOY.md), or "
        "MCP_ALLOW_UNAUTHENTICATED_HTTP=1 for local loopback testing only."
    )


def check_mealie() -> None:
    """Fail fast if the Mealie token is missing or rejected."""
    try:
        me = get_client().whoami()
    except (MealieConfigError, MealieError) as exc:
        raise AuthConfigError(f"Mealie check failed: {exc}") from exc
    if me.get("admin"):
        print(
            "mealie-mcp: WARNING — MEALIE_TOKEN belongs to an ADMIN user. Use a "
            "dedicated non-admin user so a leaked token cannot administer Mealie.",
            file=sys.stderr,
        )


def main() -> None:
    # Tool modules register themselves on import via @mcp.tool()
    from mealie_mcp.tools import mealplans, organizers, recipes, shopping  # noqa: F401

    try:
        transport = resolve_transport()
        check_mealie()
        if transport == "stdio":
            mcp.run()
            return
        check_http_is_authenticated()
    except AuthConfigError as exc:
        print(f"mealie-mcp: {exc}", file=sys.stderr)
        sys.exit(2)

    import uvicorn
    from starlette.middleware import Middleware

    from mealie_mcp.ratelimit import RateLimitMiddleware

    # Stateless by default: every tool call carries its own auth and needs no
    # server-side session; with sessions a restart makes the client's session
    # id unknown -> opaque "400 Bad Request" until the client reconnects.
    stateless = os.environ.get("MCP_HTTP_STATELESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    app = mcp.http_app(
        path=os.environ.get("MCP_HTTP_PATH", DEFAULT_PATH),
        middleware=[Middleware(RateLimitMiddleware)],
        stateless_http=stateless,
    )
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MCP_HTTP_PORT", DEFAULT_PORT)),
        log_level="info",
        # Needed for the rate limiter and for correct scheme/host behind a proxy.
        proxy_headers=True,
        forwarded_allow_ips="*" if _truthy(os.environ.get("MCP_TRUST_FORWARDED_FOR")) else None,
    )


if __name__ == "__main__":
    main()
