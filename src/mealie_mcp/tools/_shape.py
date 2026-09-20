"""Turn Mealie's verbose payloads into what a model needs to read.

Mealie's full recipe JSON is several kilobytes of ids, timestamps and nested
food/unit objects. The tools return the human-relevant subset; the slug is
always included so the model can ask for more or refer back.
"""

from __future__ import annotations

from typing import Any

from mealie_mcp.client import MealieClient

STUB_MARKERS = ("Could not detect ingredients", "Could not detect instructions")


def ingredient_text(ing: dict[str, Any]) -> str:
    """One ingredient as a single line, preferring Mealie's own rendering."""
    display = (ing.get("display") or "").strip()
    if display:
        return display
    note = (ing.get("note") or "").strip()
    qty = ing.get("quantity")
    unit = (ing.get("unit") or {}) if isinstance(ing.get("unit"), dict) else {}
    food = (ing.get("food") or {}) if isinstance(ing.get("food"), dict) else {}
    parts = []
    if qty:
        parts.append(f"{qty:g}" if isinstance(qty, (int, float)) else str(qty))
    if unit.get("name"):
        parts.append(str(unit["name"]))
    if food.get("name"):
        parts.append(str(food["name"]))
    if note:
        parts.append(note)
    return " ".join(parts) or (ing.get("originalText") or "")


def recipe_summary(r: dict[str, Any], client: MealieClient) -> dict[str, Any]:
    return {
        "slug": r.get("slug"),
        "name": r.get("name"),
        "description": (r.get("description") or "")[:200] or None,
        "categories": [c.get("name") for c in r.get("recipeCategory") or []],
        "tags": [t.get("name") for t in r.get("tags") or []],
        "servings": r.get("recipeServings") or None,
        "total_time": r.get("totalTime") or None,
        "rating": r.get("rating"),
        "url": client.recipe_url(r["slug"]) if r.get("slug") else None,
    }


def recipe_detail(r: dict[str, Any], client: MealieClient) -> dict[str, Any]:
    ingredients: list[dict[str, Any]] = []
    for ing in r.get("recipeIngredient") or []:
        line: dict[str, Any] = {"text": ingredient_text(ing)}
        if ing.get("title"):
            line["section"] = ing["title"]
        ingredients.append(line)
    steps: list[dict[str, Any]] = []
    for i, st in enumerate(r.get("recipeInstructions") or [], start=1):
        step: dict[str, Any] = {"n": i, "text": st.get("text") or ""}
        if st.get("title"):
            step["section"] = st["title"]
        steps.append(step)
    nutrition = {k: v for k, v in (r.get("nutrition") or {}).items() if v}
    out = recipe_summary(r, client)
    out.update(
        {
            "description": r.get("description") or None,
            "yield": r.get("recipeYield") or None,
            "prep_time": r.get("prepTime") or None,
            "cook_time": r.get("performTime") or None,
            "source_url": r.get("orgURL") or None,
            "ingredients": ingredients,
            "instructions": steps,
            "notes": [
                {"title": n.get("title"), "text": n.get("text")} for n in r.get("notes") or []
            ]
            or None,
            "nutrition": nutrition or None,
            "last_made": r.get("lastMade"),
        }
    )
    if is_stub(r):
        out["warning"] = (
            "This recipe is an import stub: Mealie could not extract ingredients/"
            "instructions from the source page (no schema.org data, paywall, or "
            "JavaScript-only content). Fix it with update_recipe or re-import."
        )
    return {k: v for k, v in out.items() if v is not None}


def is_stub(r: dict[str, Any]) -> bool:
    texts = [ingredient_text(i) for i in r.get("recipeIngredient") or []]
    texts += [s.get("text") or "" for s in r.get("recipeInstructions") or []]
    return any(m in t for t in texts for m in STUB_MARKERS)


def mealplan_entry(e: dict[str, Any], client: MealieClient) -> dict[str, Any]:
    recipe = e.get("recipe") or {}
    out: dict[str, Any] = {
        "id": e.get("id"),
        "date": e.get("date"),
        "meal": e.get("entryType"),
    }
    if recipe:
        out["recipe"] = {
            "slug": recipe.get("slug"),
            "name": recipe.get("name"),
            "url": client.recipe_url(recipe["slug"]) if recipe.get("slug") else None,
        }
    if e.get("title"):
        out["title"] = e["title"]
    if e.get("text"):
        out["text"] = e["text"]
    return out


def shopping_item(it: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": it.get("id"),
        "text": (it.get("display") or "").strip() or ingredient_text(it),
        "checked": bool(it.get("checked")),
    }
    label = it.get("label") or {}
    if isinstance(label, dict) and label.get("name"):
        out["label"] = label["name"]
    refs = it.get("recipeReferences") or []
    if refs:
        out["from_recipes"] = len(refs)
    return out
