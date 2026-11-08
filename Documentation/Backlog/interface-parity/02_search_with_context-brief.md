# Feature Brief: Search with context on one shared service (REST, MCP, CLI)

**Story Points:** 5 — estimated 2026-11-06 with /estimate

Slice S2 of the [interface parity roadmap](./roadmap.md). It follows the rules C1-C14 of [Brief 00](./00_service_layer_foundation_brief.md) and reuses the pieces slice S1 ([brief](./01_search_brief.md)) extracted into `archon_search/services/`. Decision D2 (roadmap) gave `search_with_context` a REST route and a CLI command.

## Problem

A search hit alone is often a cut-off fragment, and `search_with_context` (hit plus the neighbouring chunks of the same document) exists only as an MCP tool, with its own implementation: the collection is optional (falls back to a configured default), the wait for the embedder is unbounded, the pipeline call has no overall time budget, HyDE runs on a cold embedder, and `graph_mode` is accepted only to be rejected (`server/mcp.py` lines 638-819). REST clients and terminal users cannot get context at all.

## Goal

The same query returns the same hits, the same neighbours, the same derived fields and the same errors on REST, MCP and the CLI, from one `SearchService` code path shared with `search`. The parity registry reports Search with context as covered on all three interfaces.

## Users & Context

- Agents (MCP) that need enough surrounding text to answer without a second call.
- API clients (REST) that today must call `search` and then fetch neighbours themselves.
- Operators who want to inspect a hit in its surroundings from a terminal.

## Core Flow

1. The caller sends a query, exactly one `collection`, an optional `context_window` (default 1, 0 allowed, at most the operator's `[search].context_window_max`), optional `top_k`, filters, `scope_filter`, `include_metadata`, `hyde`, `rag_fusion` and `acl_context`.
2. The service normalises and validates the request with the same rules and order as `search` (blank query or collection is a validation error; an unknown or invalid collection name is `not_found`).
3. The collection's embedder is awaited for at most 0.5 s; if it is still loading, the call answers from keyword search only. A cold cross-encoder is bounded the same way as in `search`. HyDE is skipped when the embedder is cold.
4. The search and the neighbour fetch run together inside one 30-second budget. Neighbours the caller may not read are dropped silently.
5. The caller gets the same response fields as `search` (`acl_filtered`, `embedding_model`, `hyde_applied`, `rag_fusion_*`, `expansion_used`, `expansion_warning`, `applied_filters`); each result carries `context_before` and `context_after`. With `acl_context=true` each hit carries `acl_gate`; neighbours do not.
6. CLI: `archon-search search-with-context "<query>" --collection docs [--context-window N] [--top-k N] ...` prints each hit as `search` does, then its neighbours as indented excerpt lines marked before or after; `--json` prints the REST body in the repo's `--json` format.

## In Scope

- `SearchService.search_with_context` with its own command and outcome types; the steps it shares with `search` (normalisation, collection lookup, embedder selection, warm-up, HyDE resolution, budget, error translation, stage timings) are factored out rather than copied. Telemetry label stays `search_with_context`.
- New REST `POST /search-with-context` with request and response schemas, the same error mapping table as `POST /search` (400, 404, 422, 500, 503 with `Retry-After`, 504).
- New CLI command `search-with-context` in its own module, an HTTP client of the new route; shared request/render helpers move to `cli/_helpers.py` instead of being imported from `cli/search.py`.
- MCP `search_with_context` rewritten as an adapter: `collection` required, `top_k` and `acl_context` added, `graph_mode` removed, response fields as above.
- Deleting what the cutover makes obsolete: the MCP tool's own embedder, HyDE, budget and telemetry logic, the `_SEARCH_TIMEOUT_SECONDS` alias in `mcp.py`, and the context-window cap in the pipeline (a new source for `GET /status.search.context_window_max`).
- Parity registry rows (service method set, REST and CLI waivers removed), field-level parity mapping for the new command, adapter-thinness test extended, OpenAPI snapshots, docs, `BREAKING.md`, a smoke run through the real server on all three interfaces.

## Out of Scope

- `collections` (multi-collection search with context): the neighbour fetch is per collection and no caller needs it yet.
- `graph_mode` on this command: a permanent design decision (`CLAUDE.md`); `search` covers graph retrieval.
- A cap on hits times neighbours, parallel neighbour fetching, a configurable budget: no evidence they are needed yet.
- Reporting neighbours dropped by ACL: unchanged behaviour, documented.
- `acl_gate` on neighbours: neighbours already carry their `acl` list.
- Retrieval, reranking and fusion algorithms: unchanged.

## Key Decisions

- **HyDE on a cold embedder: skipped, as in `search`** (not kept as a context-only exception): one rule on every surface; the cost is a `BREAKING.md` entry for MCP agents.
- **`graph_mode` removed everywhere** (not kept on MCP as a rejected field): the field set stays identical across surfaces and needs no permanent parity waiver; the tool description points agents to `search`.
- **Inputs and response equal `search`'s, plus `acl_context` and `top_k`** (not today's four summary fields): the interface must not decide the feature set (roadmap principle 2); `top_k` matters more here because each hit multiplies the payload.
- **`acl_gate` on hits only**: one gate rule, same as `search`.
- **`top_k_max` and `context_window_max` stay independent** (no product cap): the 30 s budget is the backstop; operators can lower either value.
- **Separate route and command, not a flag on `search`** (`POST /search-with-context`, `search-with-context`): `POST /search` keeps its response shape; matches the MCP tool and the roadmap's "REST new, CLI new".
- **CLI shows neighbours in text output** (not only in `--json`): otherwise the command adds nothing a human can see over `search`.
- Where limits or defaults conflict between interfaces, REST's bounded values win (roadmap D5).

