# Deploying mealie-mcp with OAuth

Reference layout: one small Linux host (a container next to Mealie is ideal), systemd, a reverse
proxy or tunnel that terminates TLS, and an OIDC IdP (Authentik shown).

## 1. Mealie side

Create a dedicated **non-admin** user and an API token for it:

```bash
MEALIE_URL=http://127.0.0.1:9000 MEALIE_ADMIN_USER=admin \
  python3 scripts/setup_mealie_user.py --password-env MEALIE_ADMIN_PASSWORD --print
```

(`--print` writes the two secrets to stdout; `--store '<cmd> {name}'` pipes each into a vault
command instead.)

## 2. IdP side (Authentik)

Create an OAuth2/OIDC provider for the server itself:

- client type **confidential**, client id `mealie-mcp`
- redirect URI (strict): `https://mealie-mcp.example.com/auth/callback`
- **`grant_types`: `authorization_code`, `refresh_token`** — the API default is an empty list,
  which fails every authorize call with "request is otherwise malformed" and logs nothing
- signing key set, `sub_mode` = user email, issuer mode per-provider
- scope mappings `mealie:read` and `mealie:write`, each with the expression
  `return {"aud": "https://mealie-mcp.example.com/mcp"}` so tokens carry the right audience,
  plus the standard `openid`, `email`, `profile`, `offline_access`
- authorization flow **with explicit consent**
- an application bound to a group of the people who may use it

MCP clients (claude.ai, Claude Desktop, Claude Code) never talk to the IdP directly: they
register with **this server's** DCR endpoint (`/register`) and the server runs the real flow
upstream with its one pre-registered client. That is why the IdP only needs one redirect URI.

## 3. Server host

```bash
REF=<sha> ./deploy/install.sh
install -m 600 deploy/example.env /etc/mealie-mcp/secrets.env   # then fill it in
systemctl edit mealie-mcp                                        # drop-in, see below
systemctl enable --now mealie-mcp
```

Drop-in (`/etc/systemd/system/mealie-mcp.service.d/10-site.conf`):

```ini
[Service]
Environment=MCP_PUBLIC_URL=https://mealie-mcp.example.com
Environment=MCP_OAUTH_ISSUER=https://auth.example.com/application/o/mealie-mcp/
Environment=MCP_OAUTH_AUDIENCE=https://mealie-mcp.example.com/mcp
Environment=MEALIE_PUBLIC_URL=https://mealie.example.com
# Only if a tunnel/proxy on another host must reach the port; firewall it to that host.
Environment=MCP_HTTP_HOST=0.0.0.0
Environment=MCP_TRUST_FORWARDED_FOR=1
```

`StateDirectory=mealie-mcp` + `FASTMCP_HOME` in the unit are load-bearing: the OAuth proxy keeps
client registrations and refresh tokens there. Lose it and every connected client gets 401 on
`/token` until it re-registers.

## 4. Edge

Point `mealie-mcp.example.com` at the host's port (Cloudflare Tunnel, Caddy, nginx — anything that
terminates TLS and forwards plain HTTP with the `Host` header intact). Restrict the port to the
proxy/tunnel address with a host firewall; `MCP_TRUST_FORWARDED_FOR=1` is only safe then.

## 5. Verify without a browser

```bash
H=https://mealie-mcp.example.com
curl -s $H/.well-known/oauth-protected-resource/mcp | jq
curl -s $H/.well-known/oauth-authorization-server | jq .registration_endpoint
curl -si -X POST $H/mcp -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | grep -i www-auth
# DCR with a foreign redirect must be rejected:
curl -s -X POST $H/register -H 'Content-Type: application/json' \
  -d '{"client_name":"x","redirect_uris":["https://evil.example/steal"]}'
```

Then in claude.ai: Settings → Connectors → Add custom connector → `https://mealie-mcp.example.com/mcp`
→ log in at your IdP → consent.

## Upgrading

```bash
REF=<new sha> ./deploy/install.sh && systemctl restart mealie-mcp
```
