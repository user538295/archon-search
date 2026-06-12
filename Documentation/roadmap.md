**Purpose**: Capture the priority-ordered forward plan for `archon-search` so contributors can see what is next and why.
**Audience**: Maintainers and contributors planning, prioritising, or scoping work.
**Status**: Draft
**Last reviewed**: 2026-06-11
**Next review**: 2026-08-31

# Roadmap

This roadmap is the project-level summary of the backlog. The authoritative, fully-rationalised list — with verification against current code — lives in `Documentation/Backlog/03_world_class_roadmap.md`. Where the two diverge, the backlog file wins.

## Principles

1. **Boundary before features.** Product separation, service contract, and security boundaries come before retrieval-quality additions.
2. **Quality before flash.** Filters, server-side multi-collection search, and stronger routing land before HyDE, RAG Fusion, or GraphRAG.
3. **Evaluation gates every ranking change.** No relevance change ships without a delta against `tests/eval/baselines/baseline.json`.
4. **Operability before scale.** Auth, health, logs, and observability come before distributed deployment.
5. **The code is the source of truth.** Backlog items are verified against the current `archon_search/` tree, not assumed from older comparisons.

## Status Snapshot (verified at last review)

Done (visible in the current repo):

- Standalone package with its own CLI, config, and release process (`archon_search/`, `cli/main.py`, `release.sh`).
- FastAPI REST control plane with OpenAPI contract; MCP endpoint sharing the same auth layer (`server/app.py`, `server/mcp.py`).
- Bearer-token auth bootstrap with `~/.archon-search/.search.env` (mode `600`) and env-var overrides (`key_manager.py`).
- Hybrid retrieval (vector + FTS), cross-encoder reranking, multi-collection centroid routing (`store.py`, `reranker.py`, `router.py`, `pipeline.py`).
- Async job model for long-running ingest/reindex operations (`archon_search/jobs/`).
- Per-collection namespace (`CollectionMeta.namespace`, stored on `_archon_collection_meta`) and per-chunk ACL (`acl` column on chunk tables, `acl.py`).
- Opt-in local telemetry with structural no-raw-query guarantee; `[telemetry].export_enabled = true` is coerced to `false` with a warning at config load (`archon_search/telemetry/`, `archon_search/config.py`).
- Deterministic evaluation harness with committed thresholds and baseline (`tests/eval/`).
- Per-OS service install (`archon_search/platform/`, `cli/install_cmd.py`).
- Concurrency hardening of the indexing-state store and router cache (A6): `IndexingStateStore` is thread-safe via an internal `RLock` (closes `CON-3` — no lost updates to `.indexing_state.json` under concurrent multi-collection writes), and `MultiCollectionRouter` gained `invalidate()` / `initial_metadata` with the FastAPI per-request router lifecycle pinned by a regression test (addresses `CON-2`). See `Architecture/530_technical_debt_refactoring_roadmap.md`.
- **Tiered install profiles (C0):** `archon-search install` now presents three profiles (`minimal`, `balanced`, `max`) for both English and multilingual stacks. The profile is written into `[database].profile` / `[database].multilingual` in `archon-search.toml`. The installer includes disk-space checks, a Jina CC-BY-NC-4.0 license gate for multilingual `balanced`/`max`, model pre-warming, reinstall guard with rollback, and a `--force --delete-db` escape hatch. Implemented in `archon_search/profiles.py`, `archon_search/install.py`, and `archon_search/cli/install_cmd.py`.
- **GitHub Releases with git-cliff changelog (C1):** `release.sh` now requires `git-cliff >= 2.4`, generates release notes via `git-cliff --unreleased`, prepends the section to `CHANGELOG.md` (committed and pushed to `main` before tagging), and verifies the commit count matches the provisional tag. A new `github-release` job in `archon-search-release.yml` (runs after `publish`, tag pushes only) extracts the first section from `CHANGELOG.md` and creates a GitHub Release via the REST API. `cliff.toml` at the repo root controls commit grouping and rendering.
- **Multilingual retrieval (C2):** Per-document language detection using fasttext `lid.176.ftz`; ISO 639-1/639-3 language tags on all chunks; `language=<code>` single-collection filter; three-state language contract (`""`/`"unknown"`/`"<code>"`); CC-BY-SA 3.0 fasttext license gate; `--accept-fasttext-license` CLI flag; startup guard for missing package/model; `FilterFlags.language_filter_used` telemetry; `/status` warning for untagged collections; language-aware FTS tokenizer (LanceDB `FTS(language="French")` API). Implemented in `archon_search/language_detector.py`, `archon_search/filters.py`, `archon_search/store_filters.py`, `archon_search/store.py`, `archon_search/pipeline.py`, `archon_search/chunker.py`, `archon_search/install.py`, `archon_search/cli/install_cmd.py`, `archon_search/server/app.py`, `archon_search/server/routes_status.py`, `archon_search/telemetry/entry.py`.
- **Markdown structural enrichment (C3a):** Every text-format chunk ingested into LanceDB now carries `_heading` (nearest preceding heading text, ≤ 512 chars) and `_section_path` (e.g. `"Installation > macOS > Homebrew"`, ≤ 512 chars, left-truncated) in its `metadata` dict. Headings are detected from ATX (`#`–`######`), setext (`===`/`---`), and RST underline styles; headings inside fenced code blocks are excluded. Binary/non-text formats silently receive empty strings. `ChunkRecord` gained transient `start_offset`/`end_offset` fields (not persisted to LanceDB). Implemented in `archon_search/enricher.py` (new), `archon_search/_types.py`, `archon_search/chunker.py`, `archon_search/pipeline.py`.
- **Page number extraction (C3b):** PDF and image chunks carry `_page_start` and (when spanning a boundary) `_page_end` in `metadata`. Page boundaries are derived from docling's `<!-- archon-search:pagebreak:v1 -->` markers excised before chunking. Implemented in `archon_search/enricher.py` (extended), `archon_search/pipeline.py`.
- **Code symbol context (C3c):** Source-code chunks (`.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.sh`) carry five AST-derived metadata fields: `_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, and `_symbol_subtype`. `CodeEnricher` (tree-sitter; optional `[code]` dep) follows the same `prepare()` / `enrich_chunk()` two-pass protocol as `MarkdownEnricher`. Python and TypeScript are mandatory first-class languages; other languages degrade gracefully with a one-time INFO log and no ingest abort. `SearchPipeline.ingest_file` dispatches by file extension. Implemented in `archon_search/code_enricher.py` (new), `archon_search/pipeline.py`.
- **HyDE query expansion (C4):** `hyde=true` on `/search`, `/explain`, or the MCP `search`/`search_with_context`/`explain` tools generates a hypothetical document via Anthropic API and uses its embedding (or a query+doc blend) for vector retrieval. Optional dependency (`archon-search[hyde]`), off by default, silent fallback on every failure path. Mutually exclusive with RAG Fusion. Implemented in `archon_search/hyde.py`, `archon_search/pipeline.py`.
- **Explain endpoint (A4):** `POST /explain` returns per-stage scores (vector rank, FTS rank, fused RRF, reranker score), applied filters, and the routing decision. Also exposed as the `explain` MCP tool. Backed by `MultiCollectionRouter.rank_with_scores()` and the `ScoredSearchCandidate` trace path. Implemented in `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py`.
- **Per-collection embedding model (C1):** `CollectionMeta.active_embedding_model` is persisted per collection; ingest/search/sync paths consult it; `validate_embedding_model()` raises `ModelValidationError` on cross-model mismatch and `/search`'s `SearchResponse` echoes the resolved `embedding_model`. Implemented in `archon_search/store.py`, `archon_search/sync.py`, `archon_search/pipeline.py`, `archon_search/server/schemas.py`.
- **Server-side multi-collection search (B3):** `SearchPipeline.search_many()` fans out one embedded query across the routed shortlist in parallel, shares the merge/rerank stages, and surfaces partial-failure handling via `ExceptionGroup`. Implemented in `archon_search/pipeline.py`, `archon_search/server/routes_search.py`.
- **Stronger collection routing (B4):** `CollectionMeta.description_embedding` persists alongside the centroid; `MultiCollectionRouter` gains `routing_strategy: "centroid" | "hybrid"` with `routing_description_weight` (default centroid, opt-in hybrid). Eval harness gains rank-sensitive `routing_mrr_centroid` / `routing_mrr_hybrid` floors in `tests/eval/thresholds.toml`. Implemented in `archon_search/router.py`, `archon_search/store.py`, `archon_search/collection_meta.py`, `archon_search/config.py`.
- **Stage-level observability (B1):** `[observability].stage_timings_enabled = True` (default) emits per-stage durations (parse, embed, route, vector, FTS, fuse, rerank, end-to-end) on the search/explain trace and into telemetry entries. Implemented in `archon_search/observability.py`, `archon_search/config.py`.
- **Deeper health/readiness (B2):** `GET /health` (liveness, unauth) and `GET /ready` (model + index + watcher readiness, authed) split the surface. Implemented in `archon_search/server/routes_health.py`, `archon_search/server/routes_ready.py`.
- **Structured logs with rotation (B7):** `TimedRotatingFileHandler` writes JSON-formatted logs under `~/.archon-search/logs/`; rotation period and retention come from config. Implemented in `archon_search/logging_setup.py`.
- **Fsync-backed durable writes (A7):** `_durable_io.atomic_write()` follows the POSIX fsyncgate recipe (write→flush→`os.fsync(file_fd)`→`os.replace`→`os.fsync(parent_dir_fd)`) and is the persistence path for `.indexing_state.json`, the API key file, and every other small JSON artifact. CI enforces 100% coverage of `_durable_io.py`. Implemented in `archon_search/_durable_io.py`.
- **MCP Pydantic response schemas (C7):** All ten MCP tools return validated Pydantic models (`McpSearchResponse`, `IngestResultSchema`, `CollectionDetailSchema`, etc.) with `extra="forbid"`; `ValidationError` is caught and surfaced as a structured error. Field-narrowing breakages are recorded in `BREAKING.md`. Implemented in `archon_search/server/mcp_schemas.py`, `archon_search/server/mcp.py`.
- **Test-suite parallel execution (C10 + C11 + C12):** `pytest-xdist` with `--dist=loadgroup`, session-scoped `connected_store`, `xdist_group("mcp")` on 16 MCP-stub files, `xdist_group("install")` on the three install-lock files, and per-worker native-thread caps in `tests/conftest.py`. Default-suite median wall time on a 14-core machine is **~127s** (range 106–140s) — down from 6.5 min before C10. CI uses `-n0` explicitly for serial `--cov-append` correctness. See `Architecture/200_testing_strategy.md`.

## Priority 0 — Product Boundary (largely landed; remaining hardening)

Most of the original product-separation backlog is shipped. Remaining items:

- Continue hardening the canonical service contract and job model so REST and MCP stay layered over the same internal pipeline as new endpoints land.
- Keep `BREAKING.md` ahead of any contract-surface change.

## Priority 1 — Retrieval Quality

The original Phase B / C retrieval slate is shipped. Items 1–8 below are all marked ✓. Remaining retrieval-quality work is captured under Priority 2 (storage correctness) and Priority 5 (advanced).

1. **Metadata filters at search time.** ✓ **A2 + C2 shipped**: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`/`before`, and `language` filters are all active via `SearchFilters`. Language filter (C2) requires `multilingual=True` for populated tags; the filter itself is active for all installs.
2. **Server-side multi-collection search primitive.** ✓ **B3 shipped**: `SearchPipeline.search_many()` embeds the query once, fans out across the routed shortlist in parallel, and shares merge/rerank. See `Documentation/Completed/B3-server-side-multi-collection-search-plan.md`.
3. **Stronger collection routing.** ✓ **B4 shipped**: opt-in `routing_strategy = "hybrid"` blends centroid cosine with description-embedding cosine (weight `routing_description_weight`); rank-sensitive `routing_mrr` floors gate the eval harness. Default remains centroid. See `Documentation/Completed/B4-stronger-collection-routing-plan.md`.
4. **HyDE / query expansion.** ✓ **C4 shipped**: optional `archon-search[hyde]` extra; `hyde=true` on `/search`, `/explain`, or MCP tools generates a hypothetical doc via Anthropic API and uses its embedding for vector retrieval; off by default; silent fallback on every failure. Mutually exclusive with RAG Fusion. See `Documentation/Completed/C4-hyde-query-expansion-plan.md`.
5. **RAG Fusion / multi-query decomposition.** ✓ **C5 shipped**: `rag_fusion=true` on any `/search`, `/explain`, or MCP `search`/`search_with_context`/`explain` call decomposes the query into N semantic variants via Anthropic API, searches in parallel, and fuses via second-pass RRF. Optional dependency (`archon-search[rag_fusion]`), off by default, silent fallback on all failure paths. Mutually exclusive with HyDE. See `Documentation/Completed/C5-rag-fusion-plan.md`.
6. **Explain / debug endpoint.** ✓ **A4 shipped**: `POST /explain` and the `explain` MCP tool return vector rank, FTS rank, fused score, reranker score, applied filters, and routing decision. See `Documentation/Completed/A4-explain-endpoint-plan.md`.
7. **Per-collection embedding model selection.** ✓ **C1 shipped**: `CollectionMeta.active_embedding_model` is the per-collection model tag; ingest/search/sync consult it; `validate_embedding_model()` raises `ModelValidationError` on cross-model query mismatch; `SearchResponse.embedding_model` echoes the resolved model. See `Documentation/Completed/C1-per-collection-embedding-model-plan.md`.
8. **Multilingual retrieval.** ✓ **C2 shipped**: fasttext `lid.176.ftz` language detection at ingest; `language=<code>` filter (single-collection); three-state contract (`""` / `"unknown"` / `"<code>"`); language-aware FTS tokenization; `FilterFlags.language_filter_used` telemetry; per-collection `GET /status` warning for untagged collections. See `Documentation/Completed/C2-multilingual-retrieval-plan.md`.

