"""OAuth 2.1 resource-server auth for the HTTP transport.

Two auth boundaries, and only the first one is OAuth
-----------------------------------------------------
OAuth gates **client -> this server**. The **server -> Mealie** hop is a
separate boundary: Mealie's API takes a Mealie API token, not an IdP access
token, so this server holds *one* Mealie API token (ideally for a dedicated,
non-admin Mealie user) and never forwards the caller's token to Mealie.

Consequently everything this server does shows up in Mealie as that one user.
The IdP decides *who* may talk to the server (group membership, consent); the
scopes decide *what kind* of calls they may make.

What this module gives the client
---------------------------------
`build_auth_provider()` returns a FastMCP auth provider that serves RFC 9728
Protected Resource Metadata at `/.well-known/oauth-protected-resource` and
answers unauthenticated calls with a `WWW-Authenticate` challenge carrying
`resource_metadata`. That pair is what lets a web client (claude.ai custom
connectors, Claude Desktop) discover the authorization server on its own.

Two modes:

- ``oauth`` — plain resource server; the client already holds a token for
  this resource.
- ``oauth-proxy`` — resource server **plus** a thin authorization-server
  front. MCP clients expect RFC 7591 Dynamic Client Registration, and many
  IdPs (Authentik outside its enterprise tier, for one) do not offer it. The
  proxy accepts the client's DCR call, then runs the real flow upstream with
  *this server's* pre-registered credentials. Login and consent are still the
  IdP's, and client redirect URIs are matched against an allow-list.

Scopes are enforced at call sites
---------------------------------
`@requires_scope(...)` wraps every tool function, so a scope that is not
granted denies the call rather than decorating the metadata.

This module is adapted from molnkontakt/odoo-mcp (LGPL-3.0), whose author is
also the author of this project.
"""

from __future__ import annotations

import functools
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

#: Read tier: search and read recipes, meal plans, shopping lists.
SCOPE_READ = "mealie:read"
#: Write tier: create/update recipes, plan meals, edit shopping lists.
SCOPE_WRITE = "mealie:write"

ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE)

#: Scopes that carry identity rather than permission. They grant nothing here,
#: but a client that may not ask for a scope cannot be issued it, and `email`
#: is what makes the caller recognisable in the logs.
IDENTITY_SCOPES: tuple[str, ...] = ("openid", "profile", "email", "offline_access")

USER_AGENT = "mealie-mcp/0.1 (+https://github.com/kanylbullen/mealie-mcp)"

_IMPLIES: dict[str, tuple[str, ...]] = {
    SCOPE_WRITE: (SCOPE_READ,),
}

AuthMode = Literal["none", "oauth", "oauth-proxy"]

F = TypeVar("F", bound=Callable[..., Any])


class AuthConfigError(RuntimeError):
    """Raised at startup when the auth configuration is unusable.

    Always fatal: a resource server that starts with a half-configured
    verifier is a resource server that accepts unverified tokens.
    """


class ScopeDenied(Exception):
    """Raised when the caller's token lacks a scope the tool requires."""


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode = "none"
    issuer: str | None = None
    jwks_uri: str | None = None
    #: Accepted `aud` values. More than one is allowed because e.g. Authentik's
    #: audience override lives in a scope mapping, and a token minted before
    #: that mapping applied carries `aud = client_id` instead.
    audiences: tuple[str, ...] = ()
    base_url: str | None = None
    resource_name: str = "mealie-mcp"
    #: Upstream (IdP) client credentials — proxy mode only.
    client_id: str | None = None
    client_secret: str | None = None
    allowed_client_redirect_uris: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    @property
    def audience(self) -> str | list[str] | None:
        if not self.audiences:
            return None
        return self.audiences[0] if len(self.audiences) == 1 else list(self.audiences)


@dataclass(frozen=True)
class Identity:
    """Who is calling, as far as this server can tell."""

    actor: str | None = None
    actor_sub: str | None = None
    client_id: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def authenticated(self) -> bool:
        return self.actor_sub is not None


#: Identity used when the server runs without OAuth (stdio, local dev).
LOCAL_IDENTITY = Identity(actor="local:stdio")


