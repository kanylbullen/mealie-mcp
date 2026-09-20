import pytest

from mealie_mcp import auth


def test_settings_none_by_default():
    assert not auth.load_auth_settings({}).enabled


def test_proxy_requires_client_credentials():
    with pytest.raises(auth.AuthConfigError, match="MCP_OAUTH_CLIENT_ID"):
        auth.load_auth_settings(
            {
                "MCP_AUTH_MODE": "oauth-proxy",
                "MCP_OAUTH_ISSUER": "https://idp.example/application/o/x/",
                "MCP_OAUTH_AUDIENCE": "https://mcp.example/mcp",
                "MCP_PUBLIC_URL": "https://mcp.example",
            }
        )


def test_default_redirect_allowlist_is_not_open():
    s = auth.load_auth_settings(
        {
            "MCP_AUTH_MODE": "oauth",
            "MCP_OAUTH_ISSUER": "https://idp.example/",
            "MCP_OAUTH_AUDIENCE": "a,b",
            "MCP_PUBLIC_URL": "https://mcp.example",
        }
    )
    assert s.audiences == ("a", "b") and s.audience == ["a", "b"]
    assert "https://claude.ai/api/mcp/auth_callback" in s.allowed_client_redirect_uris
    assert "*" not in s.allowed_client_redirect_uris


def test_write_implies_read():
    assert auth.expand_scopes(["mealie:write"]) == {"mealie:write", "mealie:read"}
    assert auth.expand_scopes("garbage") == frozenset()


def test_check_scopes_enforced_when_enabled(monkeypatch):
    auth.set_auth_settings(
        auth.AuthSettings(mode="oauth", issuer="i", audiences=("a",), base_url="b")
    )
    try:
        ident = auth.Identity(actor="x", actor_sub="1", scopes=("mealie:read",))
        auth.check_scopes("mealie:read", identity=ident)
        with pytest.raises(auth.ScopeDenied):
            auth.check_scopes("mealie:write", identity=ident)
        with pytest.raises(auth.ScopeDenied):
            auth.check_scopes("mealie:read", identity=auth.Identity())
    finally:
        auth.set_auth_settings(None)


def test_check_scopes_noop_when_disabled():
    auth.set_auth_settings(auth.AuthSettings())
    try:
        auth.check_scopes("mealie:write", identity=auth.Identity())
    finally:
        auth.set_auth_settings(None)