## Priority 2 — Ingestion and Storage Correctness

- Connector / federation architecture beyond local filesystem.
- ~~Replace full-collection FTS rebuild as the default update path with incremental / additive FTS maintenance.~~ **✓ C6 shipped**: `store.optimize_fts()` replaces `rebuild_fts_index()` at all ingest and sync call sites (O(delta) vs. O(collection)). Delete path FTS is maintained via `optimize_fts` after `delete_document`. `reindex_metadata` no longer triggers any FTS call. See `Documentation/Architecture/210_performance_and_scalability.md`.
- Remove full metadata rescans from incremental sync.
- Streaming / incremental chunking for very large files.
- Chunk-level enrichment (heading ancestry, section path, page numbers, code-symbol context):
  - **C3a — Markdown structural enrichment** (`_heading`, `_section_path`): ✓ **shipped**. See Status Snapshot above.
  - **C3b — Page number extraction** (`_page_start`, `_page_end`): ✓ **shipped**. See Status Snapshot above. Plans/briefs archived under `Completed/C3b-page-number-extraction-brief.md` and `Completed/C3b-page-number-extraction-plan.md`.
  - **C3c — Code symbol context** (`_symbol_type`, `_symbol_subtype`, `_containing_function`, `_containing_class`, `_module_path`): ✓ **shipped**. `CodeEnricher` (tree-sitter, optional `[code]` dep) enriches source-code chunks (`.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.sh`) with five symbol-level metadata fields. Python and TypeScript are mandatory first-class languages; other languages degrade gracefully. See `archon_search/code_enricher.py` and `Completed/C3c-code-symbol-context-plan.md`.
