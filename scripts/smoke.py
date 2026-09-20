#!/usr/bin/env python3
"""End-to-end smoke test against a real Mealie, through the MCP layer.

Read tools run against real data; write tools run a create -> update -> plan ->
shop -> clean-up cycle on a throwaway recipe so nothing is left behind.
Run with MEALIE_URL and MEALIE_TOKEN set (e.g. under `phase run`). Prints no secrets.

    python3 scripts/smoke.py [--write] [--url https://example.com/recipe]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

os.environ.setdefault("MCP_AUTH_MODE", "none")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastmcp import Client  # noqa: E402

from mealie_mcp.app import mcp  # noqa: E402
from mealie_mcp.client import get_client  # noqa: E402
from mealie_mcp.tools import mealplans, organizers, recipes, shopping  # noqa: E402,F401

TEST_NAME = "ZZ mealie-mcp smoke test"


def show(label, res):
    data = res.data if hasattr(res, "data") and res.data is not None else res.structured_content
    text = json.dumps(data, ensure_ascii=False, default=str)
    limit = int(os.environ.get("SMOKE_WIDTH", "300"))
    print(f"✓ {label}: {text[:limit]}{'…' if len(text) > limit else ''}")
    return data


async def main(write: bool, url: str | None):
    raw = get_client()
    print("whoami:", raw.whoami()["username"], "admin=", raw.whoami()["admin"])
    async with Client(mcp) as c:
        tools = await c.list_tools()
        print(f"tools: {len(tools)}")
        cats = show("list_categories_and_tags", await c.call_tool("list_categories_and_tags", {}))
        found = show(
            "search_recipes", await c.call_tool("search_recipes", {"query": "kyckling", "limit": 3})
        )
        if cats["tags"]:
            # display name (with å/ä/ö if any) must filter, not fall through to "everything"
            by_tag = show(
                "search_recipes(tags=name)",
                await c.call_tool("search_recipes", {"tags": [cats["tags"][-1]], "limit": 1}),
            )
            unfiltered = await c.call_tool("search_recipes", {"limit": 1})
            assert by_tag["total"] <= unfiltered.data["total"], "tag filter did not apply"
            res = await c.call_tool(
                "search_recipes", {"tags": ["finns-inte-xyz"]}, raise_on_error=False
            )
            assert res.is_error and "Unknown tag" in res.content[0].text, res.content
            print("✓ search_recipes(unknown tag) -> error")
        if found["recipes"]:
            show(
                "get_recipe", await c.call_tool("get_recipe", {"slug": found["recipes"][0]["slug"]})
            )
        today = dt.date.today()
        show(
            "get_mealplan",
            await c.call_tool(
                "get_mealplan",
                {
                    "start_date": str(today - dt.timedelta(days=7)),
                    "end_date": str(today + dt.timedelta(days=7)),
                },
            ),
        )
        lists = show("list_shopping_lists", await c.call_tool("list_shopping_lists", {}))
        if lists["lists"]:
            show(
                "get_shopping_list",
                await c.call_tool(
                    "get_shopping_list", {"list_id_or_name": lists["lists"][0]["id"]}
                ),
            )
        if not write:
            return

        # -- write cycle -------------------------------------------------
        created = show(
            "create_recipe",
            await c.call_tool(
                "create_recipe",
                {
                    "name": TEST_NAME,
                    "ingredients": [
                        "# Sås",
                        "2 dl grädde",
                        "1 gul lök, hackad",
                        "# Övrigt",
                        "400 g kycklinglårfilé",
                        "salt och peppar",
                    ],
                    "instructions": [
                        "# Förbered",
                        "Hacka löken.",
                        "# Laga",
                        "Bryn kycklingen.",
                        "Häll på grädden och låt sjuda 10 min.",
                    ],
                    "description": "Skapad av smoke-testet, ska raderas.",
                    "servings": 4,
                    "total_time": "25 min",
                    "tags": [cats["tags"][0]] if cats["tags"] else [],
                    "categories": ["finns-inte-xyz"],
                },
            ),
        )
        slug = created["slug"]
        try:
            show(
                "update_recipe",
                await c.call_tool(
                    "update_recipe", {"slug": slug, "notes": ["Testanteckning"], "servings": 2}
                ),
            )
            entry = show(
                "add_mealplan_entry",
                await c.call_tool(
                    "add_mealplan_entry",
                    {
                        "date": str(today + dt.timedelta(days=30)),
                        "meal": "dinner",
                        "recipe_slug": slug,
                    },
                ),
            )
            show(
                "update_mealplan_entry",
                await c.call_tool(
                    "update_mealplan_entry",
                    {"entry_id": entry["id"], "meal": "lunch", "note": "flyttad"},
                ),
            )
            show(
                "remove_mealplan_entry",
                await c.call_tool("remove_mealplan_entry", {"entry_id": entry["id"]}),
            )
            if lists["lists"]:
                lid = lists["lists"][0]["id"]
                before = {i["id"] for i in raw.shopping_list(lid).get("listItems", [])}
                show(
                    "add_shopping_items",
                    await c.call_tool(
                        "add_shopping_items",
                        {"items": ["ZZ smoke-vara 1", "ZZ smoke-vara 2"], "list_id_or_name": lid},
                    ),
                )
                show(
                    "add_recipe_to_shopping_list",
                    await c.call_tool(
                        "add_recipe_to_shopping_list", {"recipe_slug": slug, "list_id_or_name": lid}
                    ),
                )
                after = raw.shopping_list(lid).get("listItems", [])
                new_ids = [i["id"] for i in after if i["id"] not in before]
                show(
                    "check_shopping_items",
                    await c.call_tool("check_shopping_items", {"item_ids": new_ids[:1]}),
                )
                res = await c.call_tool(
                    "check_shopping_items", {"item_ids": ["not-a-uuid"]}, raise_on_error=False
                )
                assert res.is_error and "not a shopping item id" in res.content[0].text
                print("✓ check_shopping_items(bad id) -> error")
                show(
                    "remove_shopping_items",
                    await c.call_tool("remove_shopping_items", {"item_ids": new_ids[1:]}),
                )
                remaining = {i["id"] for i in raw.shopping_list(lid).get("listItems", [])}
                assert not (set(new_ids[1:]) & remaining), "remove_shopping_items left items"
                # the one ticked item is what clear_checked removes; anything the user had
                # ticked before goes too, which is what that button means
                show(
                    "clear_checked_shopping_items",
                    await c.call_tool("clear_checked_shopping_items", {"list_id_or_name": lid}),
                )
                remaining = {i["id"] for i in raw.shopping_list(lid).get("listItems", [])}
                assert new_ids[0] not in remaining, "clear_checked left the ticked item"
            if url:
                imp = show(
                    "create_recipe_from_url",
                    await c.call_tool(
                        "create_recipe_from_url", {"url": url, "allow_duplicate": True}
                    ),
                )
                assert imp["created"] is True
                dup = show(
                    "create_recipe_from_url(again)",
                    await c.call_tool("create_recipe_from_url", {"url": url}),
                )
                assert dup["created"] is False and imp["slug"] in dup["duplicate_of"]
                show("delete_recipe", await c.call_tool("delete_recipe", {"slug": imp["slug"]}))
            res = await c.call_tool(
                "create_recipe_from_url",
                {"url": "https://www.ica.se/recept/finns-inte-000000/"},
                raise_on_error=False,
            )
            assert res.is_error and "No recipe found" in res.content[0].text, res.content
            print("✓ create_recipe_from_url(404 page) -> error, nothing saved")
            if found["recipes"] and found["recipes"][0]["slug"] != slug:
                res = await c.call_tool(
                    "delete_recipe", {"slug": found["recipes"][0]["slug"]}, raise_on_error=False
                )
                assert res.is_error and "not created through this server" in res.content[0].text
                print("✓ delete_recipe(someone else's recipe) -> refused")
        finally:
            show("delete_recipe", await c.call_tool("delete_recipe", {"slug": slug}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--url")
    a = ap.parse_args()
    asyncio.run(main(a.write, a.url))
