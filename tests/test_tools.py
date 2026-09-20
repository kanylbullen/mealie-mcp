import pytest

from mealie_mcp.client import MealieError
from mealie_mcp.tools import mealplans, organizers, recipes, shopping


def test_search_and_get(fake):
    res = recipes.search_recipes(query="gryta")
    assert res["total"] == 1 and res["recipes"][0]["slug"] == "testgryta"
    detail = recipes.get_recipe("testgryta")
    assert detail["ingredients"][0] == {"text": "2 dl grädde", "section": "Sås"}
    assert detail["instructions"] == [{"n": 1, "text": "Koka."}]
    assert detail["url"] == "http://mealie.test/r/testgryta"


def test_create_recipe_reports_unknown_tags_instead_of_creating(fake):
    out = recipes.create_recipe(
        name="Ny",
        ingredients=["# Sås", "1 dl mjölk"],
        instructions=["# Steg", "Rör."],
        tags=["vardag", "Påhittad"],
        parse_ingredients=False,
    )
    put = next(b for m, p, b in fake.calls if m == "PUT")
    assert put["tags"] == [{"id": "t1", "groupId": "g", "name": "Vardag", "slug": "vardag"}]
    assert (
        put["recipeIngredient"][0]["title"] == "Sås"
        and put["recipeIngredient"][0]["note"] == "1 dl mjölk"
    )
    assert put["recipeInstructions"] == [{"title": "Steg", "text": "Rör."}]
    assert out["skipped"]["categories_or_tags"] == ["Påhittad"]
    assert fake.created_tags == []


def test_create_missing_tags_when_asked(fake):
    recipes.create_recipe(
        name="Ny",
        ingredients=["x"],
        instructions=["y"],
        tags=["Påhittad"],
        parse_ingredients=False,
        create_missing_tags=True,
    )
    assert fake.created_tags == ["Påhittad"]


def test_create_recipe_validates(fake):
    with pytest.raises(ValueError):
        recipes.create_recipe(name="Ny", ingredients=[], instructions=["y"])


def test_mealplan_validation_and_add(fake):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        mealplans.get_mealplan("igår")
    with pytest.raises(ValueError, match="meal must be"):
        mealplans.add_mealplan_entry("2026-01-01", meal="brunch", title="x")
    with pytest.raises(ValueError, match="No recipe"):
        mealplans.add_mealplan_entry("2026-01-01", recipe_slug="missing")
    out = mealplans.add_mealplan_entry("2026-01-01", recipe_slug="testgryta")
    assert out["created"] and out["recipe"]["slug"] == "testgryta"
    plan = mealplans.get_mealplan("2026-01-01", "2026-01-07")
    assert plan["entries"][0]["meal"] == "dinner"


def test_shopping_defaults_to_only_list(fake):
    lst = shopping.get_shopping_list()
    assert lst["name"] == "Veckohandling" and lst["items"][0]["text"] == "mjölk"
    out = shopping.add_shopping_items(["ägg", " "])
    assert out["added"] == 1
    bulk = next(b for m, p, b in fake.calls if p.endswith("create-bulk"))
    assert bulk[0]["note"] == "ägg" and bulk[0]["shoppingListId"] == "l1"


def test_organizers(fake):
    assert organizers.list_categories_and_tags() == {"categories": ["Middag"], "tags": ["Vardag"]}


def test_mealie_error_message_is_compact(fake):
    with pytest.raises(MealieError, match="404"):
        recipes.get_recipe("missing")
