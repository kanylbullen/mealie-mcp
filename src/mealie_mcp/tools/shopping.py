"""Shopping-list tools: list, read, add a recipe's ingredients, add free items,
tick, remove, clear what is ticked."""

from __future__ import annotations

import uuid
from typing import Any

from mealie_mcp.app import mcp
from mealie_mcp.auth import SCOPE_READ, SCOPE_WRITE, requires_scope
from mealie_mcp.client import MealieError, get_client
from mealie_mcp.tools._shape import shopping_item


def _resolve_list(list_id_or_name: str | None) -> dict[str, Any]:
    """Accept a list id or a (case-insensitive) list name; default to the only
    list when the household has exactly one."""
    client = get_client()
    lists = client.shopping_lists()
    if not lists:
        raise ValueError("The household has no shopping lists yet; create one in Mealie first.")
    key = (list_id_or_name or "").strip()
    if not key:
        if len(lists) == 1:
            return lists[0]
        names = [lst.get("name") for lst in lists]
        raise ValueError(f"Several shopping lists exist; say which: {names}")
    for lst in lists:
        if lst.get("id") == key or str(lst.get("name", "")).casefold() == key.casefold():
            return lst
    raise ValueError(f"No shopping list {key!r}; known: {[lst.get('name') for lst in lists]}")


def _item_ids(item_ids: list[str]) -> list[str]:
    """Ids as Mealie will accept them, so a typo is a clear error here rather
    than a Pydantic uuid_parsing dump from Mealie."""
    ids: list[str] = []
    for raw in item_ids:
        key = (raw or "").strip()
        if not key:
            continue
        try:
            ids.append(str(uuid.UUID(key)))
        except ValueError as exc:
            raise ValueError(
                f"{key!r} is not a shopping item id; ids come from get_shopping_list."
            ) from exc
    if not ids:
        raise ValueError("item_ids is empty")
    return ids


def _get_item(item_id: str) -> dict[str, Any]:
    client = get_client()
    try:
        return client.shopping_item(item_id)
    except MealieError as exc:
        if exc.status == 404:
            raise ValueError(
                f"No shopping item {item_id!r} (already removed?); see get_shopping_list."
            ) from exc
        raise


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def list_shopping_lists() -> dict[str, Any]:
    """Shopping lists in the household with counts of open items."""
    client = get_client()
    out = []
    for lst in client.shopping_lists():
        items = lst.get("listItems") or []
        out.append(
            {
                "id": lst.get("id"),
                "name": lst.get("name"),
                "open_items": sum(1 for i in items if not i.get("checked")),
                "checked_items": sum(1 for i in items if i.get("checked")),
            }
        )
    return {"lists": out}


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def get_shopping_list(
    list_id_or_name: str | None = None, include_checked: bool = False
) -> dict[str, Any]:
    """Items on a shopping list (open items only unless `include_checked`).
    Omit the list when the household has just one."""
    client = get_client()
    lst = client.shopping_list(_resolve_list(list_id_or_name)["id"])
    items = [
        shopping_item(i)
        for i in lst.get("listItems") or []
        if include_checked or not i.get("checked")
    ]
    return {"id": lst.get("id"), "name": lst.get("name"), "items": items}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def add_recipe_to_shopping_list(
    recipe_slug: str, list_id_or_name: str | None = None, scale: float = 1.0
) -> dict[str, Any]:
    """Put a recipe's ingredients on a shopping list. `scale` multiplies
    quantities (2 = double batch). Mealie merges same foods into one line."""
    client = get_client()
    lst = _resolve_list(list_id_or_name)
    try:
        recipe = client.get_recipe(recipe_slug.strip())
    except MealieError as exc:
        if exc.status == 404:
            raise ValueError(f"No recipe with slug {recipe_slug!r}") from exc
        raise
    if scale <= 0:
        raise ValueError("scale must be > 0")
    client.add_recipe_to_shopping_list(lst["id"], str(recipe["id"]), scale=float(scale))
    after = client.shopping_list(lst["id"])
    return {
        "added": True,
        "recipe": {"slug": recipe.get("slug"), "name": recipe.get("name")},
        "list": {"id": lst["id"], "name": lst.get("name")},
        "open_items": sum(1 for i in after.get("listItems") or [] if not i.get("checked")),
    }


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def add_shopping_items(items: list[str], list_id_or_name: str | None = None) -> dict[str, Any]:
    """Add free-text items to a shopping list ("mjölk", "2 kg potatis")."""
    client = get_client()
    lst = _resolve_list(list_id_or_name)
    lines = [i.strip() for i in items if i and i.strip()]
    if not lines:
        raise ValueError("items is empty")
    if len(lines) > 100:
        raise ValueError("At most 100 items per call.")
    existing = len(lst.get("listItems") or [])
    payload = [
        {
            "shoppingListId": lst["id"],
            "note": ln,
            "quantity": 0,
            "position": existing + n,
            "checked": False,
        }
        for n, ln in enumerate(lines)
    ]
    client.create_shopping_items(payload)
    return {"added": len(lines), "list": {"id": lst["id"], "name": lst.get("name")}, "items": lines}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def check_shopping_items(item_ids: list[str], checked: bool = True) -> dict[str, Any]:
    """Tick (or untick) items by id — ids come from `get_shopping_list`.
    Ticked items stay on the list (greyed out); `remove_shopping_items` or
    `clear_checked_shopping_items` take them off."""
    client = get_client()
    done: list[str] = []
    for item_id in _item_ids(item_ids):
        # PUT needs the whole item; fetch it first to keep fields intact.
        item = _get_item(item_id)
        item["checked"] = bool(checked)
        client.update_shopping_item(item_id, item)
        done.append(item_id)
    return {"updated": done, "checked": bool(checked)}


@mcp.tool(annotations={"destructiveHint": True})
@requires_scope(SCOPE_WRITE)
def remove_shopping_items(item_ids: list[str]) -> dict[str, Any]:
    """Take items off a shopping list for good (ids from `get_shopping_list`).
    Use `check_shopping_items` instead when the item was bought."""
    client = get_client()
    ids = _item_ids(item_ids)
    removed = [shopping_item(_get_item(i)) for i in ids]
    client.delete_shopping_items(ids)
    return {"removed": removed}


@mcp.tool(annotations={"destructiveHint": True})
@requires_scope(SCOPE_WRITE)
def clear_checked_shopping_items(list_id_or_name: str | None = None) -> dict[str, Any]:
    """Remove every ticked item from a shopping list (what Mealie's own
    "delete checked" button does). Open items are untouched."""
    client = get_client()
    lst = client.shopping_list(_resolve_list(list_id_or_name)["id"])
    ticked = [i for i in lst.get("listItems") or [] if i.get("checked")]
    client.delete_shopping_items([str(i["id"]) for i in ticked])
    return {
        "list": {"id": lst.get("id"), "name": lst.get("name")},
        "removed": len(ticked),
        "open_items": sum(1 for i in lst.get("listItems") or [] if not i.get("checked")),
    }
