**Purpose**: Catalog known technical debt in `archon-search`, with severity, triggers, and refactor candidates, so maintainers can decide what to pay down and when.
**Audience**: Maintainers and backend engineers.
**Status**: Accepted
**Last reviewed**: 2026-05-20 / **Next review**: 2026-08-20

# Technical Debt and Refactoring Roadmap

This document is the canonical register of accepted compromises in `archon-search`. It complements the per-release [`BREAKING.md`](../../BREAKING.md) (which records contract changes that already shipped or are queued) by tracking debt that has **not yet been scheduled**.

For architectural context, see [`100_system_architecture_overview.md`](./100_system_architecture_overview.md). For the security and privacy invariants debt is measured against, see [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md). For the release flow that ships paydowns, see [`510_release_and_environment_strategy.md`](./510_release_and_environment_strategy.md).

## Principles

1. **Debt is a forecast, not a moral failing.** Every entry below was a deliberate trade-off at the time. The register exists to surface *triggers* — the conditions under which a trade-off stops paying.
2. **Every entry has a trigger and a reference.** If an item cannot point to a file path, a `BREAKING.md` entry, or a documented invariant, it does not belong here. Ungrounded debt is rumor.
3. **The eval harness is the regression gate.** Debt that bypasses [`tests/eval/`](../../tests/eval/README.md) — for example, a refactor without a metric to defend it — is high-risk by default. See [`200_testing_strategy.md`](./200_testing_strategy.md).
4. **Bias toward shipping over polishing absent measurable user pain.** A debt item without a trigger event (user report, eval regression, security finding) stays Low severity.
5. **Out-of-scope items are recorded, not hidden.** Deliberate non-goals (single-process server, no horizontal scale) live in [Out of scope](#out-of-scope) so they are not re-litigated as debt.

## Debt register

### API contract drift

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| API-1 | MCP `search` tool response shape change (bare list → `{results, acl_filtered}`) is announced but not yet released. Consumers may still depend on the old shape. | API contract | Med | Tag pushed before consumers migrate. | [`BREAKING.md`](../../BREAKING.md) "[next release] — MCP `search` tool response shape"; `archon_search/server/mcp.py` |
| API-2 | REST `/search` `top_k` field still accepted by the Pydantic schema but ignored at the route. The schema documents a parameter that has no effect. | API contract | Med | Confused user report or external integration relying on per-request `top_k`. | [`BREAKING.md`](../../BREAKING.md) "[next release] — REST `/search` per-request `top_k` no longer honored"; `archon_search/server/routes_search.py`, `schemas.py` |
| API-3 | MCP-mirrors-REST aspiration is partial. MCP exposes 17 tools (as of D7: 13 original + `create_key`, `list_keys`, `revoke_key`, `rotate_key`); REST also surfaces `state`, `status`, `route`, `jobs` (list + resume), `telemetry`, and collection `reindex`/`add`/`remove`. The `GET /jobs` list and `POST /jobs/{id}/resume` endpoints have no MCP mirror yet. The naming gap compounds the drift. **D9 (shipped)** mounted the MCP HTTP app at `/mcp` on the REST port — the transport is now reachable on the shipped server and namespace-correct, but the tool-set asymmetry above is unchanged. See `Documentation/Completed/mcp-wiring-team-plan.md`. | API contract | Low | New REST endpoint added; downstream MCP client requests parity. | `archon_search/server/mcp.py`, `archon_search/server/app.py`, [`520_api_design_and_contracts.md`](./520_api_design_and_contracts.md) |
| ~~API-4~~ | ~~MCP tools return raw `dataclasses.asdict(...)` payloads without a Pydantic response model.~~ **Resolved in C7.** All 11 MCP tools now validate return values through explicit Pydantic schemas in `archon_search/server/mcp_schemas.py` (`extra='forbid'`). Schema drift surfaces as `{"error": "...", "code": "schema_validation_error"}`. See `BREAKING.md` C7 entries and `mcp_schemas.py`. | API contract | ~~Med~~ Resolved | — | `archon_search/server/mcp_schemas.py`, `archon_search/server/mcp.py` |
| API-5 | REST error responses are inconsistent: most failures raise `HTTPException` (FastAPI-serialized `{"detail": ...}`), but `routes_search.py` and `routes_collections.py` mix in hand-built `JSONResponse({"detail": ...}, status_code=...)`. Same shape today by convention, but the two paths can diverge silently. (A3 removed the mix from the pipeline-failure and timeout paths; the meta-lookup 503 and 404 paths at `routes_search.py:86-93` still use `JSONResponse`.) | API contract / Reliability | Low | Error envelope is extended (e.g., add `code` field) in one path and not the other. | `archon_search/server/routes_search.py:86-93`, `routes_collections.py`, `routes_jobs.py`; [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) |

### Privacy and security

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| ~~SEC-1~~ | ~~Auth middleware supports a single default key plus an optional `namespaces` map of static keys. There is no rotation, expiry, or revocation primitive — restart with a new key file is the only path.~~ **Resolved by D7**: `KeyStore` in `key_manager.py` adds durable multi-key store with `create`, `revoke`, `list_keys`, `active_keys`, `rotate_default_key`. `POST /keys/rotate`, `DELETE /keys/{id}`, `archon-search key rotate/revoke` provide live rotation and revocation without server restart. | Security | ~~Med~~ **Resolved** | — | `archon_search/key_manager.py`, `archon_search/server/routes_keys.py`, `archon_search/cli/key_cmd.py`; [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) |
| ~~SEC-2~~ | ~~Telemetry `doc_id` is path-derived. With telemetry enabled, the JSONL log under `~/.archon-search/search-logs/` reveals filesystem paths. Documented as accepted risk; a hashed-doc-id mode is planned.~~ **Resolved by D8**: `[telemetry] hash_doc_ids = true` enables HMAC-SHA256 hashing of `result_doc_ids` before JSONL write. Salt stored at `get_data_dir()/.telemetry-salt` (mode 0600). `doc_ids_hashed: bool` field in every entry. `GET /status` exposes `telemetry.hash_doc_ids_enabled`. See `archon_search/telemetry/hasher.py`, ADR-05 Amendment. | Security | ~~Med~~ **Resolved** | — | `archon_search/telemetry/hasher.py`, `archon_search/telemetry/entry.py`, `archon_search/server/routes_status.py`; ADR [`05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md) |
| SEC-3 | Telemetry retains the structural no-raw-query invariant; enforcement is backed by a dedicated structural test (`tests/telemetry/test_entry_factories.py::test_factory_signatures_reject_raw_query_argument`) that introspects every `TelemetryEntry` factory and forbids `{"query", "query_text", "body", "request"}` kwargs. The residual debt is that the invariant lives outside the type system — a maintainer who bypasses the factories (e.g., constructs `TelemetryEntry` directly with a renamed field) would not be caught. | Security | Low | New telemetry field requested for debugging that does not go through the factories. | `archon_search/telemetry/entry.py`; `tests/telemetry/test_entry_factories.py`; [`CLAUDE.md`](../../CLAUDE.md) "Structural invariant" |
| CORS-1 | FastAPI `CORSMiddleware` is mounted with `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]` and no config knob. Because the middleware short-circuits OPTIONS preflight **before** `APIKeyMiddleware` runs, CORS is the only layer governing whether arbitrary remote origins can attempt cross-origin requests on a non-loopback bind. Acceptable while the server binds to `127.0.0.1` only; becomes a CSRF/exfiltration surface as soon as the bind moves off loopback without a reverse proxy that overrides CORS headers. | Security | Low | Bind address moves off `127.0.0.1` without a hardening reverse proxy. | `archon_search/server/app.py:122`; [`Documentation/SecurityGuide/05_network_exposure_and_tls.md`](../SecurityGuide/05_network_exposure_and_tls.md); [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) |

### Telemetry / config contract

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| TEL-1 | `[telemetry].export_enabled = true` is **silently coerced to `false` with a warning**, not rejected at config load. Code and [`CLAUDE.md`](../../CLAUDE.md) agree on the silent-coerce behavior; the residual debt is that this is a warn-and-continue path rather than a hard config-load failure, and downstream docs/ADRs that describe "external transmission" semantics need to be checked against the actual no-op v1 implementation. #Unverified — ADR-05 wording was not re-opened during this audit. | Reliability / Docs | Med | User reports unexpected silent demotion, or contract is cited in an audit. | `archon_search/config.py` lines 209–217; [`CLAUDE.md`](../../CLAUDE.md) "Telemetry"; [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) |

### Platform

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| PLT-1 | Windows service lifecycle is a stub. Every method on `WindowsSearchService` (`start`, `stop`, `restart`, `register`, `unregister`) raises `NotImplementedError`; only `status()` returns a `not running` shape. CLI `install`/`start`/`stop` on Windows surface the `NotImplementedError`. | Architecture | Low | First Windows user request, or a CI runner added for `win32`. | `archon_search/platform/windows.py`; `archon_search/platform/service.py` |
| PLT-2 | CI runs only on `ubuntu-latest`. macOS branches are exercised via `patch("sys.platform", "darwin")` mocks; the Windows branch is the explicit stub (PLT-1) and has no native test. The launchd/systemd integration paths are never exercised against the real OS in CI. | Testing / Reliability | Low | Native-OS regression that mocks miss (e.g., a `launchctl` flag change). | `.github/workflows/`, `archon_search/platform/{macos,linux,windows}.py`, `tests/test_service_*.py` |
| PLT-3 | `install_cmd.py` invokes `launchctl`/`systemctl` with `subprocess.run(..., check=False, capture_output=True)` and discards the result. If the helper is missing or fails, the uninstall path silently no-ops with no warning logged. | Reliability | Low | User reports an install/uninstall that "did nothing" and there is no log to triage. | `archon_search/cli/install_cmd.py` (uninstall path) |

### Concurrency and runtime state

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| CON-1 | `archon_search/parser.py:_parse_with_docling` lazy-initialises `self._converter` with an acknowledged check-then-set race (in-code comment: "NOT THREAD SAFE … current RAG pipeline is sequential, so this is an accepted limitation"). The risk is dormant only as long as ingest stays single-threaded. | Reliability | Low | Parallel ingest path added (e.g., `asyncio.gather` over images). | `archon_search/parser.py` `_parse_with_docling` |
| CON-2 | **Addressed by A6.** `MultiCollectionRouter` now exposes `invalidate()` (clears `_cached_metadata`; idempotent) and an `initial_metadata` constructor param; the eval harness injects metadata via the constructor instead of writing the private `_cached_metadata`. The originally documented "stale centroids until restart" symptom does **not** occur in the FastAPI `/route` path, which builds a fresh router per request (`routes_route._build_router`) — now pinned by a regression test. `invalidate()` ships as pre-emptive API for a future shared-router migration; the in-flight-fetch TOCTOU residual is documented on the method. | Reliability | Med (resolved) | A future shared, long-lived router instance must call `invalidate()` after collection mutations or the stale-centroid symptom reappears. | `archon_search/router.py` (`invalidate`, `initial_metadata`, `_cached_metadata`, `fetch_metadata`); [`210_performance_and_scalability.md`](./210_performance_and_scalability.md) |
| CON-3 | **Closed by A6.** `IndexingStateStore` (`progress.py`) is now thread-safe: `write` / `update_collection` / `remove_collection` / `set_trigger` and the new `reset_in_progress(predicate)` are serialized by an internal `threading.RLock`; `read()` is an unlocked snapshot. The cross-collection lost-update race on the shared `.indexing_state.json` is gone — concurrent collection writers no longer clobber each other. `sync._reset_stale_in_progress` now delegates to the locked `reset_in_progress(...)` (no more call-site read→modify→write). **Durability under power-loss is still open** (owned by A7/fsync); A6 fixed consistency only. | Reliability | Med (resolved; durability tracked by A7) | Two collections sync concurrently; previously one writer overwrote the other's update — now serialized. | `archon_search/progress.py` (`IndexingStateStore.write`, `update_collection`, `remove_collection`, `set_trigger`, `reset_in_progress`); `archon_search/sync.py` (`_reset_stale_in_progress`) |
| CON-4 | **Resolved by B5.** Three concrete defects existed: (1) ingest performed a batch-only centroid overwrite — the centroid was recomputed from all vectors on every ingest batch, costing O(chunks); (2) delete did not update the centroid at all, leaving the routing centroid stale after deletions; (3) the watcher-sync hot path triggered a full O(chunks) rescan on every sync cycle. B5 fixes all three: ingest is now O(batch) via `(centroid_sum, chunk_count)` incremental maintenance; delete updates the centroid O(chunks-in-document); the O(chunks) full scan is retained only for explicit `recompute_collection_meta` calls (reindex, crash recovery, periodic drift reset). The `centroid_incremental_enabled` feature flag (removed in D4) was the escape hatch; it is no longer needed. | Performance | Med (resolved) | Reopen if incremental maintenance introduces centroid drift beyond the accepted threshold; trigger is `needs_recompute = True` on more than a configurable fraction of collections. | `archon_search/store.py` (`_meta_schema`, `update_collection_meta`, `delete_document`); `archon_search/pipeline.py` (`recompute_collection_meta`); [`210_performance_and_scalability.md`](./210_performance_and_scalability.md) |
| CON-5 | **Resolved in A3**: `routes_search.py` previously returned **200 OK with empty `results`** when a pipeline stage raised an exception — pipeline failure was logged at `warning` and silently downgraded to `SearchResponse(results=[], acl_filtered=False)`, so clients could not distinguish "no hits" from "search broke". Now returns HTTP 500 on pipeline stage failure (bare re-raise) and HTTP 504 on timeout; telemetry is emitted on both failure paths. (resolved in A3; see [`BREAKING.md`](../../BREAKING.md) `[next release]` — `POST /search` pipeline-exception behavior) | Reliability / API contract | ~~Med~~ Done | Would have caused silent empty-result regressions before the A3 fix — a downstream LanceDB/embedder fault produced user-visible recall drops with no error signal. | `archon_search/server/routes_search.py`; [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) |

### Eval and CI

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| EVL-1 | The eval harness uses deterministic, label-blind, SHA-256-derived embedder and reranker backends (`EvalEmbedderBackend`). Latency p50/p95 thresholds are a regression guard, not a production SLA, and recall/NDCG are computed against stub scoring — production-model evaluation is **not** in CI. | Testing | High | Production-model regression that the harness misses; any change to ranking heuristics. | `archon_search/eval/backends.py`; [`tests/eval/README.md`](../../tests/eval/README.md); [`210_performance_and_scalability.md`](./210_performance_and_scalability.md) |
| EVL-2 | Eval fixtures (`documents.jsonl`, `queries.jsonl`, `labels.jsonl`) and `thresholds.toml` evolve by waiver. The maintenance burden grows with corpus size and there is no automated drift check between fixture corpus and production-shape corpora. | Testing | Low | Fixture set grows past the point a human can review threshold deltas confidently. | `tests/eval/README.md` ("threshold-lowering policy, waivers") |
| TLG-1 | `--cov-fail-under=85` is enforced only on the default `pytest` invocation. Split-CI matrices must `coverage combine` before applying the threshold; the discipline is convention, not a check. A future split-CI workflow that forgets to combine silently reports false-green coverage. | Tooling | Med | New CI matrix added (OS, Python version) without a combine step. | `pyproject.toml` (`addopts` comment lines 55–61); [`CLAUDE.md`](../../CLAUDE.md) "Repository conventions"; [`200_testing_strategy.md`](./200_testing_strategy.md) |

### Schema versioning

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| SCH-1 | The LanceDB metadata table has no `schema_version` column. Column-absent rows are tolerated at read time by treating missing columns as `None` (e.g., `description_embedding` added in B4, previously `acl`, `centroid`, `description`). Each new optional column adds another "absent = None" tolerance site with no version invariant to gate on. B4 adds one more such site (`description_embedding`); B5 (incremental centroid `sum`/`count`) and B6 may add more. Once two or more columns are absent-tolerant simultaneously, a `schema_version` marker becomes a prerequisite for safe migration ordering and for detecting partially migrated stores. | Architecture / Data | Low | B5 or B6 ship a new column; the absent-column list exceeds two entries; or a store-migration bug silently leaves a store in an intermediate state with no detectable version signal. | `archon_search/collection_meta.py` (`description_embedding`), `archon_search/store.py` (metadata table reads); `Documentation/Architecture/130_data_architecture_and_persistence.md` |

### Internal architecture

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| ARCH-1 | Two parallel domain-types modules: `archon_search/_types.py` (search/ingest results) and `archon_search/types.py` (jobs/queries/collection facade). Importers must remember which symbols live where, and there is no rule that distinguishes them. Consolidation is low-risk (incremental re-export). | Architecture / Docs | Low | New domain type lands in the wrong module and triggers an import-shuffle PR. | `archon_search/_types.py`, `archon_search/types.py`; [`110_component_catalog_and_layer_breakdown.md`](./110_component_catalog_and_layer_breakdown.md) |
| ARCH-2 | **Resolved in C9 (Task 1.1).** `load_config()` now reads `ARCHON_SEARCH_HOST` (any non-empty string) and `ARCHON_SEARCH_PORT` (int 1–65535) env vars; precedence is env > TOML > default. The `FileNotFoundError` early-return was converted to a fall-through so env vars apply even when no TOML file is mounted (the standard container path). See `archon_search/config.py` (`load_config`, `_apply_env_overrides`) and `tests/test_config_env_overrides.py`. | Operations / Architecture | ~~Low~~ Done | — | `archon_search/config.py` (`_apply_env_overrides`); [`160_operational_readiness_monitoring_and_reliability.md`](./160_operational_readiness_monitoring_and_reliability.md) |
| ARCH-2a | `load_config(path, *, serve: bool = False)` accepts a CLI-layer concern (`serve` mode flips the host default to `0.0.0.0` before TOML/env processing) in the config layer. This is an intentional short-cut for C9 to keep the foreground/container path one line; the cleaner alternative is a sentinel for "was this value explicitly set?" applied by the caller after `load_config()` returns. | Architecture / Config | Low | Another deployment mode needs a different host default (e.g., systemd socket activation), forcing a second kwarg. | `archon_search/config.py` (`load_config` `serve` kwarg); `archon_search/cli/serve.py` (planned in C9 Task 3.1) |
| ARCH-3 | **Resolved in B1 (2026-05-26).** `RequestContextMiddleware` (`server/middleware_context.py`) now mints or validates an `X-Request-ID` on every HTTP request and writes the correlation ID into a `ContextVar`. Structured log lines emitted by `/search`, `/route`, `/explain`, and the MCP tools carry `correlation_id`; the `TelemetryEntry` schema also records it. `bind_stage_recorder()` + `record_stage()` accumulate per-stage wall-time timings available via `StageRecorder.stage_timings_ms`. See `archon_search/observability.py`. | Observability | ~~Low~~ Done | — | `archon_search/observability.py`, `archon_search/server/middleware_context.py`; [`160_operational_readiness_monitoring_and_reliability.md`](./160_operational_readiness_monitoring_and_reliability.md) |
| ARCH-4 | Search-execution config keys are split across two TOML sections. `top_k_retrieve` / `top_k_return` (which also drive the multi-collection per-leg `candidate_depth = max(top_k_retrieve * 3, 20)`) live under `[database]`, while B3's fan-out keys (`max_fanout`, `fanout_leg_trim`, `fanout_timeout_seconds`) live under `[search]`. Operators tuning fan-out recall/latency must edit two sections, and the split obscures that `[database]` keys govern retrieval behavior, not storage. A future cleanup should migrate all search-execution parameters under `[search]` (with back-compat reads from `[database]`). | Architecture / Config | Low | Operator confusion tuning fan-out, or a new search-execution key lands in the "wrong" section. | `archon_search/config.py` (`[database]` block parsing `top_k_*`, `[search]` block parsing `max_fanout`/`fanout_leg_trim`/`fanout_timeout_seconds`); [`210_performance_and_scalability.md`](./210_performance_and_scalability.md) |

### CLI performance

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| CLI-1 | ~~**RESOLVED (feature 210):** `collection list` and `collection info` now open `SearchStore` directly via `_make_store()` instead of calling `create_pipeline()`, eliminating the GPT-2 tokenizer cost (~1 s). The remaining floor is the `lancedb` first-import (~900 ms). Lazy chunker init in `SearchPipeline` is still deferred (wider blast-radius) but no longer blocks these commands.~~ | Performance | Resolved | — | `archon_search/cli/collection.py` (`_make_store`, `list_cmd`, `info`); [2026-07-15-210-cli-store-commands-slow-brief.md](../../Documentation/Completed/2026-07-15-210-cli-store-commands-slow-brief.md) |

### Query expansion provider matrix

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| ~~QE-1~~ | ~~HyDEGenerator and RAGFusionGenerator were coupled to a single provider (`AnthropicQueryExpansionProvider`) — operators had no way to use a local or alternative LLM without patching source code.~~ **Resolved in G10.** `QueryExpansionProvider` protocol defined in `query_expansion_protocol.py`; `OllamaQueryExpansionProvider` (BE-3) and `OpenAIQueryExpansionProvider` (BE-6) added in `archon_search/providers/`; `[hyde].provider` / `[rag_fusion].provider` TOML fields route to the correct adapter at startup; wizard (`archon-search wizard`) prompts for provider, model, and Ollama base URL; `ConfigError` raised at startup when the required package is missing; `GET /status` exposes `hyde.provider` and `rag_fusion.provider`. Setting `provider = "ollama"` achieves zero-transmission operation (query text never leaves the host). | Architecture / Ops | ~~Med~~ **Resolved** | — | `archon_search/query_expansion_protocol.py`; `archon_search/providers/`; `archon_search/config.py` (`HyDEConfig.provider`, `RAGFusionConfig.provider`); `archon_search/server/app.py` (`_check_provider_deps`, `_build_query_expansion_provider`); `archon_search/install/config_writer.py` (`WizardFeatures`); `archon_search/server/schemas.py` (`HydeStatusDetail.provider`, `RagFusionStatusDetail.provider`) |

### Graph retrieval

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| ~~GRAPH-1~~ | ~~Naive graph expansion (`graph_mode="naive"`) had no upper bound on the number of first-degree neighbour names appended to the query. Very high-degree graph nodes ("god" entities connected to hundreds of nodes) produced unbounded FTS queries, degrading precision and increasing latency.~~ **Resolved in E2h BE-8.** `GraphExpander` now caps the neighbour-name list at `[graph].naive_max_expansion_terms` (default `20`) before deduplication. See `BREAKING.md` "[next release] — E2h BE-8: naive graph expansion is now capped". | Performance / Reliability | ~~Med~~ **Resolved** | — | `archon_search/graph_expander.py` (`_naive_max_expansion_terms`); `archon_search/config.py` (`GraphConfig.naive_max_expansion_terms`); `BREAKING.md` E2h BE-8 entry |

### Documentation

| ID | Item | Category | Severity | Trigger | Refs |
| --- | --- | --- | --- | --- | --- |
| DOC-1 | The MCP tool surface is documented in three places (`mcp.py`, `CLAUDE.md`, and the API reference at [`600_api_reference_or_public_interface.md`](./600_api_reference_or_public_interface.md)). As of D1/D2 all three agree on the 13 tool names. The residual debt is that this consistency is convention, not enforced — any new MCP tool added to `mcp.py` without updating `CLAUDE.md` and the API reference re-introduces the drift. | Docs | Low | New MCP tool added without updating `CLAUDE.md` and the API reference. | `archon_search/server/mcp.py`; [`CLAUDE.md`](../../CLAUDE.md) "MCP tools" |
| DOC-2 | **E2a: `scope_filter` validation is triplicated** — `_check_scope_filter` is implemented independently in `routes_search.py:39`, `routes_explain.py:53`, and a parallel `_validate_scope_filter` in `mcp.py:128`. All three enforce the same regex-based rules (reject bare `*`, leading `*`, mid-string `*`, multiple `*`). Any change to the validation rule (e.g., new allowed characters, max length) must be applied in all three locations — a missed site silently creates an inconsistency between REST and MCP. Extract to a shared `_validate_scope_filter(value: str) -> None` utility in `store_filters.py` or a new `archon_search/server/_validators.py`. | Code Quality | Low | Validation rule change required (e.g., allow `.` in scope names, max-length constraint added). | `archon_search/server/routes_search.py:39`; `archon_search/server/routes_explain.py:53`; `archon_search/server/mcp.py:128` |

### In-code debt markers

A repository-wide `grep -RIn "TODO\|FIXME\|XXX\|HACK" archon_search/` returns one match as of 2026-05-22: `archon_search/server/routes_search.py:15` — `# TODO: make configurable via config.py (see /route for parity)` — for the `_SEARCH_TIMEOUT_SECONDS = 30.0` constant (added in A3). This is ARCH-2-adjacent (no env override for a timeout constant). The only in-code self-warning predating A3 is the `# NOT THREAD SAFE` comment in `parser.py` captured as CON-1 above. The items above otherwise derive from documented contracts, configuration coercion, stub modules, or runtime patterns surfaced by audit rather than from `TODO`-style markers.

## Prioritization matrix

```mermaid
quadrantChart
    title Debt prioritization — impact vs. effort
    x-axis Low effort --> High effort
    y-axis Low impact --> High impact
    quadrant-1 Plan deliberately
    quadrant-2 Pay down soon
    quadrant-3 Defer
    quadrant-4 Quick wins
    "API-1 MCP search shape": [0.2, 0.55]
    "API-2 top_k ignored": [0.25, 0.5]
    "API-3 MCP/REST gap": [0.7, 0.45]
    "API-4 MCP unvalidated dicts": [0.4, 0.6]
    "API-5 error envelope mix": [0.2, 0.3]
    "SEC-1 key rotation ✅": [0.75, 0.7]
    "SEC-2 doc_id hashing ✅": [0.55, 0.6]
    "SEC-3 telemetry invariant test": [0.2, 0.45]
    "CORS-1 wildcard CORS": [0.15, 0.4]
    "TEL-1 export_enabled coerce": [0.15, 0.5]
    "PLT-1 Windows stub": [0.85, 0.3]
    "PLT-2 single-OS CI": [0.7, 0.35]
    "PLT-3 silent subprocess": [0.15, 0.2]
    "CON-1 parser race": [0.2, 0.3]
    "CON-2 router cache stale": [0.3, 0.65]
    "CON-3 state-store race": [0.3, 0.55]
    "CON-4 centroid recompute ✅": [0.55, 0.55]
    "CON-5 search 200-on-error ✅": [0.2, 0.6]
    "ARCH-1 dual types modules": [0.3, 0.3]
    "ARCH-2 host:port env": [0.15, 0.3]
    "ARCH-3 request IDs ✅": [0.4, 0.4]
    "ARCH-4 config section split": [0.3, 0.25]
    "EVL-1 prod-model eval": [0.8, 0.85]
    "EVL-2 fixture drift": [0.55, 0.35]
    "TLG-1 split-CI coverage": [0.35, 0.55]
    "DOC-1 CLAUDE.md MCP names": [0.1, 0.25]
```

Resolved by A6: **CON-2** (router cache invalidation API + per-request lifecycle pinned), **CON-3** (state-store RLock; durability still tracked under A7). Resolved by B1: **ARCH-3** (correlation ID + stage-latency surface). Resolved by B5: **CON-4** (incremental centroid maintenance; three defects fixed). Resolved by C9: **ARCH-2** (host/port env overrides), and the C9 brief's separate "ARCH-3" relocatable-path-root item (delivered as `paths.get_data_dir()` + lazy accessors in `key_manager`, `jobs`, `language_detector`, `cli/ingest.py`, and `config.load_config()`). Quick wins (low effort, mid-to-high impact): **TEL-1**, **DOC-1**, ~~**CON-5**~~ (resolved in A3), **API-1/API-2** (already on the next-release queue). Plan deliberately (high effort, high impact): **EVL-1**, ~~**SEC-1**~~ (resolved by D7), ~~**SEC-2**~~ (resolved by D8), **API-3**, **API-4**. Defer until a trigger fires: **PLT-1**, **PLT-2**, **PLT-3**, **CON-1**, **EVL-2**, **ARCH-1**.

## Planned refactors

- **Decide whether `export_enabled = true` should hard-fail at config load** (TEL-1). Current behavior (warn + coerce to `false`) is what `CLAUDE.md` describes; if ADR-05 prescribes "reject at load", reconcile in a single PR.
- **Ship the queued breaking changes** (API-1, API-2). Both are already in [`BREAKING.md`](../../BREAKING.md); they paydown on the next tagged release via [`release.sh`](../../release.sh).
- ~~**Hashed `doc_id` mode for telemetry** (SEC-2). Track in [`Backlog/`](../Backlog/) once a concrete request arrives; ADR-05 is the design anchor.~~ **Resolved by D8** — `[telemetry] hash_doc_ids = true` applies HMAC-SHA256 to `result_doc_ids` before JSONL write. See `archon_search/telemetry/hasher.py` and ADR-05 Amendment.
- **Production-model eval lane in CI** (EVL-1). Likely a `live`-marker job that runs on tag pushes only, gated by [`tests/eval/thresholds.toml`](../../tests/eval/thresholds.toml). See the long-form plan in [`Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) (graph track E2b–E2j complete as of 2026-07-12).
- ~~**Multi-key auth with rotation** (SEC-1). Builds on the existing `namespaces` map in `middleware_auth.py`; needs a key-file format that supports expiry and revocation.~~ **Resolved by D7** — see `archon_search/key_manager.py` (`KeyStore`), `routes_keys.py`, `cli/key_cmd.py`.
- **Lint/test that gates MCP tool name parity across `mcp.py`, `CLAUDE.md`, and the API reference** (DOC-1). Pair with API-3 review.
- ~~**Decide search-failure semantics** (CON-5). Either propagate a 5xx with the standard error envelope, or document that empty `results` + `acl_filtered=false` is the failure signal. Whichever wins, encode it in `routes_search.py` and a test.~~ **Done in A3** — pipeline exceptions now return HTTP 500 / 504; see [`BREAKING.md`](../../BREAKING.md) `[next release]` — `POST /search` pipeline-exception behavior.
- ~~**Invalidate `MultiCollectionRouter._cached_metadata` after collection mutations** (CON-2).~~ **Addressed by A6**: `invalidate()` and `initial_metadata` shipped, the eval path no longer writes the private cache, and the FastAPI per-request router lifecycle is pinned by a regression test. Wiring `invalidate()` into a future shared, long-lived router is the remaining follow-up.
- ~~**Close the cross-collection race on `.indexing_state.json`** (CON-3).~~ **Closed by A6**: `IndexingStateStore` now serialises all mutating methods with an internal `threading.RLock`, and `sync._reset_stale_in_progress` delegates to the locked `reset_in_progress(...)`. Durability under power-loss (fsync) remains open and is tracked under A7.
- **Wrap MCP responses in Pydantic models** (API-4). Reuse the REST `response_model` schemas (`SearchResponse`, `CollectionMeta`, etc.) so that MCP and REST share the validation gate.
- ~~**Incremental centroid update** (CON-4).~~ **Resolved by B5**: ingest is O(batch) via `(centroid_sum, chunk_count)` incremental maintenance; delete updates the centroid O(chunks-in-document); O(chunks) full scan retained only for explicit `recompute_collection_meta` (reindex, crash recovery, drift reset). The `centroid_incremental_enabled` escape-hatch flag was removed in D4; the incremental path is now unconditional.

## Out of scope

The following look like debt but are deliberate design choices. They are recorded here so they are not re-opened without a new ADR.

- **Single-process local server, no horizontal scale.** Runtime state under `~/.archon-search/` is local-only; LanceDB is opened by a single process. See ADR [`01_lancedb_as_local_vector_store.md`](../ADRs/01_lancedb_as_local_vector_store.md) and [`210_performance_and_scalability.md`](./210_performance_and_scalability.md).
- **Opt-in, local-only telemetry.** No external transmission, no third-party sinks. See ADR [`05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md).
- **CalVer with no compatibility signal in the version string.** [`BREAKING.md`](../../BREAKING.md) is the compatibility contract; the version is time-only by design.
- **Two-stage retrieval (dense + cross-encoder).** Replacing the second stage is a design change, not debt. See ADR [`03_cross_encoder_reranker_second_stage.md`](../ADRs/03_cross_encoder_reranker_second_stage.md).
- **Centroid pre-ranking for multi-collection routing.** Alternative routers are an open design space, not unpaid debt. See ADR [`04_multi_collection_router_with_centroid_preranking.md`](../ADRs/04_multi_collection_router_with_centroid_preranking.md).

## Related documents

- [`100_system_architecture_overview.md`](./100_system_architecture_overview.md) — pipeline context.
- [`140_error_handling_strategy.md`](./140_error_handling_strategy.md) — how contract drift surfaces at runtime.
- [`150_security_and_privacy_architecture.md`](./150_security_and_privacy_architecture.md) — invariants behind the SEC-* and TEL-* entries.
- [`200_testing_strategy.md`](./200_testing_strategy.md) — coverage and eval gates.
- [`510_release_and_environment_strategy.md`](./510_release_and_environment_strategy.md) — how paydowns ship.
- [`../BREAKING.md`](../../BREAKING.md) — already-queued contract changes.
- [`../roadmap.md`](../../roadmap.md) and [`../Backlog/`](../Backlog/) — where planned refactors land.
