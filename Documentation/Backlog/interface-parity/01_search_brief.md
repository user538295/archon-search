# Feature Brief: Search on one implementation across REST, MCP, OpenAI shim and CLI

Slice S1 of the [interface parity roadmap](./roadmap.md), the pilot. It follows the rules C1-C14 of [Brief 00](./00_service_layer_foundation_brief.md). Roadmap S0 has landed: `archon_search/services/` (`authz`, `request_hyde`, `search_budget`, `validators`), the import-boundary test `tests/test_layer_import_boundary.py` and the parity registry `tests/parity/registry.py` already exist, so this slice edits them. This is the first consumer of the service layer, so it implements what Brief 00 deferred: `ServiceError`, the services container, the first service and the adapter error-mapping tables, none of which exist yet.

## Problem

Search is implemented three times. REST (`server/routes_search.py`, handler from line 292), the MCP `search` tool (`server/mcp.py`, from line 513) and the OpenAI shim (`server/routes_openai_shim.py`, `chat_completions` from line 152) all call `SearchPipeline.search` or `search_many`, and each re-implements everything around the call. A comparison of the three found 20 behavioral differences (table below), several of them visible to users: an MCP single-collection search can block up to 120 seconds on a cold embedder where REST degrades to full-text-only after 0.5 seconds; a `ppr` search over several collections is a 422 on REST and silently runs on MCP; MCP records no telemetry when a search times out, and the shim records no telemetry for any outcome. The CLI has no search command at all.

The `top_k` parameter is worse than a drift. REST validates it (`1 <= top_k <= top_k_max`) and then never forwards it, because `pipeline.search` and `search_many` take no `top_k`; the result count comes only from `[database].top_k_return` (`tests/integration/test_s313_top_k_does_not_set_result_count.py` pins this). The OpenAI shim accepts `top_k` and ignores it. MCP has no `top_k`.

## Goal

The same query returns the same ordered results, the same derived fields and the same errors on REST, MCP and the CLI. The OpenAI shim runs on the same service, keeping its protocol-specific rendering. One `SearchService` owns validation, collection resolution, embedder selection, warm-up, HyDE and RAG Fusion, the budget, telemetry and derived fields. Operators can run `archon-search search` from a terminal. The parity registry reports Search as covered on every interface.

## Users & Context

- API clients (REST) that today depend on exact 422 strings.
- MCP agents (Claude Code and others) that call `search` with flat filter parameters.
- OpenAI-compatible clients using `model = "archon-search"` or `"archon-search/<collection>"`.
- Operators who want to try a query from a terminal without writing `curl`.

## Core Flow

The canonical order of steps, owned by `SearchService.search(command, caller)`:

1. Normalize and validate the command: stripped non-empty `query`; exactly one of `collection` or `collections`; non-empty, stripped, de-duplicated `collections`; `collections` count within `max_fanout`; `top_k` within bounds; `scope_filter` syntax; `graph_mode` value, `graph` enabled, `scope_filter` and `graph_mode` mutually exclusive, `ppr` not combined with `collections`. REST today validates the query, the collection selection and the `graph_mode` value in its Pydantic request model first (a framework-shaped 422), then in the handler: fan-out count, `top_k` above the maximum, `scope_filter` syntax, `scope_filter` with `graph_mode`, `ppr` with `collections`, and graph enabled last. Which of the two orders the service keeps, and how REST renders model-level errors, is open (team plan Q1, Q2).
2. Verify the collection exists in the caller's namespace (single path) or let `search_many` verify it (multi path); translate a missing collection and a metadata-store failure into `ServiceError`.
3. Select the embedder (single path): wait at most the embedder warm-up bound, then degrade to full-text-only with `embedder_cold` instead of failing.
4. Warm the models (`warmup_for_search`).
5. Resolve HyDE and RAG Fusion, suppressed by the existing rules (`hyde_suppressed`).
6. Run the pipeline under the search budget.
7. Derive response fields once (`expansion_used`, `expansion_warning`, the effective embedding model, applied filters, metadata projection, ACL gate when requested).
8. Record telemetry for every outcome: success, timeout, internal error.
9. Return a `SearchResult`; each adapter renders it.

