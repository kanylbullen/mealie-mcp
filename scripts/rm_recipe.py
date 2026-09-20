#!/usr/bin/env python3
"""Delete a recipe by slug (there is deliberately no MCP tool for this). Usage: rm_recipe.py <slug>"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from mealie_mcp.client import get_client  # noqa: E402

slug = sys.argv[1]
r = get_client().get_recipe(slug)
get_client().request("DELETE", f"/api/recipes/{slug}")
print(f"deleted {r['name']!r} ({slug})")
