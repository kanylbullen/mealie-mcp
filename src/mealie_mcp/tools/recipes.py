"""Recipe tools: search, read, import from URL, create from text, update, delete own.

`delete_recipe` only removes recipes this server's own Mealie user created —
its imports and text recipes, i.e. what a model can undo of its own doing.
Everything else stays deletable only in the web UI: a confirm gate would not
stop a model that is convinced it should proceed, it would just make it type
the name.
"""

from __future__ import annotations

import difflib
import os
from typing import Any

from mealie_mcp.app import mcp
from mealie_mcp.auth import SCOPE_READ, SCOPE_WRITE, requires_scope
from mealie_mcp.client import MealieClient, MealieError, get_client
from mealie_mcp.tools._shape import is_stub, recipe_detail, recipe_summary

_MAX_PER_PAGE = 50
_SKIPPED_HINT = "Not found; pick from list_categories_and_tags or pass create_missing_tags=true."
_LOW_CONFIDENCE = 0.7
_UNNAMED_STUB = "no recipe name found"


# -- organizer / food / unit resolution --------------------------------------


def require_recipe(client: MealieClient, slug: str) -> dict[str, Any]:
    """A recipe by slug, or a ValueError that names the slug and the way forward
    (instead of Mealie's raw `GET /api/recipes/x -> 404`)."""
    slug = slug.strip()
    try:
        return client.get_recipe(slug)
    except MealieError as exc:
        if exc.status == 404:
            raise ValueError(f"No recipe with slug {slug!r}; use search_recipes first.") from exc
        raise


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(i.get("name", "")).strip().casefold(): i for i in items}