CLI: `archon-search search "<query>" --collection docs [--collection other] ...` prints a ranked list (rank, score, collection, source path, text excerpt); `--json` prints the full response. One `--collection` selects the single-collection path; several select the multi-collection path.

## In Scope

- `SearchService` (single and multi-collection), `SearchCommand`, the service result type, the first `ServiceError` types and the services container, following C2-C6. The result type cannot be called `SearchResult`: `archon_search/_types.py` already defines the per-chunk `SearchResult` (imported across the package); the name is open (team plan Q3).
- Rewiring of REST `POST /search`, the MCP `search` tool and the OpenAI shim's search path to the service; deletion of the duplicated code in those three files, including the telemetry-enqueue wrappers, the error mapping and the `config is None` fallbacks that exist only for the test harness. In `mcp.py` the shared failure constants, `_enqueue_internal_error`, the scope-filter helper and `_resolve_embedder` are also used by `search_with_context`, `explain` and the ingest tools, so they stay until those slices; the other tools' `config is None` fallbacks stay too.
- Resolution of every drift in the table below.
- Removal of the duplicated literals: `"search failed"`, `"internal_error"`, the three valid-`graph_mode` definitions, the fan-out and `top_k` messages (also repeated in `routes_explain.py` and the MCP explain tool, which S3 will then reuse), `"collection not found"`, `"Retry-After": "30"`, and the `5`, `100` and `8` limits.
- New CLI command `search` in `cli/search.py`, registered in `cli/main.py`. The CLI is an HTTP client of REST and never imports `services/` or `server/` (boundary rule 4, ADR 20). The shared CLI helpers from S0 are `cli/_helpers.py` (key resolver, default URL, connection-failure message, text cleaner); the request-sending and error-rendering helpers are private to `cli/collection.py`, which a command may not import, so where they live is open (team plan Q19).
- MCP `search` gains `acl_context` and `top_k` (Q1); REST and MCP return the same response fields; the MCP response schema gains `embedding_model`, `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, `applied_filters` and `acl_gate`.
- The first field-level parity check (decision Q3).
- Docs: `600`, `110`, `120`, `140`, `520`, `UserManual/60_searching.md`, `UserManual/65_graph_search.md`, the MCP developer guide (`05_mcp_integration`), the new CLI command (no per-command CLI doc exists today; where it goes is open, team plan Q22), `BREAKING.md` plus the migration-guide row (`MigrationGuide/03_breaking_changes_index.md`). Other docs also quote the changed behavior and are listed in the team plan's Documentation update section: `UserManual/85_openai_compatible_api.md`, `UserManual/30_configuration.md`, `DeveloperGuide/06_error_handling.md`, `MigrationGuide/06_client_migration_examples.md`, `530`, `tsp_contract/openapi-openai-shim.yaml`, ADR 20 and ADR 17.

## Out of Scope

- `search_with_context` (S2), `explain` (S3) and `route` (S4). They reuse the pieces this slice extracts.
- Changes to retrieval algorithms, reranking, fusion or the graph expansion. The only core change allowed is per-request `top_k` (decision Q1).
- The shim's streaming format and its OpenAI envelope; they remain adapter rendering.
- Making `SEARCH_TIMEOUT_SECONDS` configurable (existing `TODO` in `services/search_budget.py`).
- `MultiCollectionRouter`: no search surface uses it today (only `route`, `explain`).

## Key Decisions

Drift table. "Canonical" is what the service does; REST is canonical where it is the more complete and better-tested surface (decision D5).

| # | Drift today (REST / MCP / shim) | Canonical behavior |
|---|---|---|
| 1 | `top_k`: validated then dropped / absent / ignored | Real on every interface: validated (`1 <= top_k <= top_k_max`) and forwarded to the pipeline; MCP and the CLI gain it (decision Q1) |
| 2 | No collection: 422 / falls back to a configured default or the literal `"default"` / fans out over all collections | `collection` or `collections` is required in the service (REST's rule); MCP loses its default. The shim keeps its model-name mapping (`archon-search` means all collections) as adapter policy, like row 7 (decision Q2) |
| 3 | `collection` handling: stripped, empty is 422 / `collection or default`, not stripped / not stripped | Stripped; empty is a validation error |
| 4 | Query: stripped, empty is 422 / unvalidated / blank is 400 and the unstripped text is searched | Stripped, empty is a validation error; the shim keeps its own error envelope and status for "no user message" |
| 5 | Validation order differs | REST's order (Core Flow step 1) |
| 6 | `ppr` with `collections`: 422 / allowed and returns `ppr_entities_matched=0` / n/a | Rejected (the pipeline path for it is a silent fall-through, so allowing it returns a misleading result) |
| 7 | Too many collections: 422 / `validation_error` / silently truncated | Service rejects. The shim keeps truncation as an adapter policy, because the model name `archon-search` means "all collections" and the protocol has no way to ask the caller to narrow; the truncation limit is read from the same `max_fanout` and still logs a warning |
| 8 | Single-path embedder wait: 0.5 s then full-text-only / unbounded (up to 120 s) / unbounded. The multi path already uses the bounded model warm-up on MCP and the shim | Bounded wait then degrade (REST). Behavior change for MCP and the shim (single path) |
| 9 | Metadata lookup failure on the single path: 503 `metadata_store_error` / generic internal error / 503 envelope | 503-class `metadata_store_error` for all |
| 10 | Timeout telemetry: recorded / not recorded / not recorded (the shim records no telemetry at all, success included) | Recorded for every outcome; whether shim traffic starts being recorded is open (team plan Q10) |
| 11 | Stage timings: single path, behind a config flag / same / none | Service records when the flag is on; the shim keeps none unless trivial |
| 12 | `acl_context` and `acl_gate`: supported / absent / absent | Supported in the service; exposed on REST, MCP and CLI; the shim does not expose it |
| 13 | Response echoes (`embedding_model`, `rag_fusion_*`, `applied_filters`): REST only | Computed in the service; present in REST, MCP and CLI `--json` |
| 14 | `include_metadata`: top-level OR `filters.include_metadata` / parameter only | REST's rule, in the service |
| 15 | Multi-path filters: passed as-is / `None` when no filter, otherwise built without `include_metadata` | One filter construction in the service, same on single and multi paths |
| 16-17 | HyDE, RAG Fusion, `graph_mode`, `scope_filter`: supported / supported / absent | Supported in the service; the shim does not expose them |
| 18 | Unknown collection on multi path: 404 / `not_found` / 404 | One `not_found` error, adapters map it |
| 19 | Namespace failure: middleware stamps / `ToolError` when absent / middleware stamps | Adapters resolve the caller; a missing identity is refused before the service is called |
| 20 | `namespace` passed as keyword / positional (single path only) / keyword | Keyword |

Other decisions:

- **Single versus multi path stays visible.** `collection="x"` runs `pipeline.search` with the per-collection embedder; `collections=["x"]` runs `search_many` with the global embedder and excludes collections whose model differs. This is core behavior, not drift, so the service exposes both and the CLI maps one `--collection` to the first and several to the second.
- **Errors move into the service (C5)**: every `except` in the three search handlers (`GraphCommunitiesNotBuiltError`, `CollectionNotFoundError`, `FanoutTimeoutError` and `SearchBudgetExceeded`, `MetadataLookupError`, `EmbedderNotReadyError`) becomes one translation in the service plus one table per adapter. The HyDE and RAG Fusion dependency errors no longer exist (ADR 19 removed them; a HyDE generator failure reaches the generic failure path), so the service has no such error kind. Existing REST body shapes (plain `ErrorDetail` versus the coded `{"detail":{"code","message"}}`) are preserved unless the table row says otherwise; the `Retry-After: 30` header is set from one constant.
- **MCP filter parameters stay flat; REST's `filters` stays nested.** Both build the same `SearchFilters` inside the service from the same canonical field names. The existing superset guard `tests/server/test_mcp_search.py:262` keeps protecting this.
- **Existing tests that pin drifted behavior are rewritten, not re-pinned** (C13): for example the MCP `ppr` plus `collections` skip in `tests/test_mcp.py:614-624` (its comment claims MCP rejects it before HyDE, which is false), and the stale docstring in `tests/integration/test_mcp_error_paths.py:101-104`. The exact-string integration tests (`test_e0c_be3_validation.py`, `test_e2h_t1_ppr_guard_paths_e2e.py`) must keep passing for REST.

## Edge Cases & Constraints

- **Cold embedder.** REST degrades; `tests/integration/test_s299_prefix_filter_cold_embedder.py` pins that. After S1, MCP and the shim degrade the same way, so an MCP client can receive full-text-only results where it used to wait.
- **Pipeline assertions.** `search_many` asserts that `scope_filter` and `graph_mode` are not combined (`pipeline.py` lines 3367, 3453 and 3498); the service validates first so the assert stays unreachable.
- **`fts_only` side effect.** In the pipeline, `fts_only` is cancelled when a `query_vector` is given; otherwise it also disables `rag_fusion` and `graph_mode`. The service passes `fts_only` unchanged and must not re-derive it.
- **Graph disabled.** MCP treats `config is None` as graph disabled; REST assumes config is present. The service requires config; the harness fallbacks in `mcp.py` are removed and the MCP tests build a real config.
- **Shim.** The shim has no tests for a `get_all_collections_meta` failure or `EmbedderNotReadyError`. A `get_all_collections_meta` failure on the all-collections model is unwrapped today (`routes_openai_shim.py` line 216), so it is a plain 500; `EmbedderNotReadyError` is not caught in the shim either, but the app-level handler in `server/app.py` (line 1507) already answers it with a 503. The service translates both: the metadata failure gains defined 503 behavior, the embedder-not-ready behavior is kept and moves into the service; add tests for both.
- **OpenAPI.** `SearchRequest` is part of both OpenAPI snapshots; `tests/contract/test_search_response_shape.py` and `test_mcp_search_response_shape.py` pin response shapes. Both must be updated together with the response unification.
- **Wire text.** Moving error constants must keep `tests/test_sanitized_wire_text_docs.py` green; update docs `600`, `110`, `120`, `140` and `BREAKING.md` in the same change.
- **Characterization gaps.** No test pins MCP versus REST validation order, MCP `ppr` with `collections`, MCP timeout without telemetry, or the unbounded embedder wait on MCP and the shim. Write those first, record the old result, then change it deliberately.
- **Telemetry endpoint labels.** Verified: REST and MCP use the same labels through `from_search_tool_result` (`endpoint="search"`, single path) and `from_search_multi_result` (`search_multi`, multi path). Keep today's labels.

## Resolved Questions

- **Q1 — `top_k`: decided A, make it real.** Add an optional `top_k` to `pipeline.search` and `search_many`, bounded by `top_k_max` (default 100), retrieving at least `top_k` candidates (`top_k_retrieve` defaults to 15). REST and the shim forward it; MCP gains `top_k`; the CLI gains `--top-k`. It is a separate task at the end of the slice, so the rest of S1 is not blocked. It is a core retrieval change: it must pass the eval gate in `tests/eval/`, and `tests/integration/test_s313_top_k_does_not_set_result_count.py` is rewritten, not re-pinned (C13). Note: the eval runner builds its pipelines with a fixed return count and never passes a per-request `top_k`, so the gate shows only that the default is unchanged (team plan Q18); REST's `top_k` schema default of 5 would override an operator's `top_k_return` once `top_k` is real (team plan Q16).
- **Q2 — No collection given: decided A, required everywhere.** MCP loses its configured default and the literal `"default"`. `default_collection` is also used by the MCP tools `ingest_file`, `ingest_directory`, `list_documents`, `delete_document`, `search_with_context` and `explain`; after S1 MCP `search` differs from them until their own slices remove it. Automatic collection choice is a separate future feature (see Future Iterations); today `MultiCollectionRouter` ranks by centroid similarity (optionally blended with the description embedding), not by an LLM, and no search surface uses it.
- **Q3 — First field-level parity check: decided A.** It compares the command fields against the REST request schema, the MCP input schema and the CLI options, with an explicit mapping for the nested REST `filters` versus the flat MCP and CLI filter parameters. This shapes Brief 00's C12 for the remaining slices.
- **Q4 — CLI human-readable output: decided A.** One line per result: rank, score, collection, source path and an excerpt cut at a named constant (about 200 characters, whitespace collapsed). No excerpt-length flag. `--json` prints the REST response body with the full text.

## Future Iterations

- `search_with_context`, `explain` and `route` on the same service (S2-S4).
- A telemetry `origin` dimension (REST, MCP, CLI).
- Making the search budget configurable.
- Automatic collection choice for search: an opt-in way to omit the collection and let the router pick one or more, built once in the service and exposed on REST, MCP and the CLI. It needs decisions on how many collections to search, a confidence cutoff, behavior when descriptions are missing (`[description]` is off by default) and a response field naming the chosen collections. Plan it after S4 (`route`).

## Recommendation

Build it. Q1 and Q2 are decided (see Resolved Questions) and both change the public contract and the characterization tests. Start from the characterization gaps listed above; a pilot that cannot show before and after behavior proves nothing. The hardest part is the response unification (MCP response schema, OpenAPI snapshots, contract tests) and the string-patch re-pointing in the many MCP tests; do not shortcut either. Do not compromise on three things: one implementation per behavior, every drift row resolved or explicitly kept with a reason, and a smoke run that sends the same query through REST, MCP, the shim and the CLI against a real server and compares the results.

## Acceptance (observable)

- The same query returns the same ordered results and the same derived fields through REST, MCP and `archon-search search --json` against a running server.
- Each drift row above is resolved as written or explicitly kept, and every changed behavior has a `BREAKING.md` entry.
- `routes_search.py`, the MCP `search` tool and the shim's search path import no HyDE, budget, validator or telemetry-building code. No test enforces this today: the boundary test's allowlist holds eight edges and none concerns search (`mcp.py` to `routes_explain` belongs to roadmap S3), so there is no allowlist entry for this slice to remove; where the check lives is open (team plan Q23).
- The OpenAI shim's existing contract tests (`test_routes_openai_shim_be5.py`, `_be6.py`, `_be7.py`, `test_openai_shim_e2e.py`) pass, plus the new shim tests for metadata failure and embedder-not-ready.
- The parity registry marks Search as covered on REST, MCP and CLI (its Search row today has `service_method` unset and a pending CLI waiver for this slice), and the default suite, `uvx ruff@0.17.3 check .` and the 85% coverage gate pass.

---

## References

- **Team plan:** [01_search-team-plan.md](./01_search-team-plan.md)
- Roadmap: [roadmap.md](./roadmap.md); contract: [00_service_layer_foundation_brief.md](./00_service_layer_foundation_brief.md)
- `archon_search/server/routes_search.py`, `server/mcp.py`, `server/routes_openai_shim.py`, `services/search_budget.py`, `services/request_hyde.py`, `services/validators.py`
- `archon_search/pipeline.py` (`search`, `search_many`, `SearchPipelineResult`), `archon_search/config.py` (`top_k_max`, `max_fanout`, `top_k_return`), `archon_search/constants.py`
- Tests named in the sections above
