"""Recipe links: /g/<group>/r/<slug>, with the group from the API, from
MEALIE_GROUP_SLUG, or (older Mealie, no group known) plain /r/<slug>."""

import logging

import httpx
from conftest import FakeMealie

from mealie_mcp.client import MealieClient
from mealie_mcp.tools import mealplans, recipes, server_info


def _client(fm: FakeMealie, **kw) -> MealieClient:
    return MealieClient(
        "http://mealie.test/", "tok", transport=httpx.MockTransport(fm.handler), **kw
    )


def test_group_slug_from_api_is_fetched_once():
    fm = FakeMealie()
    c = _client(fm)
    assert c.recipe_url("pannkakor") == "http://mealie.test/g/lugnet/r/pannkakor"
    assert c.recipe_url("gryta") == "http://mealie.test/g/lugnet/r/gryta"
    assert sum(1 for _, p, _ in fm.calls if p == "/api/users/self") == 1
    assert c.group_slug_source == "api"


def test_group_slug_env_overrides_api_without_a_lookup():
    fm = FakeMealie()
    c = MealieClient.from_env(
        {"MEALIE_URL": "http://mealie.test", "MEALIE_TOKEN": "tok", "MEALIE_GROUP_SLUG": "annan"}
    )
    c._http = _client(fm)._http
    assert c.recipe_url("pannkakor") == "http://mealie.test/g/annan/r/pannkakor"
    assert c.group_slug_source == "env"
    assert not fm.calls


def test_public_url_is_used_for_links():
    c = _client(FakeMealie(), public_url="https://mealie.example/")
    assert c.recipe_url("x") == "https://mealie.example/g/lugnet/r/x"


def test_no_group_slug_falls_back_to_plain_route_and_warns(caplog):
    fm = FakeMealie()
    fm.me_group = {}
    c = _client(fm)
    with caplog.at_level(logging.WARNING, logger="mealie_mcp.client"):
        assert c.recipe_url("pannkakor") == "http://mealie.test/r/pannkakor"
        assert c.recipe_url("pannkakor") == "http://mealie.test/r/pannkakor"
    assert c.group_slug is None and c.group_slug_source is None
    assert sum("groupSlug" in r.message for r in caplog.records) == 1  # warned once, cached


def test_failed_lookup_falls_back_without_raising_and_retries(caplog):
    fm = FakeMealie()
    down = {"on": True}

    def handler(request):
        if down["on"] and request.url.path == "/api/users/self":
            return httpx.Response(503, json={"detail": "down"})
        return fm.handler(request)

    c = MealieClient("http://mealie.test", "tok", transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="mealie_mcp.client"):
        assert c.recipe_url("x") == "http://mealie.test/r/x"
    assert caplog.records
    down["on"] = False
    assert c.recipe_url("x") == "http://mealie.test/g/lugnet/r/x"


def test_every_tool_link_carries_the_group(fake):
    urls = [r["url"] for r in recipes.search_recipes(query="gryta")["recipes"]]
    urls.append(recipes.get_recipe("testgryta")["url"])
    urls += [
        e["recipe"]["url"] for e in mealplans.get_mealplan("2026-01-01", "2026-01-07")["entries"]
    ]
    assert urls and all(u.startswith("http://mealie.test/g/lugnet/r/") for u in urls)


def test_server_info_shows_fallback_group(fake):
    fake.me_group = {}
    assert server_info.server_info()["mealie"]["group"] == {
        "name": None,
        "slug": None,
        "source": None,
    }