## Edge Cases & Constraints

- **Cold embedder or cold cross-encoder**: keyword-only or un-reranked answer, silently, as in `search` (accepted limitation recorded in the S1 brief).
- **Budget exceeded** (large `top_k` times a large window): `timeout` error (REST 504), nothing partial is returned.
- **`context_window` 0**: hits only, empty `context_before` and `context_after`. Above the cap: validation error naming the bound.
- **Malformed `chunk_id`** (no numeric suffix): that hit gets empty context and a server warning, as today.
- **Neighbour denied by ACL**: dropped silently; the hit stays.
- **Telemetry**: success, timeout and internal error recorded for every interface (MCP records only success today); the structural no-raw-query invariant is untouched.
- **MCP behaviour changes to list in `BREAKING.md`** (derived by comparing today's tool with the S1 service; confirm while writing the characterization tests): `collection` required; bounded embedder wait with keyword-only degrade; bounded cold cross-encoder; 30 s budget and a new `timeout` error; HyDE skipped on a cold embedder; `graph_mode` argument removed; metadata-store failure answers `metadata_store_error` (today `internal_error`); not-found text becomes `collection not found` and an invalid name becomes `not_found`; blank query or collection is a validation error; validation order follows `search`; new response fields and arguments.
- Characterization tests that pin the old behaviour are rewritten, not re-pinned: `tests/test_s391_mcp_search_reranker_score_doc_gap.py`, `tests/test_mcp.py:590` (HyDE on a cold embedder), the `{module}._SEARCH_TIMEOUT_SECONDS` patch in `tests/integration/test_disconnect_cancels_provider_call_e2e.py`.

## Open Questions

- How does FastMCP treat a client that still sends the removed `graph_mode` argument (ignored or a schema error)? Verify and document in the migration note.
- Where does `GET /status.search.context_window_max` read the cap from once the pipeline no longer holds it, and does `SearchStatusDetail` or its snapshot change?
- Neighbour fetch is one store call per hit (`pipeline.py` `search_with_context`): keep inside the service as is, or batch? Measure under the 30 s budget at `top_k_max`.
- Which service layer owns the context-window validation (normaliser, like `top_k`) and its exact error text for REST 422.
- Generalise `tests/parity/field_parity.py` (hard-coded to `search`) or add a second mapping.
- Telemetry label plumbing: `_ENDPOINT = "search"` is hard-coded in `search_service.py`; parameterise it.
- Whether `MCP search_with_context` may still build a service without `config` (it tolerates `config is None` today; `search` does not).

## Future Iterations

- `collections` (multi-collection) on search with context.
- A cap on hits times neighbours, or parallel neighbour fetch, if real use shows timeouts.
- `acl_gate` or an "dropped by ACL" count for neighbours.
- A keyword-only signal in responses (shared with S1's Future Iterations).
- Configurable search budget.

## Recommendation

Build it, and build it right after S1 while the pattern is fresh: the service already has every step this needs, so the slice is mostly extraction plus two thin new adapters. The hardest part is the MCP convergence: eight-plus behaviour changes, each pinned by existing tests and docs that must be rewritten deliberately. Do not compromise on one implementation per behaviour (no copy of `SearchService.search` logic) or on the smoke run that sends one query through REST, MCP and the CLI and compares hits and neighbours.

## Acceptance (observable)

- The same query returns the same hits, neighbours and derived fields through REST, MCP and `archon-search search-with-context --json` against a running server.
- `archon-search search-with-context` text output shows each hit with indented before/after neighbour lines.
- MCP `search_with_context` without `collection` answers a validation error; a request that exceeds 30 s answers `timeout`.
- Every changed MCP behaviour has a `BREAKING.md` entry; the parity registry marks Search with context as covered on all three interfaces; the default suite, `uvx ruff@0.17.3 check .` and the 85% coverage gate pass.

---

## References

- **Team plan:** [02_search_with_context-team-plan.md](./02_search_with_context-team-plan.md)
