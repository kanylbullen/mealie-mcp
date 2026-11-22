# Changelog

## 0.1.1 — 2026-11-22

### Fixed
- Recipe links were `{MEALIE_URL}/r/<slug>`, which current Mealie does not serve: recipe routes
  need the group, `{MEALIE_URL}/g/<group>/r/<slug>`. Every tool that returns a recipe link
  (`search_recipes`, `get_recipe`, `create_recipe`, `create_recipe_from_url`, `update_recipe`,
  `get_mealplan`, `add_mealplan_entry`, `update_mealplan_entry`, `add_random_mealplan_entry`)
  now includes it. The group slug is read once from `GET /api/users/self` (`groupSlug`) and
  cached on the client.

### Added
- `MEALIE_GROUP_SLUG` overrides the group slug from the API.
- `server_info` reports the group (`name`, `slug`, and `source`: `env`, `api`, or `null`).

### Notes
- If no group slug is available (older Mealie without `groupSlug`, or the lookup fails), links
  fall back to `/r/<slug>` and a warning is logged; a failed lookup is retried on the next link.

## 0.1.0 — 2026-09-20

First release.