#: Redirect URIs accepted from MCP clients in proxy mode. Wildcards allowed.
#: `None` (the FastMCP default) accepts *any* client redirect, which turns the
#: DCR endpoint into an open redirector for the authorization code.
DEFAULT_CLIENT_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
)


def load_auth_settings(env: dict[str, str] | None = None) -> AuthSettings:
    """Read auth configuration from the environment (pure; no network)."""
    src = os.environ if env is None else env
    mode = (src.get("MCP_AUTH_MODE") or "none").strip().lower()
    if mode not in ("none", "oauth", "oauth-proxy"):
        raise AuthConfigError(
            f"MCP_AUTH_MODE must be 'none', 'oauth' or 'oauth-proxy', got {mode!r}."
        )
    if mode == "none":
        return AuthSettings(mode="none")

    issuer = (src.get("MCP_OAUTH_ISSUER") or "").strip()
    audience = (src.get("MCP_OAUTH_AUDIENCE") or "").strip()
    base_url = (src.get("MCP_PUBLIC_URL") or "").strip()
    jwks_uri = (src.get("MCP_OAUTH_JWKS_URI") or "").strip()
    client_id = (src.get("MCP_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (src.get("MCP_OAUTH_CLIENT_SECRET") or "").strip()
    redirects = (src.get("MCP_OAUTH_CLIENT_REDIRECT_URIS") or "").strip()

    required: list[tuple[str, str]] = [
        ("MCP_OAUTH_ISSUER", issuer),
        ("MCP_OAUTH_AUDIENCE", audience),
        ("MCP_PUBLIC_URL", base_url),
    ]
    if mode == "oauth-proxy":
        required += [
            ("MCP_OAUTH_CLIENT_ID", client_id),
            ("MCP_OAUTH_CLIENT_SECRET", client_secret),
        ]

    missing = [name for name, value in required if not value]
    if missing:
        raise AuthConfigError(
            f"MCP_AUTH_MODE={mode} requires {', '.join(missing)}. "
            f"MCP_OAUTH_ISSUER is the IdP issuer (Authentik: "
            f"https://auth.example.com/application/o/<slug>/), "
            f"MCP_OAUTH_AUDIENCE is the value this server requires in the token's "
            f"`aud` (comma-separate to accept more than one), MCP_PUBLIC_URL is the "
            f"externally reachable base URL of this server, and "
            f"MCP_OAUTH_CLIENT_ID/SECRET are the credentials the IdP issued for "
            f"*this server* as an OAuth client."
        )

    return AuthSettings(
        mode=mode,  # type: ignore[arg-type]
        issuer=issuer,
        jwks_uri=jwks_uri or None,
        audiences=tuple(a.strip() for a in audience.split(",") if a.strip()),
        base_url=base_url,
        resource_name=(src.get("MCP_RESOURCE_NAME") or "mealie-mcp").strip(),
        client_id=client_id or None,
        client_secret=client_secret or None,
        allowed_client_redirect_uris=(
            tuple(u.strip() for u in redirects.split(",") if u.strip())
            if redirects
            else DEFAULT_CLIENT_REDIRECT_URIS
        ),
    )


def discover_oidc(issuer: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch the issuer's OIDC discovery document. Failing here is fatal on purpose."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    # Explicit User-Agent: urllib's default is blocked outright by Cloudflare's
    # managed bot rules, and the resulting 403 reads like a dead endpoint.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return dict(json.loads(resp.read().decode()))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AuthConfigError(
            f"OIDC discovery failed for issuer {issuer!r} ({url}): {exc}."
        ) from exc


_settings: AuthSettings | None = None


def get_auth_settings() -> AuthSettings:
    global _settings
    if _settings is None:
        _settings = load_auth_settings()
    return _settings


def set_auth_settings(settings: AuthSettings | None) -> None:
    """Override the cached settings. For tests and for startup."""
    global _settings
    _settings = settings


def build_auth_provider(settings: AuthSettings | None = None) -> Any | None:
    """Build the FastMCP auth provider, or None when auth is disabled."""
    settings = settings or get_auth_settings()
    if not settings.enabled:
        return None

    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    assert settings.issuer and settings.audiences and settings.base_url

    doc: dict[str, Any] | None = None
    jwks_uri = settings.jwks_uri
    if jwks_uri is None or settings.mode == "oauth-proxy":
        doc = discover_oidc(settings.issuer)
        jwks_uri = jwks_uri or str(doc.get("jwks_uri") or "")
    if not jwks_uri:
        raise AuthConfigError(
            f"No JWKS URI for issuer {settings.issuer!r}; set MCP_OAUTH_JWKS_URI."
        )

    verifier = JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=settings.issuer,
        audience=settings.audience,
    )

    if settings.mode == "oauth":
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[settings.issuer],  # type: ignore[list-item]
            base_url=settings.base_url,
            scopes_supported=list(ALL_SCOPES),
            resource_name=settings.resource_name,
        )

    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    assert doc is not None
    missing = [k for k in ("authorization_endpoint", "token_endpoint") if not doc.get(k)]
    if missing:
        raise AuthConfigError(
            f"OIDC discovery document for {settings.issuer!r} is missing "
            f"{', '.join(missing)}; cannot proxy to it."
        )

    return OAuthProxy(
        upstream_authorization_endpoint=str(doc["authorization_endpoint"]),
        upstream_token_endpoint=str(doc["token_endpoint"]),
        upstream_revocation_endpoint=(
            str(doc["revocation_endpoint"]) if doc.get("revocation_endpoint") else None
        ),
        upstream_client_id=str(settings.client_id),
        upstream_client_secret=str(settings.client_secret),
        token_verifier=verifier,
        base_url=settings.base_url,
        valid_scopes=list(ALL_SCOPES + IDENTITY_SCOPES),
        allowed_client_redirect_uris=list(settings.allowed_client_redirect_uris),
        # The IdP's consent screen plus the "which client is asking" step. A
        # DCR endpoint that approves silently is how an unknown client gets a token.
        require_authorization_consent=True,
    )


