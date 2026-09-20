"""Meal-plan tools: read a date range, add / change / remove entries."""

from __future__ import annotations

import datetime as dt
from typing import Any

from mealie_mcp.app import mcp
from mealie_mcp.auth import SCOPE_READ, SCOPE_WRITE, requires_scope
from mealie_mcp.client import MealieError, get_client
from mealie_mcp.tools._shape import mealplan_entry

MEAL_TYPES = ("breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert")
_MAX_RANGE_DAYS = 62


def _date(value: str, name: str = "date") -> str:
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _meal(value: str) -> str:
    meal = (value or "").strip().lower()
    if meal not in MEAL_TYPES:
        raise ValueError(f"meal must be one of {MEAL_TYPES}, got {value!r}")
    return meal


def _recipe_id(slug: str) -> tuple[str, dict[str, Any]]:
    client = get_client()
    try:
        recipe = client.get_recipe(slug.strip())
    except MealieError as exc:
        if exc.status == 404:
            raise ValueError(f"No recipe with slug {slug!r}; use search_recipes first.") from exc
        raise
    return str(recipe["id"]), recipe


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def get_mealplan(start_date: str, end_date: str | None = None) -> dict[str, Any]:
    """List planned meals between two dates (inclusive, YYYY-MM-DD).

    Omit `end_date` for a single day. Each entry has an `id` (needed to change
    or remove it), the meal type, and either a linked recipe or free text.
    """
    client = get_client()
    start = _date(start_date, "start_date")
    end = _date(end_date, "end_date") if end_date else start
    if end < start:
        raise ValueError("end_date is before start_date")
    if (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > _MAX_RANGE_DAYS:
        raise ValueError(f"Range too long; ask for at most {_MAX_RANGE_DAYS} days at a time.")
    entries = client.mealplans(start, end)
    return {
        "start_date": start,
        "end_date": end,
        "entries": [mealplan_entry(e, client) for e in entries],
    }


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def add_mealplan_entry(
    date: str,
    meal: str = "dinner",
    recipe_slug: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Plan a meal on a date. Give a `recipe_slug`, or a free-text `title`
    ("Rester", "Äta ute") when there is no recipe. `meal` is one of
    breakfast, lunch, dinner, side, snack, drink, dessert.
    """
    client = get_client()
    entry: dict[str, Any] = {"date": _date(date), "entryType": _meal(meal)}
    if recipe_slug:
        entry["recipeId"], _ = _recipe_id(recipe_slug)
    elif title and title.strip():
        entry["title"] = title.strip()
    else:
        raise ValueError("Give either recipe_slug or title.")
    if note:
        entry["text"] = note.strip()
    created = client.create_mealplan_entry(entry)
    out = mealplan_entry(created, client)
    out["created"] = True
    return out


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def update_mealplan_entry(
    entry_id: int,
    date: str | None = None,
    meal: str | None = None,
    recipe_slug: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Move or change a planned meal (ids come from `get_mealplan`).

    Setting `recipe_slug` replaces any free-text title; setting `title`
    detaches the recipe.
    """
    client = get_client()
    current = client.get_mealplan_entry(int(entry_id))
    body: dict[str, Any] = {
        "id": current["id"],
        "groupId": current["groupId"],
        "userId": current["userId"],
        "date": current["date"],
        "entryType": current["entryType"],
        "title": current.get("title") or "",
        "text": current.get("text") or "",
        "recipeId": current.get("recipeId"),
    }
    if date is not None:
        body["date"] = _date(date)
    if meal is not None:
        body["entryType"] = _meal(meal)
    if recipe_slug:
        body["recipeId"], _ = _recipe_id(recipe_slug)
        body["title"] = ""
    elif title is not None:
        body["title"] = title.strip()
        if title.strip():
            body["recipeId"] = None
    if note is not None:
        body["text"] = note.strip()
    saved = client.update_mealplan_entry(int(entry_id), body)
    out = mealplan_entry(saved, client)
    out["updated"] = True
    return out


@mcp.tool(annotations={"destructiveHint": True})
@requires_scope(SCOPE_WRITE)
def remove_mealplan_entry(entry_id: int) -> dict[str, Any]:
    """Remove one planned meal by id. Only the plan entry goes; the recipe stays."""
    client = get_client()
    before = mealplan_entry(client.get_mealplan_entry(int(entry_id)), client)
    client.delete_mealplan_entry(int(entry_id))
    return {"removed": True, "entry": before}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def add_random_mealplan_entry(date: str, meal: str = "dinner") -> dict[str, Any]:
    """Let Mealie pick a recipe for a slot, using the household's meal-plan
    rules (e.g. only recipes tagged "vardag" on weekdays). This CREATES the
    entry; remove it with `remove_mealplan_entry` if the pick is bad.
    """
    client = get_client()
    created = client.random_mealplan_entry(_date(date), _meal(meal))
    out = mealplan_entry(created, client)
    out["created"] = True
    return out
