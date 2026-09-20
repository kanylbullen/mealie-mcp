"""Thin HTTP client for the Mealie REST API.

One client, one Mealie API token. Configuration (environment):

    MEALIE_URL          base URL of the Mealie instance, e.g. http://127.0.0.1:9000
    MEALIE_TOKEN        long-lived API token (Mealie: user menu -> API tokens).
                        Use a dedicated non-admin user: everything this server
                        does is attributed to it in Mealie.
    MEALIE_TOKEN_ENV    optional indirection: name of another variable that
                        holds the token (e.g. MEALIE_MCP_TOKEN as stored in a
                        vault), so no shell needs to copy secrets between names
    MEALIE_PUBLIC_URL   optional; the URL humans open in a browser, used for
                        links in tool results (defaults to MEALIE_URL)
    MEALIE_TIMEOUT      seconds per request (default 30). URL imports get 3x
                        because Mealie fetches and possibly runs an LLM pass.

The client deliberately exposes only the endpoints the tools use.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

USER_AGENT = "mealie-mcp/0.1 (+https://github.com/kanylbullen/mealie-mcp)"


class MealieError(RuntimeError):
    """A Mealie API call failed. `status` is the HTTP status (0 = transport)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class MealieConfigError(RuntimeError):
    pass


def _clean(detail: Any, limit: int = 400) -> str:
    """Error detail as one short line. FastAPI's 422 body is a list of
    Pydantic error dicts; a model only needs "where: what", not the ctx."""
    if isinstance(detail, list) and detail and all(isinstance(d, dict) for d in detail):
        parts = []
        for d in detail:
            loc = ".".join(str(x) for x in d.get("loc") or [] if x not in ("body", "path", "query"))
            parts.append(f"{loc}: {d.get('msg')}" if loc else str(d.get("msg")))
        text = "; ".join(parts)
    elif isinstance(detail, dict) and isinstance(detail.get("message"), str):
        text = detail["message"]
    else:
        text = detail if isinstance(detail, str) else repr(detail)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MealieClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        public_url: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not base_url or not token:
            raise MealieConfigError("MEALIE_URL and MEALIE_TOKEN are required.")
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self.timeout = timeout
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MealieClient:
        src = os.environ if env is None else env
        token = (src.get("MEALIE_TOKEN") or "").strip()
        indirect = (src.get("MEALIE_TOKEN_ENV") or "").strip()
        if not token and indirect:
            token = (src.get(indirect) or "").strip()
        return cls(
            (src.get("MEALIE_URL") or "").strip(),
            token,
            public_url=(src.get("MEALIE_PUBLIC_URL") or "").strip() or None,
            timeout=float(src.get("MEALIE_TIMEOUT") or 30),
        )

    # -- plumbing ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            resp = self._http.request(
                method,
                path,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise MealieError(f"Mealie unreachable: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise MealieError(
                f"Mealie {method} {path} -> {resp.status_code}: {_clean(detail)}",
                status=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def recipe_url(self, slug: str) -> str:
        """Browser URL of a recipe. Mealie's group route needs the group slug,
        so `/r/<slug>` is used: it redirects to the right group when logged in."""
        return f"{self.public_url}/r/{slug}"

    # -- app --------------------------------------------------------------

    def about(self) -> dict[str, Any]:
        return self.get("/api/app/about")

    def whoami(self) -> dict[str, Any]:
        return self.get("/api/users/self")

    @property
    def user_id(self) -> str:
        """Id of the Mealie user behind the token (cached; it cannot change)."""
        if not hasattr(self, "_user_id"):
            self._user_id = str(self.whoami()["id"])
        return self._user_id

    # -- recipes ----------------------------------------------------------

    def search_recipes(
        self,
        *,
        search: str | None = None,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        require_all_tags: bool = False,
        per_page: int = 20,
        page: int = 1,
        order_by: str | None = None,
        order_direction: str = "asc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "search": search or None,
            "page": page,
            "perPage": per_page,
            "orderBy": order_by,
            "orderDirection": order_direction,
            "requireAllTags": "true" if require_all_tags else None,
        }
        # httpx encodes list values as repeated query params, which is what Mealie expects.
        if categories:
            params["categories"] = categories
        if tags:
            params["tags"] = tags
        return self.request("GET", "/api/recipes", params=params)

    def get_recipe(self, slug: str) -> dict[str, Any]:
        return self.get(f"/api/recipes/{slug}")

    def recipes_by_source_url(self, url: str) -> list[dict[str, Any]]:
        """Recipes whose orgURL equals `url` (with or without trailing slash).

        Mealie's `queryFilter` is a string expression with double-quoted values
        and no escape that survives (a backslash makes it 500), so a URL
        containing a quote cannot be expressed: such a URL skips the query
        rather than risking a filter that means something else. The result is
        compared again here, so a mangled filter can only cost a round trip,
        never return an unrelated recipe as a "duplicate".
        """
        variants = {url, url.rstrip("/"), url.rstrip("/") + "/"}
        if any('"' in v for v in variants):
            return []
        clause = " OR ".join(f'orgURL = "{v}"' for v in sorted(variants))
        found = self.get("/api/recipes", queryFilter=clause, perPage=10).get("items", [])
        return [r for r in found if str(r.get("orgURL") or "").rstrip("/") == url.rstrip("/")]

    def delete_recipe(self, slug: str) -> None:
        self.request("DELETE", f"/api/recipes/{slug}")

    def create_recipe(self, name: str) -> str:
        """Create an empty recipe; Mealie returns the new slug."""
        return str(self.request("POST", "/api/recipes", json={"name": name}))

    def create_recipe_from_url(self, url: str, *, include_tags: bool = False) -> str:
        return str(
            self.request(
                "POST",
                "/api/recipes/create/url",
                json={"url": url, "includeTags": include_tags},
                timeout=self.timeout * 3,
            )
        )

    def update_recipe(self, slug: str, recipe: dict[str, Any]) -> dict[str, Any]:
        """Full replace (PUT). Callers fetch, modify and send the whole object."""
        return self.request("PUT", f"/api/recipes/{slug}", json=recipe)

    def patch_recipe(self, slug: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/api/recipes/{slug}", json=fields)

    def parse_ingredients(
        self, ingredients: list[str], *, parser: str = "nlp"
    ) -> list[dict[str, Any]]:
        return self.request(
            "POST",
            "/api/parser/ingredients",
            json={"parser": parser, "ingredients": ingredients},
            timeout=self.timeout * 3,
        )

    # -- organizers -------------------------------------------------------

    def categories(self) -> list[dict[str, Any]]:
        return self.get("/api/organizers/categories", perPage=-1).get("items", [])

    def tags(self) -> list[dict[str, Any]]:
        return self.get("/api/organizers/tags", perPage=-1).get("items", [])

    def create_category(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/api/organizers/categories", json={"name": name})

    def create_tag(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/api/organizers/tags", json={"name": name})

    # -- meal plans -------------------------------------------------------

    def mealplans(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self.get(
            "/api/households/mealplans",
            start_date=start_date,
            end_date=end_date,
            perPage=-1,
            orderBy="date",
            orderDirection="asc",
        ).get("items", [])

    def get_mealplan_entry(self, entry_id: int) -> dict[str, Any]:
        return self.get(f"/api/households/mealplans/{entry_id}")

    def create_mealplan_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/households/mealplans", json=entry)

    def update_mealplan_entry(self, entry_id: int, entry: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/api/households/mealplans/{entry_id}", json=entry)

    def delete_mealplan_entry(self, entry_id: int) -> None:
        self.request("DELETE", f"/api/households/mealplans/{entry_id}")

    def random_mealplan_entry(self, date: str, entry_type: str) -> dict[str, Any]:
        return self.request(
            "POST", "/api/households/mealplans/random", json={"date": date, "entryType": entry_type}
        )

    # -- shopping ---------------------------------------------------------

    def shopping_lists(self) -> list[dict[str, Any]]:
        return self.get("/api/households/shopping/lists", perPage=-1).get("items", [])

    def shopping_list(self, list_id: str) -> dict[str, Any]:
        return self.get(f"/api/households/shopping/lists/{list_id}")

    def add_recipe_to_shopping_list(self, list_id: str, recipe_id: str, scale: float = 1) -> Any:
        return self.request(
            "POST",
            f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}",
            json={"recipeIncrementQuantity": scale},
        )

    def create_shopping_items(self, items: list[dict[str, Any]]) -> Any:
        return self.request("POST", "/api/households/shopping/items/create-bulk", json=items)

    def shopping_item(self, item_id: str) -> dict[str, Any]:
        return self.get(f"/api/households/shopping/items/{item_id}")

    def update_shopping_item(self, item_id: str, item: dict[str, Any]) -> Any:
        return self.request("PUT", f"/api/households/shopping/items/{item_id}", json=item)

    def update_shopping_items(self, items: list[dict[str, Any]]) -> Any:
        """Bulk update. Each entry must be a WHOLE item: Mealie fills anything
        left out with defaults, so a partial entry silently wipes note,
        quantity and position (verified 2026-09-20)."""
        if not items:
            return None
        return self.request("PUT", "/api/households/shopping/items", json=items)

    def delete_shopping_items(self, item_ids: list[str]) -> None:
        if item_ids:
            self.request("DELETE", "/api/households/shopping/items", params={"ids": item_ids})


_client: MealieClient | None = None


def get_client() -> MealieClient:
    """Process-wide client, built from the environment on first use."""
    global _client
    if _client is None:
        _client = MealieClient.from_env()
    return _client


def set_client(client: MealieClient | None) -> None:
    """Override the process-wide client (tests)."""
    global _client
    _client = client