def resolve_organizers(
    client: MealieClient,
    names: list[str] | None,
    kind: str,
    *,
    create_missing: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map category/tag names onto Mealie organizer objects.

    Returns (resolved, skipped). Matching is case-insensitive on the name;
    unknown names are created only when `create_missing` is set, otherwise
    reported back so the caller can pick an existing one instead of letting
    a model grow the taxonomy one typo at a time.
    """
    if not names:
        return [], []
    existing = _by_name(client.categories() if kind == "category" else client.tags())
    resolved: list[dict[str, Any]] = []
    skipped: list[str] = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        hit = existing.get(name.casefold())
        if hit is None and create_missing:
            hit = client.create_category(name) if kind == "category" else client.create_tag(name)
            existing[name.casefold()] = hit
        if hit is None:
            skipped.append(name)
        else:
            # groupId is required: without it Mealie's PUT fails with a misleading
            # "Recipe already exists" (its catch-all for IntegrityError).
            resolved.append(
                {
                    "id": hit["id"],
                    "groupId": hit.get("groupId"),
                    "name": hit["name"],
                    "slug": hit.get("slug"),
                }
            )
    return resolved, skipped


def resolve_filter_slugs(client: MealieClient, names: list[str] | None, kind: str) -> list[str]:
    """Category/tag names (or slugs) -> slugs for the recipe search filter.

    Mealie filters on slug, and its slugs strip diacritics ("Viktväktarna" ->
    "viktvaktarna"), so the display names `list_categories_and_tags` returns
    would silently match nothing — Mealie then returns the whole library, which
    looks like a successful search. Unknown names are an error instead.
    """
    if not names:
        return []
    items = client.categories() if kind == "category" else client.tags()
    lookup: dict[str, str] = {}
    for it in items:
        slug = str(it.get("slug") or "")
        if not slug:
            continue
        lookup[str(it.get("name", "")).strip().casefold()] = slug
        lookup[slug.casefold()] = slug
    slugs: list[str] = []
    unknown: list[str] = []
    for raw in names:
        key = raw.strip().casefold()
        if not key:
            continue
        if key in lookup:
            slugs.append(lookup[key])
        else:
            unknown.append(raw.strip())
    if unknown:
        known = sorted(str(it.get("name", "")) for it in items)
        close: list[str] = []
        for u in unknown:
            close += [
                n for n in difflib.get_close_matches(u, known, n=3, cutoff=0.5) if n not in close
            ]
        hint = f"did you mean {close}?" if close else "no similar name"
        raise ValueError(
            f"Unknown {kind}{'s' if len(unknown) > 1 else ''} {unknown} — {hint} "
            f"({len(known)} {kind} names exist; see list_categories_and_tags)"
        )
    return slugs


def _parser_name(client: MealieClient) -> str:
    configured = (os.environ.get("MEALIE_INGREDIENT_PARSER") or "auto").strip().lower()
    if configured != "auto":
        return configured
    try:
        return "openai" if client.about().get("enableOpenai") else "nlp"
    except MealieError:
        return "nlp"


def _ensure_named(client: MealieClient, path: str, name: str, cache: dict[str, Any]) -> Any:
    """Return the id of a food/unit called `name`, creating it if needed."""
    key = name.strip().casefold()
    if key in cache:
        return cache[key]
    if not cache:
        for item in client.get(path, perPage=-1).get("items", []):
            cache[str(item.get("name", "")).strip().casefold()] = item["id"]
            for alias in item.get("aliases") or []:
                cache[str(alias.get("name", "")).strip().casefold()] = item["id"]
        if key in cache:
            return cache[key]
    created = client.request("POST", path, json={"name": name.strip()})
    cache[key] = created["id"]
    return created["id"]


def build_ingredients(
    client: MealieClient, lines: list[str], *, parse: bool
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn free-text ingredient lines into Mealie ingredient objects.

    Lines starting with `#` become section headers (Mealie's `title`). With
    `parse`, Mealie's own parser splits quantity / unit / food so the recipe
    can feed shopping lists properly; foods and units it names are created if
    missing. If parsing fails, the lines are stored verbatim and a warning
    says so — a recipe with unparsed ingredients is still a usable recipe.
    """
    cleaned = [ln.strip() for ln in lines if ln and ln.strip()]
    headers: dict[int, str] = {}
    body: list[str] = []
    for ln in cleaned:
        if ln.startswith("#"):
            headers[len(body)] = ln.lstrip("#").strip()
        else:
            body.append(ln)

    def verbatim() -> list[dict[str, Any]]:
        out = []
        for i, ln in enumerate(body):
            item: dict[str, Any] = {"note": ln, "originalText": ln}
            if i in headers:
                item["title"] = headers[i]
            out.append(item)
        return out

    if not parse or not body:
        return verbatim(), None

    try:
        parsed = client.parse_ingredients(body, parser=_parser_name(client))
    except MealieError as exc:
        return verbatim(), f"Ingredient parser failed ({exc}); lines stored as written."

    if len(parsed) != len(body):
        return verbatim(), (
            f"Ingredient parser returned {len(parsed)} items for {len(body)} lines; "
            "lines stored as written."
        )

    foods: dict[str, Any] = {}
    units: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    unsure: list[str] = []
    invented: list[str] = []
    try:
        for i, (line, p) in enumerate(zip(body, parsed, strict=True)):
            ing = dict(p.get("ingredient") or {})
            item: dict[str, Any] = {
                "quantity": ing.get("quantity") or 0,
                "note": _note_from_line(line, ing.get("note")),
                "originalText": line,
            }
            avg = (p.get("confidence") or {}).get("average")
            if isinstance(avg, (int, float)) and avg < _LOW_CONFIDENCE:
                unsure.append(line)
            food = ing.get("food") or {}
            unit = ing.get("unit") or {}
            if isinstance(food, dict) and food.get("name"):
                if food.get("id") or _written(food["name"], line):
                    item["food"] = {
                        "id": food.get("id")
                        or _ensure_named(client, "/api/foods", food["name"], foods),
                        "name": food["name"],
                    }
                else:
                    # A food Mealie does not know and the cook did not write (the LLM
                    # parser translating "citron" to "lemon") must not enter the food
                    # database under that name. Keep the line whole instead.
                    invented.append(f"{line!r} (parser said {food['name']!r})")
                    item["note"] = line
                    item["quantity"] = 0
                    unit = {}
            if isinstance(unit, dict) and unit.get("name"):
                item["unit"] = {
                    "id": unit.get("id")
                    or _ensure_named(client, "/api/units", unit["name"], units),
                    "name": unit["name"],
                }
            if "food" not in item and not item["note"]:
                item["note"] = line
            if i in headers:
                item["title"] = headers[i]
            out.append(item)
    except MealieError as exc:
        return verbatim(), f"Could not create foods/units ({exc}); lines stored as written."
    notes: list[str] = []
    if invented:
        notes.append(
            "Stored as plain text because the parser named a food that is not in the line "
            "and not in Mealie's food list: " + "; ".join(invented) + "."
        )
    if unsure:
        notes.append(
            "Ingredient parser was unsure about: "
            + "; ".join(repr(u) for u in unsure)
            + ". Check them in the result and fix with update_recipe if wrong."
        )
    return out, (" ".join(notes) or None)


def _written(name: str, line: str) -> bool:
    """Did the cook write this food? Some word of the parsed name must occur in
    the line: "knippa dill" for "1 knippe dill" passes on "dill", while "lemon"
    for "citron" fails."""
    words = [w for w in name.casefold().split() if len(w) > 2]
    return any(w in line.casefold() for w in words)


def _note_from_line(line: str, parsed_note: Any) -> str:
    """The preparation note ("hackad", "saften") must come from the cook's own
    line. Mealie's LLM parser sometimes embellishes it — "saften-the juice" —
    and that text ends up on the shopping list. After the first comma is what
    the cook wrote; otherwise keep the parser's note only if it is really there."""
    if "," in line:
        return line.split(",", 1)[1].strip()
    note = str(parsed_note or "").strip()
    return note if note and note.casefold() in line.casefold() else ""


def build_instructions(steps: list[str]) -> list[dict[str, Any]]:
    """Steps as Mealie instruction objects; `#` lines become section titles."""
    out: list[dict[str, Any]] = []
    pending_title = ""
    for raw in steps:
        text = (raw or "").strip()
        if not text:
            continue
        if text.startswith("#"):
            pending_title = text.lstrip("#").strip()
            continue
        out.append({"title": pending_title, "text": text})
        pending_title = ""
    return out


# -- tools -------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def search_recipes(
    query: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    require_all_tags: bool = False,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Search recipes by free text and/or category / tag names.

    Returns compact summaries (slug, name, categories, tags, servings, time).
    Use `get_recipe` with a slug for ingredients and instructions. Category and
    tag names are matched case-insensitively against `list_categories_and_tags`;
    an unknown name is an error (not an unfiltered result).
    """
    client = get_client()
    limit = max(1, min(int(limit), _MAX_PER_PAGE))
    res = client.search_recipes(
        search=query,
        categories=resolve_filter_slugs(client, categories, "category"),
        tags=resolve_filter_slugs(client, tags, "tag"),
        require_all_tags=require_all_tags,
        per_page=limit,
        page=max(1, int(page)),
    )
    return {
        "total": res.get("total"),
        "page": res.get("page"),
        "pages": res.get("total_pages"),
        "recipes": [recipe_summary(r, client) for r in res.get("items", [])],
    }


@mcp.tool(annotations={"readOnlyHint": True})
@requires_scope(SCOPE_READ)
def get_recipe(slug: str) -> dict[str, Any]:
    """Read one recipe in full: ingredients, instructions, notes, nutrition, source URL."""
    client = get_client()
    return recipe_detail(require_recipe(client, slug), client)


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def create_recipe_from_url(
    url: str, import_tags: bool = False, allow_duplicate: bool = False
) -> dict[str, Any]:
    """Import a recipe from a web page URL using Mealie's scraper.

    Mealie fetches the page itself (schema.org data, then an LLM pass if
    configured). Paywalled or JavaScript-only pages yield a *stub* recipe with
    "Could not detect ingredients" — the result flags that with a warning so
    you can fix it with `update_recipe` or ask the user for the text instead.
    A page with no recipe at all (404, wrong link) is not kept.

    If the library already has a recipe from this URL it is returned instead
    (`created: false`, `duplicate_of`); pass `allow_duplicate` to import anyway.
    """
    client = get_client()
    url = url.strip()
    if not url:
        raise ValueError("url is required")
    if not allow_duplicate:
        existing = client.recipes_by_source_url(url)
        if existing:
            out = recipe_detail(client.get_recipe(existing[0]["slug"]), client)
            out["created"] = False
            out["duplicate_of"] = [r["slug"] for r in existing]
            out["hint"] = "Already imported from this URL; pass allow_duplicate=true to re-import."
            return out
    slug = client.create_recipe_from_url(url, include_tags=import_tags)
    recipe = client.get_recipe(slug)
    if is_stub(recipe) and str(recipe.get("name", "")).casefold().startswith(_UNNAMED_STUB):
        client.delete_recipe(slug)
        raise ValueError(
            f"No recipe found at {url}: the page has no name, ingredients or instructions "
            "(probably 404 or not a recipe page). Nothing was saved."
        )
    out = recipe_detail(recipe, client)
    out["created"] = True
    return out


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def create_recipe(
    name: str,
    ingredients: list[str],
    instructions: list[str],
    description: str | None = None,
    servings: float | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    source_url: str | None = None,
    parse_ingredients: bool = True,
    create_missing_tags: bool = False,
) -> dict[str, Any]:
    """Create a recipe from text (dictated, pasted, or written by you).

    `ingredients`: one line per ingredient as a cook would write it
    ("2 dl grädde", "1 gul lök, hackad"). A line starting with `#` starts a
    section ("# Sås"). With `parse_ingredients` (default) Mealie splits each
    line into quantity / unit / food so shopping lists work; foods and units it
    names are created if missing.
    `instructions`: one step per line; `#` lines are section titles.
    Times are free text ("20 min"). Categories/tags must already exist unless
    `create_missing_tags` is set; unknown ones are reported in `skipped`.
    """
    client = get_client()
    if not name or not name.strip():
        raise ValueError("name is required")
    if not ingredients or not instructions:
        raise ValueError("ingredients and instructions must both be non-empty")

    slug = client.create_recipe(name.strip())
    recipe = client.get_recipe(slug)
    warnings: list[str] = []

    ing_objs, warn = build_ingredients(client, ingredients, parse=parse_ingredients)
    if warn:
        warnings.append(warn)
    cats, skipped_c = resolve_organizers(
        client, categories, "category", create_missing=create_missing_tags
    )
    tgs, skipped_t = resolve_organizers(client, tags, "tag", create_missing=create_missing_tags)

    recipe.update(
        {
            "description": (description or "").strip(),
            "recipeIngredient": ing_objs,
            "recipeInstructions": build_instructions(instructions),
            "recipeCategory": cats,
            "tags": tgs,
            "orgURL": (source_url or "").strip() or None,
        }
    )
    if servings is not None:
        recipe["recipeServings"] = float(servings)
        recipe["recipeYieldQuantity"] = float(servings)
    if prep_time is not None:
        recipe["prepTime"] = prep_time
    if cook_time is not None:
        recipe["performTime"] = cook_time
    if total_time is not None:
        recipe["totalTime"] = total_time

    saved = client.update_recipe(slug, recipe)
    out = recipe_detail(saved, client)
    out["created"] = True
    skipped = skipped_c + skipped_t
    if skipped:
        out["skipped"] = {
            "categories_or_tags": skipped,
            "hint": _SKIPPED_HINT,
        }
    if warnings:
        out["warnings"] = warnings
    return out


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def update_recipe(
    slug: str,
    name: str | None = None,
    description: str | None = None,
    servings: float | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    notes: list[str] | None = None,
    source_url: str | None = None,
    parse_ingredients: bool = True,
    create_missing_tags: bool = False,
) -> dict[str, Any]:
    """Update parts of an existing recipe. Only the fields you pass change.

    `ingredients`, `instructions`, `categories`, `tags` and `notes` REPLACE the
    existing list when given (pass the full new list; read the recipe first).
    Formats are the same as in `create_recipe`.
    """
    client = get_client()
    slug = slug.strip()
    recipe = require_recipe(client, slug)
    warnings: list[str] = []
    skipped: list[str] = []

    if name is not None and name.strip():
        recipe["name"] = name.strip()
    if description is not None:
        recipe["description"] = description
    if servings is not None:
        recipe["recipeServings"] = float(servings)
        recipe["recipeYieldQuantity"] = float(servings)
    if prep_time is not None:
        recipe["prepTime"] = prep_time
    if cook_time is not None:
        recipe["performTime"] = cook_time
    if total_time is not None:
        recipe["totalTime"] = total_time
    if source_url is not None:
        recipe["orgURL"] = source_url.strip() or None
    if categories is not None:
        cats, sk = resolve_organizers(
            client, categories, "category", create_missing=create_missing_tags
        )
        recipe["recipeCategory"] = cats
        skipped += sk
    if tags is not None:
        tgs, sk = resolve_organizers(client, tags, "tag", create_missing=create_missing_tags)
        recipe["tags"] = tgs
        skipped += sk
    if ingredients is not None:
        objs, warn = build_ingredients(client, ingredients, parse=parse_ingredients)
        recipe["recipeIngredient"] = objs
        if warn:
            warnings.append(warn)
    if instructions is not None:
        recipe["recipeInstructions"] = build_instructions(instructions)
    if notes is not None:
        recipe["notes"] = [{"title": "", "text": n} for n in notes if n and n.strip()]

    saved = client.update_recipe(slug, recipe)
    out = recipe_detail(saved, client)
    out["updated"] = True
    if skipped:
        out["skipped"] = {
            "categories_or_tags": skipped,
            "hint": _SKIPPED_HINT,
        }
    if warnings:
        out["warnings"] = warnings
    if is_stub(saved):
        out.setdefault("warnings", []).append("Recipe still contains import-stub placeholders.")
    return out


@mcp.tool(annotations={"destructiveHint": True})
@requires_scope(SCOPE_WRITE)
def delete_recipe(slug: str) -> dict[str, Any]:
    """Delete a recipe that was created through this server (an import or a
    text recipe you made). Recipes other household members created cannot be
    deleted here — that is done in the Mealie web UI."""
    client = get_client()
    slug = slug.strip()
    recipe = require_recipe(client, slug)
    if str(recipe.get("userId") or "") != client.user_id:
        raise ValueError(
            f"Recipe {slug!r} was not created through this server; delete it in the Mealie UI."
        )
    client.delete_recipe(slug)
    return {"deleted": True, "slug": slug, "name": recipe.get("name")}
