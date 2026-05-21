# Review: MigrationGuide/06_client_migration_examples.md

## Summary

The document is overall accurate against the server code. All before/after shape claims for both NR-1 (MCP `search` response shape) and NR-2 (REST `/search` `top_k` ignored) are verified. One factual issue: the doc says the config key lives under `[database]` in `archon-search.toml`, which **matches the code** (`config.py` lines 158–167 read from the `database` section). However, the doc's own cited source — `BREAKING.md` — claims the key lives under `[search]`. The doc is correct against code; BREAKING.md is wrong, and that's a discrepancy the doc papers over without flagging.

No other inaccuracies were found in the server-surface claims. The example client snippets reference plausible illustrative APIs (`session.identity`) that are out of scope for server-truth verification.

Verification basis:
- `archon_search/server/mcp.py` — `search` tool definition at lines 38–74; returns `{"results": [...], "acl_filtered": ...}` at line 59. Parameters are `query` and `collection` only (lines 39–42); no `top_k`.
- `archon_search/server/routes_search.py` — `SearchRequest` at lines 17–36 with `top_k: int = Field(default=5, ge=1, le=100)` (line 20). `SearchResponse` at lines 57–59 with `results` and `acl_filtered`. The route at lines 62–84 calls `pipeline.search(body.query, body.collection, namespace=ns)` (line 77) — `body.top_k` is never passed, confirming the route-level ignore.
- `archon_search/config.py` — `top_k_return: int = 5` default at line 40; loader reads from `database` TOML section at lines 158–167.
- `BREAKING.md` — `[next release]` entries match the doc's two-change scope; but the second entry literally says `[search] top_k_return`, conflicting with the code's `[database]` section.

## Inaccuracies (numbered)

1. **Mismatch between cited authority and code, silently resolved.** Section "NR-2 → Operator step (config)" instructs `[database] top_k_return = 10`. This is correct per `archon_search/config.py:158–167` (loader reads `database["top_k_return"]`). However, `BREAKING.md`'s NR-2 entry says: *"Configure `[search] top_k_return` in `archon-search.toml`"*. The migration doc claims `BREAKING.md` is the authoritative contract ("`BREAKING.md` defines the contract; this doc shows the diff") and yet diverges from it without acknowledgement. Either the migration doc should flag the BREAKING.md error, or BREAKING.md should be fixed. As-written, the doc is correct against code but contradicts its declared source-of-truth.

## Verified claims

- NR-1 old shape: bare list of result dicts on MCP `search`. (Plausible pre-change shape; current code returns the new dict shape — see point under "Verification basis".)
- NR-1 new shape: `{"results": [...], "acl_filtered": bool}` — **verified** (`mcp.py:59`).
- Result dict fields `doc_id`, `chunk_id`, `text`, `score`, `source_path` — **verified** via `SearchResultSchema` (`routes_search.py:39–54`) and the `asdict(r)` of `SearchResult` returned by the pipeline.
- "REST `/search` is unaffected by NR-1. Its response has always been `{"results": [...], "acl_filtered": ...}`" — **verified** (`routes_search.py:57–59`); the "always been" historical claim is unverifiable from current code alone but is structurally consistent.
- "The new `acl_filtered` flag was previously unavailable on the MCP surface" — consistent with NR-1 being the change that introduces it; not contradicted by code.
- NR-2 "the route ignores `top_k`" — **verified**: `routes_search.py:77` calls `pipeline.search(body.query, body.collection, namespace=ns)`; `body.top_k` is parsed but never forwarded.
- NR-2 "`SearchRequest` still declares `top_k: int = Field(default=5, ge=1, le=100)`" — **verified verbatim** at `routes_search.py:20`.
- NR-2 "Sending it is not an error — it is silently ignored" — **verified** by code path (request body validates and is then dropped on the floor).
- NR-2 "the pipeline uses `config.top_k_return`" — consistent with `config.py:40` default `5`; pipeline-side use not re-verified line-by-line here but matches the route's behavior of not overriding it.
- "default `5`" for `top_k_return` — **verified** (`config.py:40`).
- "MCP `search` does not accept a `top_k` parameter, so NR-2 has no MCP-side code change" — **verified** (`mcp.py:39–42` parameters are `query` and `collection` only).
- "9 total" MCP tools (implicit via doc's reference to `mcp.py`) — **verified**: `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document` (`mcp.py` `@app.tool()` decorators).
- File/code-path references (`archon_search/server/mcp.py`, `archon_search/server/routes_search.py`) — **verified** to exist and contain the claimed symbols.

## Unverifiable / ambiguous

- Historical claim that REST `/search` "has always been" `{"results":[...], "acl_filtered":...}` — cannot verify "always" without git archaeology; current shape is correct.
- Client snippets' SDK surfaces (e.g. `session.identity` in Python MCP client, `@modelcontextprotocol/sdk` types) are illustrative consumer-side code, not server claims; not within review scope.
- Cross-references to `Architecture/530_technical_debt_refactoring_roadmap.md` "API-2" and `03_breaking_changes_index.md` — link targets not validated as part of this review (server-code-only scope).
- "There is no shim period; when the tag lands, both shapes do not coexist." — release-process claim, not testable against current source.
- The doc's "Last reviewed: 2026-05-20 / Next review: 2027-05-20" header dates — meta, not server-verifiable.
