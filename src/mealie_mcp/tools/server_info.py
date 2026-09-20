"""What is actually running here — so a test run can state the version it
tested instead of inferring it from behaviour."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mealie_mcp import __version__
from mealie_mcp.app import mcp
from mealie_mcp.auth import SCOPE_READ, current_identity, get_auth_settings, requires_scope
from mealie_mcp.client import MealieError, get_client


def deployed_ref() -> str | None:
    """The git ref `deploy/install.sh` checked out, if this is a git deploy."""
    env = (os.environ.get("MEALIE_MCP_GIT_REF") or "").strip()
    if env:
        return env
    # Installed, the package sits under <base>/venv/lib/.../mealie_mcp, so the
    # walk passes <base> but never <base>/src — check both names.
    for parent in Path(__file__).resolve().parents:
        for ref in (parent / "GIT_REF", parent / "src" / "GIT_REF"):
            if ref.is_file():
                return ref.read_text().strip() or None
    return None


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def server_info() -> dict[str, Any]:
    """Version and identity of this MCP server and the Mealie behind it.

    Useful when reporting a bug or running a test protocol: say which build
    you tested rather than guessing from behaviour.
    """
    client = get_client()
    settings = get_auth_settings()
    identity = current_identity()
    out: dict[str, Any] = {
        "server": {
            "name": "mealie-mcp",
            "version": __version__,
            "git_ref": deployed_ref(),
            "auth_mode": settings.mode,
        },
        "caller": {"actor": identity.actor, "scopes": list(identity.scopes)},
    }
    try:
        about = client.about()
        me = client.whoami()
        out["mealie"] = {
            "version": about.get("version"),
            "url": client.public_url,
            "user": me.get("username"),
            "admin": bool(me.get("admin")),
        }
    except MealieError as exc:
        out["mealie"] = {"error": str(exc)}
    return out