def expand_scopes(scopes: object) -> frozenset[str]:
    """Expand granted scopes through the tier implications."""
    if not isinstance(scopes, (list, tuple, set, frozenset)):
        return frozenset()
    granted = {str(s) for s in scopes}
    changed = True
    while changed:
        changed = False
        for scope in list(granted):
            for implied in _IMPLIES.get(scope, ()):
                if implied not in granted:
                    granted.add(implied)
                    changed = True
    return frozenset(granted)


def _current_token() -> Any | None:
    from fastmcp.server.dependencies import get_access_token

    try:
        return get_access_token()
    except Exception:
        return None


def current_identity() -> Identity:
    """Resolve the caller from the access token of the request in flight."""
    if not get_auth_settings().enabled:
        return LOCAL_IDENTITY

    token = _current_token()
    if token is None:
        return Identity()

    claims: dict[str, Any] = getattr(token, "claims", None) or {}
    sub = claims.get("sub")
    actor = claims.get("email") or claims.get("preferred_username") or claims.get("name") or sub
    return Identity(
        actor=str(actor) if actor else None,
        actor_sub=str(sub) if sub else None,
        client_id=getattr(token, "client_id", None) or claims.get("azp"),
        scopes=tuple(sorted(expand_scopes(getattr(token, "scopes", ())))),
    )


def check_scopes(*needed: str, identity: Identity | None = None) -> None:
    """Raise ScopeDenied unless the caller holds every scope in `needed`.

    A no-op when auth is disabled: stdio deployments have no token, and the
    trust boundary there is the local process.
    """
    if not get_auth_settings().enabled:
        return

    ident = current_identity() if identity is None else identity
    if not ident.authenticated:
        raise ScopeDenied(
            "This tool requires an authenticated caller, but the request carried "
            "no verified access token."
        )

    missing = sorted(set(needed) - set(ident.scopes))
    if missing:
        raise ScopeDenied(
            f"Missing scope(s) {missing} for this tool. Granted: {sorted(ident.scopes) or '[]'}."
        )


def requires_scope(*needed: str) -> Callable[[F], F]:
    """Enforce scopes before a tool body runs. Apply *under* `@mcp.tool()`."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            check_scopes(*needed)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
