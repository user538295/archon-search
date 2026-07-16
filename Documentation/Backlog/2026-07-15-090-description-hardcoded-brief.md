# Feature Brief: Collection Responses Always Show Blank Description

## Problem
Every time you view a collection — in a list, in detail, or after updating it — the description field is always empty, even when archon-search has already generated one.

## Goal
Collection responses return the real stored description instead of an empty string.

## Users & Context
Operators who have description generation enabled (`description_generator` configured) and anyone using the REST API or CLI to inspect collections. They see a blank field where a useful summary should appear.

## Core Flow
1. User calls `GET /collections`, `GET /collections/{name}`, or `PATCH /collections/{name}`.
2. The response includes the collection's stored description (not `""`).

## In Scope
- Fix `description=""` literal in three route handlers: `list_collections`, `get_collection_info`, `patch_collection` (`routes_collections.py:110, 375, 611`).
- Read `meta.description` (already populated by the description generator) and surface it in the response.

## Out of Scope
- Changing how descriptions are generated or stored — they're already correct on disk.
- Adding description generation where it doesn't exist today.

## Key Decisions
- **`meta.description or ""`**: Fall back to empty string if no description exists yet — avoids `None` leaking into the API response.
- **Bundle with bug-024** (`chunk_count=0` hardcoded): same file, same handler methods, same PR. No reason to split.

## Edge Cases & Constraints
- Collections created before the description generator was enabled will have no description; the `or ""` fallback handles this cleanly.
- The `patch_collection` handler calls `count_chunks()` for embedding-model logic but discards the result (bug-024); fixing both in the same PR keeps the handlers consistent.

## Open Questions
- ~~Does `CollectionSummary` (used by `list_collections`) expose `description` in its Pydantic schema?~~ **Resolved:** yes — `CollectionSummary` declares `description: str = ""` (`schemas.py:435`) and `CollectionDetail` inherits it (`schemas.py:444`). No schema change needed; the default already handles the "not marked non-optional" concern.
- **Implementation note (not a decision):** in `list_collections`, `col_meta` can be `None` (`routes_collections.py:106`, `meta_by_name.get(name)`), so guard the read — `col_meta.description if col_meta else ""`, not a bare `col_meta.description or ""`, which would crash on a collection with no metadata. Verify whether `meta` can also be `None` in `get_collection_info`/`patch_collection` before writing `meta.description`.

## Future Iterations
- Surface description in `GET /status` collection entries (currently also returns stub data — tracked separately).

## References
- [[archon_search/server/routes_collections.py]] `[code-agent]` — lines 110, 375, 611 contain the hardcoded `description=""` literals
- [[Documentation/Backlog/bug-024-chunk-count-hardcoded-brief.md]] `[user]` — sibling bug in the same handlers; bundle both fixes into one PR

## Recommendation
One-line fix per handler, three lines total. The description is already stored correctly — this is purely a handler read gap. Bundle with bug-024 so the collection response handlers are fixed end-to-end in a single PR.
