"""Organizer tools: the household's categories and tags, so a model reuses
existing names instead of inventing near-duplicates."""

from __future__ import annotations

from typing import Any

from mealie_mcp.app import mcp
from mealie_mcp.auth import SCOPE_READ, requires_scope
from mealie_mcp.client import get_client


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def list_categories_and_tags() -> dict[str, Any]:
    """All recipe categories and tags in use. These names work as-is in
    `search_recipes` filters and in `create_recipe` / `update_recipe`; only
    create new ones when nothing fits."""
    client = get_client()
    return {
        "categories": sorted(c.get("name", "") for c in client.categories()),
        "tags": sorted(t.get("name", "") for t in client.tags()),
    }