- Export / import / backup / restore APIs.
- Schema migration strategy that does not require full re-ingest.

## Priority 3 — Standalone Operability

- ~~Deeper health, readiness, and diagnostics (storage connectivity, model load, warm state, index build, watcher state, collection staleness).~~ **✓ B2 shipped**: `GET /health` (liveness, unauthed) + `GET /ready` (model + index + watcher readiness, authed). See Status Snapshot above.
- Install-time / background provider validation for both embedder and reranker.
- ~~Observability and stage-level latency breakdowns (parse, embed, route, vector, FTS, fuse, rerank, end-to-end).~~ **✓ B1 shipped**: per-stage durations on search/explain traces and telemetry. See Status Snapshot above.
- ~~Structured operational logs with rotation.~~ **✓ B7 shipped**: `TimedRotatingFileHandler` + JSON formatter, retention from config. See Status Snapshot above.
- Background maintenance jobs (stale-collection detection, compaction, orphan cleanup, retry policy, integrity checks).

## Priority 4 — Product Surface and UX

- Streaming search results.
- Python and TypeScript SDKs.
- Admin / debug UI (only after explain APIs stabilise — A4 has stabilised them).
- Per-collection access-control policies (after namespaces + auth; per-chunk ACL already exists via `acl.py`).
- **C8 — Extended setup wizard with optional feature selection.** *Active.* Task 1.1 (`WizardFeatures` dataclass) shipped (`4e0bd6e`); 11 tasks remain. The current wizard asks only two questions (profile + confirm). Ten optional features — including `[code]` tree-sitter enrichment, `[multilingual]` language detection, reranker toggle, telemetry, filesystem watcher, routing strategy, and the HyDE extra — require manual config editing or knowing undocumented CLI flags. C8 adds an interactive feature-selection step to the wizard so users can enable any optional feature at install time, including automatic `pip install archon-search[<extra>]` invocation where needed. Investigation and gap analysis: `Backlog/C8-wizard-optional-features-investigation.md`; plan: `Backlog/C8-wizard-optional-features-plan.md`.
- **C9 — Container support (Docker + GHCR).** *Active (not started).* `docker run` and `docker compose up` start `archon-search` configured purely via env vars, with all runtime state on a single mounted volume and logs to stderr. CPU (`:latest`) and NVIDIA GPU (`:gpu`) image variants are published to GHCR on tag push via `archon-search-release.yml`. Plan: `Backlog/C9-container-support-plan.md`; brief: `Backlog/C9-container-support-brief.md`.
- **C13 — Bypass FTS-index rebuild in tests that don't query FTS.** *Proposed (not scheduled).* Adds `rebuild_fts: bool = True` to `SearchPipeline.ingest_directory()` (mirroring `ingest_file`); estimated impact 127s → 90–105s default-suite median wall time. Brief: `Backlog/C13-fts-rebuild-test-bypass-brief.md`.

