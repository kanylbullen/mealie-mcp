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
    assert organizers.list_categories_and_tags() == {
        "categories": ["Middag"],
        "tags": ["Vardag", "Viktväktarna"],
    }


def test_mealie_error_message_is_compact(fake):
    with pytest.raises(MealieError, match="404"):
        recipes.get_recipe("missing")


def test_search_resolves_tag_display_names_to_slugs(fake):
    recipes.search_recipes(tags=["Viktväktarna", "VARDAG"])
    params = next(r for r in fake.requests if r.url.path == "/api/recipes")
    assert params.url.params.get_list("tags") == ["viktvaktarna", "vardag"]


def test_search_unknown_tag_is_an_error_not_an_unfiltered_result(fake):
    with pytest.raises(ValueError, match=r"Unknown tag \['Fisk'\]; existing tag names"):
        recipes.search_recipes(tags=["Fisk"])
    assert not any(r.url.path == "/api/recipes" for r in fake.requests)


def test_import_returns_existing_recipe_for_known_url(fake):
    fake.by_url = [{"slug": "testgryta", "orgURL": "https://x.se/r/"}]
    out = recipes.create_recipe_from_url("https://x.se/r")
    assert out["created"] is False and out["duplicate_of"] == ["testgryta"]
    assert not any(r.url.path == "/api/recipes/create/url" for r in fake.requests)
    out = recipes.create_recipe_from_url("https://x.se/r", allow_duplicate=True)
    assert out["created"] is True


def test_import_of_empty_page_is_deleted_and_reported(fake):
    fake.stub_next_import = True
    with pytest.raises(ValueError, match="No recipe found"):
        recipes.create_recipe_from_url("https://x.se/404")
    assert fake.deleted == ["stub"]


def test_delete_recipe_only_own(fake):
    with pytest.raises(ValueError, match="not created through this server"):
        recipes.delete_recipe("testgryta")
    with pytest.raises(ValueError, match="No recipe"):
        recipes.delete_recipe("missing")
    assert recipes.delete_recipe("mine") == {"deleted": True, "slug": "mine", "name": "Testgryta"}
    assert fake.deleted == ["mine"]


def test_note_comes_from_the_cooks_line_not_the_llm():
    assert recipes._note_from_line("1/2 citron, saften", "saften-the juice") == "saften"
    assert recipes._note_from_line("1 gul lök, hackad", None) == "hackad"
    assert recipes._note_from_line("2 dl grädde", "cream") == ""
    assert recipes._note_from_line("salt och peppar", "och peppar") == "och peppar"


def test_shopping_remove_and_clear(fake):
    good = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ValueError, match="not a shopping item id"):
        shopping.check_shopping_items(["i1"])
    with pytest.raises(ValueError, match="No shopping item"):
        shopping.remove_shopping_items(["11111111-0000-4000-8000-000000000001"])
    out = shopping.remove_shopping_items([good])
    assert out["removed"][0]["text"] == "mjölk" and fake.deleted == [good]
    out = shopping.clear_checked_shopping_items()
    assert out["removed"] == 1 and out["open_items"] == 1 and fake.deleted[-1] == "i2"


def test_mealplan_missing_entry_is_a_clear_error(fake):
    with pytest.raises(ValueError, match="No meal plan entry with id 999"):
        mealplans.update_mealplan_entry(999, meal="lunch")
    with pytest.raises(ValueError, match="No meal plan entry"):
        mealplans.remove_mealplan_entry(999)


def test_422_detail_is_compacted():
    from mealie_mcp.client import _clean

    detail = [{"type": "uuid_parsing", "loc": ["path", "item_id"], "msg": "Input should be a UUID"}]
    assert _clean(detail) == "item_id: Input should be a UUID"
    assert _clean({"message": "Not found.", "error": True}) == "Not found."
