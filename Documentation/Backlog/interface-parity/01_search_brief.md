# Feature Brief: Search on one implementation across REST, MCP, OpenAI shim and CLI

Slice S1 of the [interface parity roadmap](./roadmap.md), the pilot. It follows the rules C1-C14 of [Brief 00](./00_service_layer_foundation_brief.md) and is the first consumer of the service layer, so it also implements what Brief 00 deferred: `ServiceError`, the services container, the first service and the adapter error-mapping tables.

## Problem

Search is implemented three times. REST (`server/routes_search.py:301-695`), the MCP `search` tool (`server/mcp.py:520-865`) and the OpenAI shim (`server/routes_openai_shim.py` ~250-345) all call `SearchPipeline.search` or `search_many`, and each re-implements everything around the call. A comparison of the three found 20 behavioral differences (table below), several of them visible to users: an MCP search can block up to 120 seconds on a cold embedder where REST degrades to full-text-only after 0.5 seconds; a `ppr` search over several collections is a 422 on REST and silently runs on MCP; MCP records no telemetry when a search times out. The CLI has no search command at all.

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

1. Normalize and validate the command: stripped non-empty `query`; exactly one of `collection` or `collections`; non-empty, stripped, de-duplicated `collections`; `collections` count within `max_fanout`; `top_k` within bounds; `scope_filter` syntax; `graph_mode` value, `graph` enabled, `scope_filter` and `graph_mode` mutually exclusive, `ppr` not combined with `collections`. This is REST's order today.
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

