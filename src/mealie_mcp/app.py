"""FastMCP app instance.

Separate from `server.py` so tool modules can import it without an import
cycle. The auth provider is attached at construction; with MCP_AUTH_MODE=none
`build_auth_provider()` returns None and the server runs unauthenticated
(stdio / loopback testing only — `server.py` refuses HTTP in that state).
"""

from fastmcp import FastMCP

from mealie_mcp.auth import build_auth_provider

mcp = FastMCP("mealie-mcp", auth=build_auth_provider())
