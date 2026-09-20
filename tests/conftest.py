"""A fake Mealie behind httpx.MockTransport, enough for the tool logic."""

from __future__ import annotations

import json

import httpx
import pytest

from mealie_mcp import client as client_mod
from mealie_mcp.client import MealieClient

TAGS = [{"id": "t1", "groupId": "g", "name": "Vardag", "slug": "vardag"}]
CATS = [{"id": "c1", "groupId": "g", "name": "Middag", "slug": "middag"}]
RECIPE = {
    "id": "r1",
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
        self.created_tags: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/api/users/self":
            return httpx.Response(200, json={"username": "mcp", "admin": False})
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
            return httpx.Response(
                200, json={"items": [RECIPE], "total": 1, "page": 1, "total_pages": 1}
            )
        if path == "/api/recipes" and request.method == "POST":
            return httpx.Response(201, json=body["name"].lower())
        if path.startswith("/api/recipes/") and request.method == "GET":
            slug = path.rsplit("/", 1)[1]
            if slug == "missing":
                return httpx.Response(404, json={"detail": "Not found"})
            return httpx.Response(200, json={**RECIPE, "slug": slug})
        if path.startswith("/api/recipes/") and request.method == "PUT":
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
                            "listItems": [{"id": "i1", "checked": False, "display": "mjölk"}],
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
                    "listItems": [{"id": "i1", "checked": False, "display": "mjölk"}],
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