- `SearchService` (single and multi-collection), `SearchCommand`, `SearchResult`, the first `ServiceError` types and the services container, following C2-C6.
- Rewiring of REST `POST /search`, the MCP `search` tool and the OpenAI shim's search path to the service; deletion of the duplicated code in those three files, including the telemetry-enqueue wrappers, the dependency-error mapping and the `config is None` fallbacks that exist only for the test harness.
- Resolution of every drift in the table below.
- Removal of the duplicated literals: `"search failed"`, `"internal_error"`, the three valid-`graph_mode` definitions, the fan-out and `top_k` messages (also repeated in `routes_explain.py` and the MCP explain tool, which S3 will then reuse), `"collection not found"`, `"Retry-After": "30"`, and the `5`, `100` and `8` limits.
- New CLI command `search` in `cli/search.py`, registered in `cli/main.py`, using the shared CLI helpers from S0.
- MCP `search` gains `acl_context`; REST and MCP return the same response fields; the MCP response schema gains `embedding_model`, `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, `applied_filters` and `acl_gate`.
- The first field-level parity check (see Open Questions).
- Docs: `600`, `110`, `120`, `140`, `520`, `UserManual/60_searching.md`, `UserManual/65_graph_search.md`, the MCP developer guide (`05_mcp_integration`), the new CLI command, `BREAKING.md` plus the migration-guide row.

## Out of Scope

- `search_with_context` (S2), `explain` (S3) and `route` (S4). They reuse the pieces this slice extracts.
- Changes to retrieval algorithms, reranking, fusion or the graph expansion. The only core change allowed is per-request `top_k` if Decision Q1 is accepted.
- The shim's streaming format and its OpenAI envelope; they remain adapter rendering.
- Making `SEARCH_TIMEOUT_SECONDS` configurable (existing `TODO` in `services/search_budget.py`).
- `MultiCollectionRouter`: no search surface uses it today (only `route`, `explain`).

## Key Decisions

Drift table. "Canonical" is what the service does; REST is canonical where it is the more complete and better-tested surface (decision D5).

| # | Drift today (REST / MCP / shim) | Canonical behavior |
|---|---|---|
| 1 | `top_k`: validated then dropped / absent / ignored | See open decision Q1 |
| 2 | No collection: 422 / falls back to a configured default or the literal `"default"` / fans out over all collections | See open decision Q2 |
| 3 | `collection` handling: stripped, empty is 422 / `collection or default`, not stripped / not stripped | Stripped; empty is a validation error |
| 4 | Query: stripped, empty is 422 / unvalidated / blank is 400 and the unstripped text is searched | Stripped, empty is a validation error; the shim keeps its own error envelope and status for "no user message" |
| 5 | Validation order differs | REST's order (Core Flow step 1) |
| 6 | `ppr` with `collections`: 422 / allowed and returns `ppr_entities_matched=0` / n/a | Rejected (the pipeline path for it is a silent fall-through, so allowing it returns a misleading result) |
| 7 | Too many collections: 422 / `validation_error` / silently truncated | Service rejects. The shim keeps truncation as an adapter policy, because the model name `archon-search` means "all collections" and the protocol has no way to ask the caller to narrow; the truncation limit is read from the same `max_fanout` and still logs a warning |
| 8 | Embedder wait: 0.5 s then full-text-only / unbounded (up to 120 s) / unbounded | Bounded wait then degrade (REST). Behavior change for MCP and the shim |
| 9 | Metadata lookup failure on the single path: 503 `metadata_store_error` / generic internal error / 503 envelope | 503-class `metadata_store_error` for all |
| 10 | Timeout telemetry: recorded / not recorded / not recorded | Recorded for every outcome |
| 11 | Stage timings: single path, behind a config flag / same / none | Service records when the flag is on; the shim keeps none unless trivial |
| 12 | `acl_context` and `acl_gate`: supported / absent / absent | Supported in the service; exposed on REST, MCP and CLI; the shim does not expose it |
| 13 | Response echoes (`embedding_model`, `rag_fusion_*`, `applied_filters`): REST only | Computed in the service; present in REST, MCP and CLI `--json` |
| 14 | `include_metadata`: top-level OR `filters.include_metadata` / parameter only | REST's rule, in the service |
| 15 | Multi-path filters: passed as-is / `None` when no filter, otherwise built without `include_metadata` | One filter construction in the service, same on single and multi paths |
| 16-17 | HyDE, RAG Fusion, `graph_mode`, `scope_filter`: supported / supported / absent | Supported in the service; the shim does not expose them |
| 18 | Unknown collection on multi path: 404 / `not_found` / 404 | One `not_found` error, adapters map it |
| 19 | Namespace failure: middleware stamps / `ToolError` when absent / middleware stamps | Adapters resolve the caller; a missing identity is refused before the service is called |
| 20 | `namespace` passed as keyword / positional / keyword | Keyword |

Other decisions:

- **Single versus multi path stays visible.** `collection="x"` runs `pipeline.search` with the per-collection embedder; `collections=["x"]` runs `search_many` with the global embedder and excludes collections whose model differs. This is core behavior, not drift, so the service exposes both and the CLI maps one `--collection` to the first and several to the second.
- **Errors move into the service (C5)**: every `except` in the three search handlers (HyDE and RAG Fusion dependency errors, `GraphCommunitiesNotBuiltError`, `CollectionNotFoundError`, `FanoutTimeoutError` and `SearchBudgetExceeded`, `MetadataLookupError`, `EmbedderNotReadyError`) becomes one translation in the service plus one table per adapter. Existing REST body shapes (plain `ErrorDetail` versus the coded `{"detail":{"code","message"}}`) are preserved unless the table row says otherwise; the `Retry-After: 30` header is set from one constant.
- **MCP filter parameters stay flat; REST's `filters` stays nested.** Both build the same `SearchFilters` inside the service from the same canonical field names. The existing superset guard `tests/server/test_mcp_search.py:262` keeps protecting this.
- **Existing tests that pin drifted behavior are rewritten, not re-pinned** (C13): for example the MCP `ppr` plus `collections` skip in `tests/test_mcp.py:646-670` (its comment claims MCP rejects it before HyDE, which is false), and the stale docstring in `tests/integration/test_mcp_error_paths.py:104-107`. The exact-string integration tests (`test_e0c_be3_validation.py`, `test_e2h_t1_ppr_guard_paths_e2e.py`) must keep passing for REST.

## Edge Cases & Constraints

- **Cold embedder.** REST degrades; `tests/integration/test_s299_prefix_filter_cold_embedder.py` pins that. After S1, MCP and the shim degrade the same way, so an MCP client can receive full-text-only results where it used to wait.
- **Pipeline assertions.** `search_many` asserts that `scope_filter` and `graph_mode` are not combined (`pipeline.py` ~3319-3324, 3407-3410, 3452-3455); the service validates first so the assert stays unreachable.
- **`fts_only` side effect.** In the pipeline, `fts_only` is cancelled when a `query_vector` is given; otherwise it also disables `rag_fusion` and `graph_mode`. The service passes `fts_only` unchanged and must not re-derive it.
- **Graph disabled.** MCP treats `config is None` as graph disabled; REST assumes config is present. The service requires config; the harness fallbacks in `mcp.py` are removed and the MCP tests build a real config.
- **Shim.** The shim has no tests for `get_all_collections_meta` failure or `EmbedderNotReadyError` (neither is wrapped in a try block today). The service translates both, so the shim gains defined 503 behavior; add tests for them.
- **OpenAPI.** `SearchRequest` is part of both OpenAPI snapshots; `tests/contract/test_search_response_shape.py` and `test_mcp_search_response_shape.py` pin response shapes. Both must be updated together with the response unification.
- **Wire text.** Moving error constants must keep `tests/test_sanitized_wire_text_docs.py` green; update docs `600`, `110`, `120`, `140` and `BREAKING.md` in the same change.
- **Characterization gaps.** No test pins MCP versus REST validation order, MCP `ppr` with `collections`, MCP timeout without telemetry, or the unbounded embedder wait on MCP and the shim. Write those first, record the old result, then change it deliberately.
- **Telemetry endpoint labels.** `from_search_tool_result` is used by both REST and MCP; whether the `endpoint` label differs per interface was not verified [unverified]. Keep today's labels unless a test says otherwise.

## Open Questions

- **Q1 — `top_k` (needs your decision).**
  - Option A: make `top_k` real. Add an optional `top_k` to `pipeline.search` and `search_many`, bounded by `top_k_max`, retrieving at least `top_k` candidates (`top_k_retrieve` defaults to 15, `top_k_max` to 100). Pro: search is feature-ready. Con: it is a core retrieval change; it must pass the eval gate in `tests/eval/`, and `test_s313` changes meaning.
  - Option B: remove `top_k` from REST and the shim and do not add it to MCP or the CLI; operators set `[database].top_k_return`. Pro: smallest and honest. Con: breaks REST clients that send it, and there is no per-request control.
  - Recommendation: A, as a separate task at the end of the slice, so the rest of S1 is not blocked by it.
- **Q2 — No collection given (needs your decision).** Option A: `collection` or `collections` is required everywhere (REST's rule); MCP loses its configured default and the literal `"default"` (a hardcode). Option B: keep a default, implemented once in the service from one config key, available on every interface. Recommendation: A. A default that depends on the interface contradicts the goal, and `AGENTS.md` and your coding rules prefer clean cutover over backward compatibility.
- Whether the first field-level parity check should compare command fields against the REST request schema, the MCP input schema and the CLI options, given the flattening of `filters`. The answer shapes Brief 00's C12 for the remaining slices.
- Exact CLI excerpt length and layout for the human-readable output; the `--json` output is the REST response body.

## Future Iterations

- `search_with_context`, `explain` and `route` on the same service (S2-S4).
- A telemetry `origin` dimension (REST, MCP, CLI).
- Making the search budget configurable.

## Recommendation

Build it, with Q1 and Q2 answered first, because both change the public contract and the characterization tests. Start from the characterization gaps listed above; a pilot that cannot show before and after behavior proves nothing. The hardest part is the response unification (MCP response schema, OpenAPI snapshots, contract tests) and the string-patch re-pointing in the many MCP tests; do not shortcut either. Do not compromise on three things: one implementation per behavior, every drift row resolved or explicitly kept with a reason, and a smoke run that sends the same query through REST, MCP, the shim and the CLI against a real server and compares the results.

## Acceptance (observable)

- The same query returns the same ordered results and the same derived fields through REST, MCP and `archon-search search --json` against a running server.
- Each drift row above is resolved as written or explicitly kept, and every changed behavior has a `BREAKING.md` entry.
- `routes_search.py`, the MCP `search` tool and the shim's search path import no HyDE, budget, validator or telemetry-building code, and the boundary test's allowlist has lost the entries this slice resolved.
- The OpenAI shim's existing contract tests (`test_routes_openai_shim_be5.py`, `_be6.py`, `_be7.py`, `test_openai_shim_e2e.py`) pass, plus the new shim tests for metadata failure and embedder-not-ready.
- The parity registry marks Search as covered on REST, MCP and CLI, and the default suite, `uvx ruff@0.17.3 check .` and the 85% coverage gate pass.

---

## References

- Roadmap: [roadmap.md](./roadmap.md); contract: [00_service_layer_foundation_brief.md](./00_service_layer_foundation_brief.md)
- `archon_search/server/routes_search.py`, `server/mcp.py`, `server/routes_openai_shim.py`, `services/search_budget.py`, `services/request_hyde.py`, `services/validators.py`
- `archon_search/pipeline.py` (`search`, `search_many`, `SearchPipelineResult`), `archon_search/config.py` (`top_k_max`, `max_fanout`, `top_k_return`), `archon_search/constants.py`
- Tests named in the sections above
