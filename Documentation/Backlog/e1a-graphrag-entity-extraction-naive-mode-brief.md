# Feature Brief: E1a — GraphRAG Entity Extraction + Naive Query Expansion

## Problem
Users searching large, relationship-dense corpora (codebases, documentation, research) get results ranked purely on lexical and semantic similarity to the query string — entities mentioned in the query that have known relationships to other entities are never surfaced. A query for "AuthService" should also pull chunks about `TokenValidator` and `SessionStore` if the graph shows they depend on it.

## Goal
After enabling `[graph].enabled = true`, every ingested document is entity-extracted into per-collection graph tables. A `graph_mode=naive` flag on `/search` and the MCP `search` tool expands the query with related entity names before retrieval, increasing recall on relationship-aware queries. The eval gate `graph_mrr >= baseline_mrr` passes.

## Users & Context
Operators indexing relationship-dense corpora: software codebases, API documentation, research papers, or internal wikis where entities (functions, classes, concepts, people) relate to each other in ways that plain vector/FTS search cannot traverse. They enable `[graph]` in TOML and re-ingest; end users query normally but can opt in to `graph_mode=naive` per request.

## Core Flow

1. Operator adds `[graph] enabled = true` (and optionally `extraction_model`) to `archon-search.toml`.
2. On first ingest with graph enabled, archon-search auto-downloads the spaCy model (`en_core_web_sm`) — logged clearly, same pattern as fastembed.
3. During each ingest job (sync, before job marked `DONE`), each chunk is passed through the extractor: spaCy NER identifies entities; if `extraction_model` is set, an LLM call classifies typed relationships (`USES`, `IMPLEMENTS`, `DEPENDS_ON`, etc.).
4. Entities and relationships are written to per-collection `_graph_nodes` and `_graph_edges` LanceDB tables with stable IDs (SHA-256 hex of `entity_name.strip().lower()`).
5. When edge count crosses `[graph].backend_threshold_edges` (default 10 000), archon-search logs a warning and surfaces a hint: run `archon-search graph migrate <collection>` to move to Kuzu.
6. At query time: `POST /search` or MCP `search` with `graph_mode=naive` loads the collection's graph (NetworkX in-memory or Kuzu), identifies entities in the query string, fetches their first-degree neighbours, and appends neighbour entity names to the retrieval query before the normal hybrid search pipeline runs.
7. Results return in the standard `SearchResult` schema — no new fields for E1a.

## In Scope
- `[graph]` TOML section: `enabled`, `extraction_model` (optional LLM model string), `backend_threshold_edges` (int, default 10 000)
- `archon-search[graph]` optional extra: spaCy + Kuzu
- spaCy `en_core_web_sm` auto-download on first use (logged, not silent)
- Per-collection `_graph_nodes` and `_graph_edges` LanceDB tables
- Stable entity IDs (SHA-256 hex)
- Entity types: people, concepts, systems, events, code symbols (spaCy NER categories mapped to these)
- Optional LLM typed relationship extraction when `extraction_model` is set
- `graph_mode=naive` parameter on `POST /search` and MCP `search`
- NetworkX for traversal when edges < threshold; Kuzu when edges >= threshold (after explicit migration)
- `archon-search graph migrate <collection>` CLI command; warning log at threshold
- Eval gate: `graph_mrr >= baseline_mrr` for naive mode
- `GET /status` exposes `graph: { enabled, backend, node_count, edge_count }` per collection

## Out of Scope
- Leiden community detection and `graph_mode=local` / `graph_mode=global` → E1b
- Graph-path provenance in `/explain` (traversal chain display) → E1c
- Auto-migration to Kuzu (explicit CLI command only in E1a)
- Graph visualisation or admin UI → E8
- Streaming search results → E3 (independent)
- Graph schema versioning / migrations across restarts — covered by existing `STORE_SCHEMA_VERSION` bump policy

## Key Decisions

