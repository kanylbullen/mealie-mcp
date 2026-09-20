# mealie-mcp

An [MCP](https://modelcontextprotocol.io) server for [Mealie](https://mealie.io): search and read
recipes, import them from URLs or plain text, plan meals, and manage shopping lists — from
claude.ai, Claude Desktop, Claude Code or any other MCP client.

Runs as a single long-lived **Streamable HTTP** server with **OAuth 2.1** in front (any OIDC IdP;
built and tested against Authentik), or over stdio for local use.

## Tools (16)

| Area | Tools |
|---|---|
| Recipes | `search_recipes`, `get_recipe`, `create_recipe_from_url`, `create_recipe`, `update_recipe` |
| Meal plan | `get_mealplan`, `add_mealplan_entry`, `update_mealplan_entry`, `remove_mealplan_entry`, `add_random_mealplan_entry` |
| Shopping | `list_shopping_lists`, `get_shopping_list`, `add_recipe_to_shopping_list`, `add_shopping_items`, `check_shopping_items` |
| Organizers | `list_categories_and_tags` |

Deliberately **not** included: deleting recipes, bulk imports, anything under `/api/admin`,
backups, user management. Deleting is a rare, deliberate act that belongs in the web UI.

Two scopes: `mealie:read` and `mealie:write` (write implies read). Scopes are checked inside
every tool function, not just declared in metadata.

## How it fits together

```
MCP client ──OAuth 2.1──► mealie-mcp ──Mealie API token──► Mealie
   │ login + consent           │
   ▼                           │ RFC 9728 metadata, WWW-Authenticate challenge,
 your IdP (Authentik, …)       │ optional RFC 7591 DCR front (OAuthProxy)
```

Two auth boundaries. OAuth gates *client → server*. The *server → Mealie* hop always uses one
Mealie API token, ideally for a **dedicated, non-admin Mealie user** — everything the server does
shows up in Mealie as that user. `scripts/setup_mealie_user.py` creates that user and token
without ever printing them.

## Quick start (stdio, local)

```bash
pip install .
export MEALIE_URL=http://127.0.0.1:9000 MEALIE_TOKEN=<api token>
mealie-mcp            # speaks MCP over stdio
```

## Remote with OAuth

See [docs/DEPLOY.md](docs/DEPLOY.md). In short:

```bash
MCP_TRANSPORT=http MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=3011
MCP_PUBLIC_URL=https://mealie-mcp.example.com
MCP_AUTH_MODE=oauth-proxy
MCP_OAUTH_ISSUER=https://auth.example.com/application/o/mealie-mcp/
MCP_OAUTH_AUDIENCE=https://mealie-mcp.example.com/mcp
MCP_OAUTH_CLIENT_ID=… MCP_OAUTH_CLIENT_SECRET=…
MEALIE_URL=http://127.0.0.1:9000 MEALIE_TOKEN=…
```

The HTTP transport **refuses to start without OAuth** unless you set
`MCP_ALLOW_UNAUTHENTICATED_HTTP=1` for loopback testing.

## Configuration

| Variable | Meaning |
|---|---|
| `MEALIE_URL` / `MEALIE_TOKEN` | Mealie base URL and API token (required) |
| `MEALIE_PUBLIC_URL` | URL for links in results, if different from `MEALIE_URL` |
| `MEALIE_INGREDIENT_PARSER` | `auto` (default: `openai` if Mealie has it, else `nlp`), `nlp`, `openai`, `brute` |
| `MCP_TRANSPORT` | `stdio` (default) or `http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` / `MCP_HTTP_PATH` | bind address (default `127.0.0.1:8000/mcp`) |
| `MCP_PUBLIC_URL` | externally reachable base URL (required for OAuth) |
| `MCP_AUTH_MODE` | `none`, `oauth` (resource server only), `oauth-proxy` (adds DCR front) |
| `MCP_OAUTH_ISSUER` / `MCP_OAUTH_AUDIENCE` / `MCP_OAUTH_JWKS_URI` | token verification |
| `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` | this server's client at the IdP (proxy mode) |
| `MCP_OAUTH_CLIENT_REDIRECT_URIS` | allow-list for MCP clients' redirect URIs (default: claude.ai/claude.com callbacks + localhost) |
| `MCP_RATELIMIT_PER_MINUTE` / `MCP_TRUST_FORWARDED_FOR` | per-IP limit on `/register`, `/token`, `/authorize` |
| `FASTMCP_HOME` | where the OAuth proxy persists client registrations — must survive restarts |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## License

MIT. The auth and rate-limit modules are adapted from
[molnkontakt/odoo-mcp](https://github.com/molnkontakt/odoo-mcp) by the same author.
