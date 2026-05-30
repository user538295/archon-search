**Purpose**: Capture the priority-ordered forward plan for `archon-search` so contributors can see what is next and why.
**Audience**: Maintainers and contributors planning, prioritising, or scoping work.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

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

## Priority 0 — Product Boundary (largely landed; remaining hardening)

Most of the original product-separation backlog is shipped. Remaining items:

- Continue hardening the canonical service contract and job model so REST and MCP stay layered over the same internal pipeline as new endpoints land.
- Keep `BREAKING.md` ahead of any contract-surface change.

## Priority 1 — Retrieval Quality

Ordered as in the backlog. None are documented as complete in the current code; treat them as the next inbound work.

1. **Metadata filters at search time.** Expose source-path / date / file-type / language / tag filters through the search API and the underlying store.
2. **Server-side multi-collection search primitive.** Embed the query once, fan out on the server, share merge/rerank.
3. **Stronger collection routing.** Keep centroids as baseline; add summary embeddings, multi-centroid / clustered representations, and description-aware routing.
4. **HyDE / query expansion.** Optional, off by default until the eval harness shows gains.
5. **RAG Fusion / multi-query decomposition.** Parallel sub-query search with benchmarked fusion.
6. **Explain / debug endpoint.** Surface vector rank, FTS rank, fused score, reranker score, applied filters, and routing decision.
7. **Per-collection embedding model selection.** `CollectionMeta.embedding_model` already exists; the search and ingest paths still operate against a global model. Validate query-time compatibility and define cross-model routing.
8. **Multilingual retrieval.** Multilingual embedding option, language metadata, language-aware FTS where the backend allows.

## Priority 2 — Ingestion and Storage Correctness

- Connector / federation architecture beyond local filesystem.
- Replace full-collection FTS rebuild as the default update path with incremental / additive FTS maintenance.
- Remove full metadata rescans from incremental sync.
- Streaming / incremental chunking for very large files.
- Chunk-level enrichment (heading ancestry, section path, page numbers, code-symbol context).
- Export / import / backup / restore APIs.
- Schema migration strategy that does not require full re-ingest.

## Priority 3 — Standalone Operability

- Deeper health, readiness, and diagnostics (storage connectivity, model load, warm state, index build, watcher state, collection staleness).
- Install-time / background provider validation for both embedder and reranker.
- Observability and stage-level latency breakdowns (parse, embed, route, vector, FTS, fuse, rerank, end-to-end).
- Structured operational logs with rotation.
- Background maintenance jobs (stale-collection detection, compaction, orphan cleanup, retry policy, integrity checks).

## Priority 4 — Product Surface and UX

- Streaming search results.
- Python and TypeScript SDKs.
- Admin / debug UI (only after explain APIs stabilise).
- Per-collection access-control policies (after namespaces + auth).

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
- **State-file durability under power loss.** A6 closed the in-process consistency race on `.indexing_state.json` (`CON-3`), but the atomic-rename write is not yet `fsync`-backed, so a power loss between rename and disk flush can still corrupt or lose the latest write. Closing this durability gap is the next step (A7 / fsync).

## Related Documents

- Authoritative backlog: `Backlog/03_world_class_roadmap.md`
- Vision and non-goals: `Architecture/000_introduction_and_guiding_principles.md`
- Engineering constraints: `Architecture/010_engineering_principles_and_constraints.md`
- Compatibility log: `../BREAKING.md`
- Evaluation harness maintenance: `../tests/eval/README.md`