- **Split E1 into E1a/E1b/E1c**: Naive mode is independent of Leiden community detection; shipping them separately reduces risk and delivers user value faster.
- **Local NER default, LLM opt-in**: Preserves local-first posture consistent with fastembed and the local reranker. LLM extraction is an upgrade, not a requirement.
- **Sync extraction within the ingest job**: Avoids the inconsistency window where a document is indexed but its graph is not yet built. The jobs system already handles long-running jobs gracefully.
- **Both NetworkX and Kuzu**: NetworkX covers most corpora with zero compiled overhead; Kuzu is the upgrade path for large graphs, reached via an explicit `migrate` command rather than silent auto-migration.
- **`backend_threshold_edges` configurable (default 10 000)**: Operators with large corpora can tune the switchover point; hardcoding would require a code change to adjust.
- **Warning + explicit migrate command**: Silent data migrations on live collections are an operational hazard. Operators must opt in.

## Edge Cases & Constraints

- **Graph disabled mid-lifecycle**: If `[graph].enabled` is toggled off after extraction, existing `_graph_nodes`/`_graph_edges` tables are left in place but never queried. No cleanup is automatic; a future `archon-search graph drop <collection>` command can handle this.
- **spaCy model absent on air-gapped installs**: Auto-download will fail. Document that `python -m spacy download en_core_web_sm` must be run manually in air-gapped environments; the ingest job returns a clear error referencing this.
- **LLM extraction failures mid-ingest**: If `extraction_model` is set and the LLM call fails (network, quota), the ingest job should log the failure and fall back to spaCy-only extraction for affected chunks — not fail the entire job.
- **Kuzu compiled wheel absent**: If `archon-search[graph]` is not installed but `[graph].enabled = true`, server startup emits a `ConfigError` with an actionable install hint.
- **ACL interaction**: `_graph_nodes` and `_graph_edges` tables are per-collection and inherit the collection's ACL. Graph expansion at query time must only use nodes/edges from collections the requesting namespace has access to.
- **`graph_mode=naive` with fanout across collections**: Each collection's graph is queried independently; entity name expansion is applied per-collection before that collection's hybrid search runs.
- **Eval harness**: Add `graph_mode=naive` fixture queries to `tests/eval/queries.jsonl`; add `graph_mrr` threshold to `thresholds.toml`. Backends must be deterministic (no LLM calls in eval backends).

## Open Questions
- Which spaCy entity categories map cleanly to the five entity types (people, concepts, systems, events, code symbols)? The mapping needs to be defined explicitly in code — spaCy's `en_core_web_sm` categories (`PERSON`, `ORG`, `GPE`, `PRODUCT`, etc.) do not map 1:1.
- For code corpora, should code-symbol entities come from the existing `_symbol_type`/`_symbol_subtype` chunk enrichment (C3) rather than re-running NER? Reusing C3 data would be more accurate for code.
- What is the LLM prompt template for typed relationship extraction? Needs to be defined and versioned — it directly affects graph quality.

## Future Iterations
- **E1b**: Leiden community detection; `graph_mode=local` (community summaries) and `graph_mode=global` (full traversal) — the high-value complex modes deferred from E1a.
- **E1c**: Graph-path provenance in `/explain` — each graph-retrieved chunk shows `(query_entity → relationship → neighbour_entity → chunk)` traversal chain.
- **`archon-search graph drop <collection>`**: Clean up `_graph_nodes`/`_graph_edges` tables when graph is disabled or collection is deleted.
- **Configurable NER model**: Allow `[graph].ner_model` to specify a different spaCy model (e.g. `en_core_web_lg`) for higher accuracy on specialised corpora.

## Recommendation
E1a is the right first step: it proves the entity extraction pipeline, establishes the graph storage layer, and delivers measurable recall improvement on relationship-aware queries — all without the Leiden complexity of local/global modes. The hardest part is the entity-type mapping from spaCy categories to archon-search's five types, and the code-symbol question (reuse C3 enrichment vs re-run NER). Nail those two decisions before implementation starts or they will silently degrade graph quality. The eval gate (`graph_mrr >= baseline_mrr`) must be wired before any code ships — it is the only objective signal that naive expansion is helping, not hurting.
