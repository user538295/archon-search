# Feature Brief: Fix `collection info` Output — Show Useful Details, Hide Embedding Vectors

## Problem
Running `archon-search collection info <name>` dumps a raw internal Python object including three arrays of 384 numbers each (embedding vectors), making the output unreadable and unusable in a terminal.

## Goal
`collection info` prints a clean, human-readable summary of a collection — the name, document count, model, status, and other operator-relevant details — with no raw embedding data. The output mirrors what the server already returns via its API.

## Users & Context
Operators and developers who want to inspect a collection's state — checking document counts, which embedding model is active, whether reindexing is needed, TTL settings, or whether a centroid has been computed. They run this command in a terminal and expect a quick, readable answer, not a data dump.

## Core Flow
1. User runs `archon-search collection info <collection-name>`.
2. CLI calls `GET /collections/<name>` on the running server (same pattern as `archon-search collection migrate`).
3. Server returns a `CollectionDetail` response — already filtered to human-relevant fields, with embedding vectors replaced by a `centroid_present: true/false` flag.
4. CLI formats and prints the response as labeled key-value lines.

## In Scope
- Replace the raw `str(meta)` output with a formatted display of `CollectionDetail` fields
- Fields to display: `name`, `description`, `namespace`, `doc_count`, `chunk_count`, `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`, `last_indexed`, `default_ttl_seconds`, `schema_version`, `centroid_present`
- Require the server to be running (consistent with the direction to proxy all read/write operations through the server)
- Error clearly if the server is not reachable or the collection is not found

## Out of Scope
- Standalone (no-server) mode for `collection info` — deferred to the broader CLI-proxies-to-server effort (bug-008)
- A `--json` flag for machine-readable output — can be added as a follow-on once the REST response is the source
- Displaying ACL sidecar details — separate concern, out of scope here

## Key Decisions
- **Use the REST API, not direct store access:** The server's `CollectionDetail` response already filters out embedding vectors and is the maintained contract. Going directly to the store would require maintaining a separate filtering layer in the CLI.
- **Centroid shown as present/absent (not the vector):** The raw centroid is never useful to a human. A boolean flag answers the only question an operator cares about: "has this collection been indexed?"
- **Require the server to be running:** Consistent with the direction for Issues 5, 7, 8 — the CLI is moving toward being a REST client, not a standalone pipeline runner. A clear error message handles the not-running case.

## Edge Cases & Constraints
- **Server not running:** Print `"Server is not running. Start it with: archon-search start"` and exit non-zero. Do not fall back to direct store access.
- **Collection not found (404):** Print `"Collection '<name>' not found."` and exit non-zero.
- **`pending_embedding_model` is null:** Omit that line from output rather than printing `None`.
- **`last_indexed` is null:** Show `"never"` rather than `None`.
- **`description` is null:** Omit the description line rather than printing `None`.

## Open Questions
- `CollectionDetail` in `schemas.py:444` does not include `default_ttl_seconds` or `schema_version` — verify whether these fields need to be added to the schema before implementation, or if they should be omitted from the display.
- `GET /collections/{name}` route: confirm it returns a single `CollectionDetail` (not a list) and that namespace filtering is handled by the auth middleware, not a query param.
- Formatting: key-value lines (`name: archon_search`) vs. a richer table layout (e.g. using `rich` if already a dependency) — check whether `rich` is in the dependency tree before deciding.

## Future Iterations
- `--json` flag for machine-readable output (useful for scripting and CI checks)
- `collection info` working without a running server (depends on bug-008 CLI proxy architecture being settled)

## References
- [[archon_search/cli/collection.py]] `[code-agent]` — current `info` command at line 227, raw `str(meta)` output
- [[archon_search/server/schemas.py]] `[code-agent]` — `CollectionDetail` at line 444, the clean filtered response model
- [[archon_search/server/routes_collections.py]] `[code-agent]` — `GET /collections/{name}` route handler
- [[archon_search/cli/maintenance_cmd.py]] `[code-agent]` — model for REST-proxying CLI command pattern

## Recommendation
This is a straightforward quality fix. The server already does the right filtering — the CLI just isn't using it. The hardest part is accepting the server-required constraint, which is the correct long-term direction. Do not add a standalone fallback here; that belongs in the broader CLI proxy refactor. The `CollectionDetail` schema gap (missing `default_ttl_seconds` and `schema_version`) should be confirmed before starting implementation — if those fields matter to operators, add them to the schema first.
