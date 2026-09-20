"""A fake Mealie behind httpx.MockTransport, enough for the tool logic."""

from __future__ import annotations

import json

import httpx
import pytest

from mealie_mcp import client as client_mod
from mealie_mcp.client import MealieClient

TAGS = [
    {"id": "t1", "groupId": "g", "name": "Vardag", "slug": "vardag"},
    {"id": "t3", "groupId": "g", "name": "Viktväktarna", "slug": "viktvaktarna"},
]
ME = "u-mcp"
CATS = [{"id": "c1", "groupId": "g", "name": "Middag", "slug": "middag"}]
RECIPE = {
    "id": "r1",
    "userId": "u-someone-else",
    "name": "Testgryta",
    "slug": "testgryta",
    "description": "d",
    "recipeServings": 4,
    "recipeCategory": CATS,
    "tags": TAGS,
    "totalTime": "30 min",
    "recipeIngredient": [{"display": "2 dl grädde", "title": "Sås"}, {"note": "salt"}],
    "recipeInstructions": [{"title": "", "text": "Koka."}],
    "notes": [],
    "nutrition": {},
    "orgURL": None,
}


class FakeMealie:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.requests: list[httpx.Request] = []
        self.created_tags: list[str] = []
        self.deleted: list[str] = []
        self.by_url: list[dict] = []
        self.stub_next_import = False
        self.parsed: list[dict] = []
        self.created_foods: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        self.requests.append(request)
        if path == "/api/users/self":
            return httpx.Response(200, json={"id": ME, "username": "mcp", "admin": False})
        if path == "/api/organizers/tags" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": TAGS
                    + [
                        {"id": "t2", "groupId": "g", "name": n, "slug": n}
                        for n in self.created_tags
                    ]
                },
            )
        if path == "/api/organizers/tags" and request.method == "POST":
            self.created_tags.append(body["name"])
            return httpx.Response(
                201,
                json={
                    "id": "t2",
                    "groupId": "g",
                    "name": body["name"],
                    "slug": body["name"].lower(),
                },
            )
        if path == "/api/organizers/categories":
            return httpx.Response(200, json={"items": CATS})
        if path == "/api/recipes" and request.method == "GET":
            if request.url.params.get("queryFilter"):
                return httpx.Response(200, json={"items": self.by_url, "total": len(self.by_url)})
            if request.url.params.get("search") == "none":
                return httpx.Response(200, json={"items": [], "total": 0})
            return httpx.Response(
                200, json={"items": [RECIPE], "total": 1, "page": 1, "total_pages": 1}
            )
        if path == "/api/recipes" and request.method == "POST":
            return httpx.Response(201, json=body["name"].lower())
        if path == "/api/app/about":
            return httpx.Response(200, json={"enableOpenai": False})
        if path == "/api/parser/ingredients":
            return httpx.Response(200, json=self.parsed)
        if path == "/api/foods" and request.method == "GET":
            return httpx.Response(200, json={"items": []})
        if path == "/api/foods" and request.method == "POST":
            self.created_foods.append(body["name"])
            return httpx.Response(201, json={"id": f"f-{len(self.created_foods)}", **body})
        if path == "/api/recipes/create/url":
            return httpx.Response(201, json="stub" if self.stub_next_import else "imported")
        if path.startswith("/api/recipes/") and request.method == "GET":
            slug = path.rsplit("/", 1)[1]
            if slug == "missing":
                return httpx.Response(404, json={"detail": "Not found"})
            if slug == "stub":
                return httpx.Response(
                    200,
                    json={
                        **RECIPE,
                        "slug": slug,
                        "name": "No Recipe Name Found - 689df663",
                        "userId": ME,
                        "recipeIngredient": [{"note": "Could not detect ingredients"}],
                    },
                )
            if slug == "mine":
                return httpx.Response(200, json={**RECIPE, "slug": slug, "userId": ME})
            return httpx.Response(200, json={**RECIPE, "slug": slug})
        if path.startswith("/api/recipes/") and request.method == "PUT":
            return httpx.Response(200, json=body)
        if path.startswith("/api/recipes/") and request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=RECIPE)
        if path.startswith("/api/households/mealplans/") and request.method == "GET":
            return httpx.Response(404, json={"detail": {"message": "Not found.", "error": True}})
        if path == "/api/households/shopping/items" and request.method == "DELETE":
            self.deleted += request.url.params.get_list("ids")
            return httpx.Response(200, json={"message": "", "error": False})
        if path.startswith("/api/households/shopping/items/") and request.method == "GET":
            item_id = path.rsplit("/", 1)[1]
            if not item_id.startswith("0000"):
                return httpx.Response(404, json={"detail": {"message": "Not found."}})
            return httpx.Response(200, json={"id": item_id, "checked": False, "display": "mjölk"})
        if path.startswith("/api/households/shopping/items/") and request.method == "PUT":
            return httpx.Response(200, json=body)
        if path == "/api/households/mealplans" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": 1, "date": "2026-01-01", "entryType": "dinner", "recipe": RECIPE}
                    ]
                },
            )
        if path == "/api/households/mealplans" and request.method == "POST":
            return httpx.Response(
                201, json={"id": 2, **body, "recipe": RECIPE if body.get("recipeId") else None}
            )
        if path == "/api/households/shopping/lists":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "l1",
                            "name": "Veckohandling",
                        }
                    ]
                },
            )
        if path == "/api/households/shopping/lists/l1":
            return httpx.Response(
                200,
                json={
                    "id": "l1",
                    "name": "Veckohandling",
                    "listItems": [
                        {"id": "i1", "checked": False, "display": "mjölk"},
                        {"id": "i2", "checked": True, "display": "ägg"},
                    ],
                },
            )
        if path == "/api/households/shopping/items/create-bulk":
            return httpx.Response(201, json={"createdItems": body})
        return httpx.Response(500, json={"detail": f"unhandled {request.method} {path}"})


@pytest.fixture
def fake():
    fm = FakeMealie()
    client_mod.set_client(
        MealieClient("http://mealie.test", "tok", transport=httpx.MockTransport(fm.handler))
    )
    yield fm
    client_mod.set_client(None)