## Priority 5 — Advanced

These remain on the long-horizon list and must not displace earlier priorities:

- Salience and temporal weighting (explicit, disableable scoring component).
- Semantic memory tiers (recent / durable / pinned / archival).
- GraphRAG / entity-relationship retrieval.
- Richer multimodal retrieval and document understanding.
- Horizontal scaling (only after the storage contract stabilises).
- Pluggable storage backends (only after the application contract stabilises).

## Already Captured As Known Risks

These are documented elsewhere and noted here so they are not lost:

- **Hashed-doc-id mode for telemetry.** Today `result_doc_ids` are derived from filesystem paths and may leak username / directory structure when telemetry is enabled. Operators accept this when they opt in; a hashed mode is planned. See README "Path-derived `doc_id` risk".
- **External telemetry transmission.** Reserved for a future release; `[telemetry].export_enabled = true` is currently coerced to `false` with a warning at config load (`archon_search/config.py`). See `Architecture/000_introduction_and_guiding_principles.md` "Explicit Non-Goals".
- ~~**State-file durability under power loss.** A6 closed the in-process consistency race on `.indexing_state.json` (`CON-3`), but the atomic-rename write is not yet `fsync`-backed, so a power loss between rename and disk flush can still corrupt or lose the latest write. Closing this durability gap is the next step (A7 / fsync).~~ **✓ A7 shipped**: `_durable_io.atomic_write()` follows the POSIX fsyncgate recipe (file + parent directory fsync); CI enforces 100% coverage of `_durable_io.py`. See Status Snapshot above.

## Related Documents

- Authoritative backlog: `Backlog/03_world_class_roadmap.md`
- Vision and non-goals: `Architecture/000_introduction_and_guiding_principles.md`
- Engineering constraints: `Architecture/010_engineering_principles_and_constraints.md`
- Compatibility log: `../BREAKING.md`
- Evaluation harness maintenance: `../tests/eval/README.md`
