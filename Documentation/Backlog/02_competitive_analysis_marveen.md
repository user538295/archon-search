**Purpose**: Comparative analysis of `archon-search` versus Marveen's memory/search subsystem to extract transferable ideas and gaps.
**Audience**: Maintainers planning future Search architecture and roadmap work.
**Status**: Draft
**Last reviewed**: 2026-04-30 / **Next review**: 2026-10-30

# Search System Comparison: Archon vs. Marveen

> **Date:** 2026-04-29
> **Purpose:** Deep technical comparison to identify opportunities for improving Archon's search system. Marveen repo: https://github.com/Szotasz/marveen

---

## Important Framing

> **Note:** All factual claims about **Marveen** in this document (line counts, FTS5 trigger details, BM25, nomic-embed-text dimensions, salience decay rates, AES-256-GCM vault, trust graph, install scripts, port locking, PID legitimacy check, tmux pane state detection, ~10 Vitest test files, etc.) are #Unverified — they were not re-verified against the Marveen repo for this review.

**Marveen is not a document search engine.** It is an AI agent management framework whose memory subsystem happens to include FTS + vector search over *agent memories* (short text snippets). Archon's search is a full document RAG pipeline (file ingestion → chunking → embedding → hybrid retrieval → reranking → multi-collection routing).

This comparison therefore has two layers:
1. **Search/retrieval subsystem quality** — pure head-to-head on search mechanics
2. **Broader system design** — what each can teach the other for building the best possible search system

---

## Dimension 1 — Architecture & Design

### Archon
**Design:** Layered service architecture with strict separation of concerns. Storage → Pipeline → Router → Server → CLI. Each layer has a single responsibility. The server exposes both a FastAPI REST surface and a FastMCP endpoint running side-by-side under shared Bearer auth. Pluggable backends via Protocol-typed interfaces (`EmbedderBackend`, `RerankerBackend`).

**Strengths:**
- Clean layering; each component is independently testable
- Standalone process — REST and MCP share one server, no cross-process coordination required
- Protocol-based extensibility without premature abstraction

**Weaknesses:**
- Single-server only — no horizontal scale design
- All collections share one global embedding model (no per-collection model selection)
- Largest modules `sync.py` (889 lines) and `store.py` (841 lines) are approaching god-object territory

**Score: 8/10**

### Marveen
**Design:** Monolithic Node.js daemon. `db.ts` is a 1,065-line god object with all SQL for all tables. `web.ts` handles routing, background intervals, and server lifecycle. Memory subsystem is tightly coupled to agent identity model (everything is scoped to `agent_id`).

**Strengths:**
- Everything in one process = zero inter-service coordination overhead
- Memory is a first-class domain object with rich metadata (tiers, salience, sector)
- Trust graph and security model are cleanly factored as pure modules

**Weaknesses:**
- No separation between storage, retrieval, and API layers
- `db.ts` as a god object means any schema change touches the entire 1,065-line file
- Memory model is tightly coupled to multi-agent use case — hard to repurpose for document search

**Score: 5/10**

---

## Dimension 2 — Indexing / Ingestion Pipeline

### Archon
**What it does:** Full document ingestion: file discovery (multi-format, ~40 binary extensions excluded via `_BINARY_EXTENSIONS` in `pipeline.py`, symlink-skipping, hidden-file filtering) → format-specific parsing (trafilatura/docling/markitdown) → Chonkie RecursiveChunker (GPT-2 tokenizer; chunk size from `SearchConfig.chunk_size`, default 512) → fastembed embedding → LanceDB persist → FTS rebuild. SHA256 content addressing gives idempotent re-ingest. Declarative sync with file change detection and crash-recovery state machine.

**Strengths:**
- Parses HTML, PDF (with OCR), Office docs, images via trafilatura/docling/markitdown #Unverified (exact format count not enforced in source)
- Idempotent: re-ingesting the same file is safe (delete-then-insert by doc_id)
- Crash recovery: IN_PROGRESS → PENDING on restart
- Auto description generation via Haiku (20% doc change threshold)
- Filesystem watcher for live synchronization

**Weaknesses:**
- FTS index is rebuilt in full on every `ingest_directory()` call — potential bottleneck for large collections
- `DocumentParser` thread-safety is not explicitly documented in `parser.py` #Unverified
- No support for streaming/incremental chunking of very large files
- Collection-level embedding model selection not supported

**Score: 9/10**

### Marveen
**What it does:** Memory ingestion only — `INSERT INTO memories` + async fire-and-forget `generateEmbedding()` to Ollama. Bulk import via `/api/memories/import` accepts pre-chunked strings; an Ollama Gemma4 call categorizes each chunk (hot/warm/cold/shared). No file discovery, no format parsing, no chunking, no dedup, no sync.

**Strengths:**
- AI-driven tier categorization at import time is a clever UX feature
- FTS5 triggers keep the full-text index automatically synchronized on every write
- Salience decay gives long-term memory management without explicit TTL management

**Weaknesses:**
- Not a document ingestion pipeline at all — caller must pre-chunk all content
- No SHA256 dedup: same content inserted twice creates two records
- No file format support — PDFs, Office docs, code files are unsupported
- Embedding generation has explicit 100 ms sleeps between Ollama calls (no batching)
- Input truncated to 2,000 characters before embedding (loses tail of longer chunks)

**Score: 2/10**

---

## Dimension 3 — Search Quality

### Archon
**Pipeline:** embed query → hybrid search (vector + FTS via LanceDB) → RRF fusion (k=60) → cross-encoder reranking (default `cross-encoder/ms-marco-MiniLM-L-6-v2`, configurable) → context-window enrichment (adjacent chunk fetch).

**Strengths:**
- Cross-encoder reranking is state-of-the-art for precision (query-document pair scoring vs. bi-encoder approximation)
- Context window expansion (`search_with_context`) recovers semantic coherence across chunk boundaries
- Multi-collection routing: centroid pre-ranking with confidence gating eliminates irrelevant collections before reranking
- Adaptive fetch size: `max(top_k * 3, 20)` ensures reranker has enough candidates
- FTS graceful degradation (vector-only if FTS index missing)

**Weaknesses:**
- No BM25 score normalization before RRF — vector and FTS ranks are assumed comparable
- Centroid representation may be a weak proxy for semantic coverage in heterogeneous collections
- No query expansion or synonyms
- No metadata filters (can't filter by file type, date, source path)

**Score: 9/10**

### Marveen
**Pipeline:** FTS5 BM25 (default) or hybrid (FTS5 + brute-force cosine) → RRF fusion (k=60). No reranker. Salience multiplier applied at context-building time (not at retrieval time).

**Strengths:**
- SQLite FTS5 with BM25 is fast and accurate for keyword-heavy queries
- Prefix matching (`word*`) is good for partial/typo-tolerant queries
- Salience model creates a soft form of personalized ranking (frequently accessed memories rise over time)
- Hot/warm/cold tier model allows explicit priority control

**Weaknesses:**
- Brute-force O(n) vector search — latency grows linearly with memory count
- No reranker at all — retrieval precision is limited to bi-encoder + BM25 scores
- No cross-collection routing (memory is always scoped to one agent)
- RRF merges ranks from async (vector) and sync (FTS) runs in-process JS — FTS is technically synchronous SQLite, not a true async path
- 2,000-character embedding truncation degrades recall on longer memories

**Score: 4/10**

---

## Dimension 4 — Embedding Model Choices

### Archon
- **Default:** `BAAI/bge-small-en-v1.5` (model specs such as 384-dim / 33 MB ONNX are upstream model properties, not asserted in archon-search source) #Unverified
- **Reranker:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (default; configurable via `reranker_model`)
- **Provider:** fastembed; runtime auto-detects GPU type via `runtime.detect_gpu()` (CUDA on Linux, METAL on ARM macOS, NONE otherwise). CoreML acceleration is referenced in `install.py` on macOS. #Unverified (clean three-way CPU/CUDA/CoreML framing is simplified)
- **Configurable:** Both models are `config.toml` fields under `[database]`; can swap without code changes
- **Lazy loading:** Thread-safe singleton; loaded on first call
- **Validation:** `install.py` validates provider configuration before committing #Unverified (specific `validate_providers()` method name not located)

**Strengths:**
- Model is configurable; upgrading to `bge-large` or `gte-qwen2` requires one config change
- GPU acceleration works out-of-the-box on CUDA and Apple Silicon
- fastembed uses ONNX Runtime — no Python-only dependency, fast inference
- Reranker is a proper cross-encoder (not bi-encoder approximation)

**Weaknesses:**
- All collections share the same embedding model — changing the model invalidates all indexed data
- No multi-lingual model configured by default
- No sentence transformer models (only fastembed-supported models available)

**Score: 8/10**

### Marveen
- **Model:** `nomic-embed-text` (768-dim) — hardcoded constant in `db.ts`
- **Provider:** Ollama local inference (`http://localhost:11434`)
- **No config option** to change the model without editing source code
- **Graceful degradation:** If Ollama is down, embedding returns `null`, system falls back to FTS-only

**Strengths:**
- Ollama provider model means no Python dependency; any Ollama-supported model works if you edit the constant
- `nomic-embed-text` 768-dim gives richer representation than 384-dim `bge-small`
- Graceful FTS fallback is operationally resilient

**Weaknesses:**
- Model name is a hardcoded source constant — not configurable
- Requires Ollama running separately (external dependency)
- No GPU acceleration path in the search code (Ollama handles it, but not exposed/configured)
- No validation that model dimensions match stored embeddings on restart
- 2,000-char input truncation is hardcoded, not configurable

**Score: 3/10**

---

## Dimension 5 — Storage Backend

### Archon
- **Store:** LanceDB (purpose-built columnar vector database, Apache Arrow format)
- **Vector index:** Native ANN (approximate nearest-neighbor) via LanceDB's internal IVF-PQ indexing
- **FTS index:** Explicit LanceDB FTS on `text` field
- **Schema:** Fixed Arrow schema per collection table + separate `_archon_collection_meta` table
- **Async client:** `lancedb.db.AsyncConnection` — non-blocking I/O

**Strengths:**
- ANN index intended for fast vector search; absolute latency / millions-of-records claims are not benchmarked in this repo #Unverified
- Native hybrid search (vector + FTS) without in-process merging
- Columnar storage — efficient for high-dimensional vector scans
- Metadata table with centroid, doc count, embedding model — enables multi-collection routing (`_ROUTING_FIELDS` in `router.py`)
- Collection isolation: each collection is an independent table

**Weaknesses:**
- LanceDB is an embedded DB — no multi-process concurrent writes
- Schema migration requires full re-ingest if schema changes
- LanceDB format is proprietary binary — not human-inspectable
- No backup/export API in the search server

**Score: 9/10**

### Marveen
- **Store:** SQLite (better-sqlite3), WAL mode, `chmod 0o600` on file and sidecars
- **Vector storage:** `TEXT` column with JSON-serialized float array — no ANN index
- **FTS:** SQLite FTS5 virtual table with BM25, maintained via INSERT/UPDATE/DELETE triggers
- **Security:** TOCTOU-safe DB creation (`O_EXCL`), file permissions enforced on init

**Strengths:**
- SQLite is universally portable, zero-dependency, battle-hardened
- WAL mode allows concurrent reads + one writer
- FTS5 with trigger-based sync is extremely reliable (no manual index maintenance)
- `chmod 0o600` + atomic writes are a thoughtful operational security decision
- Single-file backup: just copy `claudeclaw.db`

**Weaknesses:**
- O(n) brute-force vector scan: loads ALL embeddings into JS heap for cosine computation
- At 10,000 memories × 768-dim × 4 bytes = ~30 MB in JS heap per search — not scalable
- No approximate nearest-neighbor index; `sqlite-vss` / `sqlite-vec` extension not used
- All tables in one SQLite file — memories, kanban, sessions, tasks share the same lock

**Score: 5/10**

---

## Dimension 6 — API / Integration Surface

### Archon
- **FastMCP server:** 9 tools (`search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`)
- **FastAPI REST surface:** full REST API alongside MCP (`routes_health.py`, `routes_state.py`, `routes_status.py`, `routes_search.py`, `routes_route.py`, `routes_collections.py`, `routes_jobs.py`, `routes_telemetry.py`); `GET /openapi.json` is authoritative
- **CLI:** `archon-search <subcommand>` — 9 subcommands: `start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config`
- **Health endpoint:** `GET /health` on the FastAPI app (FastAPI + FastMCP run side-by-side)
- **Authentication:** Bearer-token auth via `APIKeyMiddleware` (`server/middleware_auth.py`); keys auto-generated by `key_manager.py`. All endpoints except `GET /health` require a Bearer token.

**Strengths:**
- MCP integration means Claude can directly query and manage search from within a session
- Full REST API enables integration with any HTTP client, not just Claude
- `/health` endpoint enables external monitoring / liveness probes
- CLI gives operators full control without needing Claude
- Shared Bearer auth across REST and MCP

**Weaknesses:**
- No streaming search results
- No query explain/debug endpoint

**Score: 7/10**

### Marveen
- **REST API:** 50+ endpoints covering memories, agents, team, scheduling, vault, connectors, kanban, daily logs
- **Dashboard:** Full web UI at `localhost:3420`
- **Telegram:** Per-agent Telegram bots via Claude `plugin:telegram`
- **Authentication:** Bearer token (auto-generated, stored in `store/.dashboard-token`)
- **CSRF protection:** Origin allowlist

**Strengths:**
- Rich REST API is integrable from any HTTP client — not Claude-specific
- Web dashboard gives non-technical users access to all features
- Bearer auth + CSRF protection is production-quality for a local service
- 50+ endpoints covering agent lifecycle, memory, scheduling, vault, MCP catalog

**Weaknesses:**
- No MCP server exposure for search — agents can't query memories via MCP tools
- No streaming endpoints
- No pagination on memory search results (only `LIMIT` parameter)
- No structured error codes — all errors return plain text messages

**Score: 7/10**

---

## Dimension 7 — Operational Concerns

### Archon
- **Install:** `SearchInstaller` wizard with GPU detection, provider setup, config write-back
- **Config:** TOML with `[server]`, `[database]`, `[routing]`, `[collections]`, `[logging]`, `[telemetry]`, `[namespaces]` sections (`config.py`), all validated at load time, full annotated example
- **State recovery:** `IN_PROGRESS → PENDING` on restart (explicit crash recovery)
- **Health checks:** `GET /health` HTTP endpoint
- **Filesystem watching:** Optional `watch = true` config; debounced per-collection triggers
- **Service management:** macOS launchd / Linux systemd / Windows via `PlatformService` ABC (`platform/macos.py`, `linux.py`, `windows.py`)

**Strengths:**
- Crash recovery is explicit and tested
- Platform-agnostic service management via strategy pattern, including Windows support
- Bearer-token auth and key auto-generation on first start

**Weaknesses:**
- No dedicated CLI health-check / doctor subcommand
- No log rotation for the search server itself
- No alerting on embedding model mismatch after upgrade
- Background sync (`sync_timeout_seconds = 0`) means startup appears healthy before data is ready

**Score: 8/10**

### Marveen
- **Install:** `install.sh` interactive wizard; `install-windows.ps1` for Windows via WSL
- **Process management:** No systemd/launchd — relies on user keeping a terminal or external process manager
- **Port locking:** `lsof`-based port acquisition with SIGTERM of prior instance
- **PID file:** `store/claudeclaw.pid` with legitimacy check
- **Logging:** Pino structured logger, no rotation
- **Update:** `./update.sh` + `GET/POST /api/updates`, auto-checks every 15 minutes
- **Backup:** `scripts/backup.sh` exists

**Strengths:**
- `install.sh` handles Node.js, Claude Code CLI, and `.env` setup in one pass
- Windows support via WSL installer is a notable operational advantage
- Auto-update check every 15 minutes is convenient

**Weaknesses:**
- No systemd/launchd integration — process not automatically restarted on crash/reboot
- No log rotation
- No equivalent of `archon doctor` health check CLI
- No embedding model validation on startup
- Startup sync is not guarded — server starts serving before all embeddings are ready (backfill is async)

**Score: 5/10**

---

## Dimension 8 — Test Coverage & Code Quality

### Archon
- **Test lines:** ~35,763 across 58 test files under `tests/` (no dedicated `tests/search/` subpackage in the standalone repo)
- **Coverage:** All core modules have dedicated test files with comprehensive happy-path + edge case coverage
- **Fixtures:** `conftest.py` with async pipeline, mock embedder/reranker/store, tmp_path DB
- **Code style:** Type hints throughout, Protocol-based extensibility, Black/isort, ruff linting
- **TDD:** Project conventions mandate tests-first

| Test Module | Lines | Coverage |
|-------------|-------|---------|
| test_sync.py | 4,612 | Sync logic, change detection, state recovery |
| test_store.py | 2,415 | LanceDB operations, hybrid search, metadata |
| test_pipeline.py | 2,129 | Ingest orchestration, description generation |
| test_install.py | 1,858 | Dependency checks, GPU detection, provider config |
| test_progress.py | 1,001 | State serialization, ETA computation |
| test_watcher.py | 771 | Filesystem event debounce, callback scheduling |
| test_reranker.py | 347 | Cross-encoder scoring |
| test_parser.py | ~327 | Format-specific parsing (HTML, PDF, Office) #Unverified (line count) |
| test_router.py | 325 | Centroid ranking, confidence gating |
| test_embedder.py | 254 | Fastembed wrapper, lazy loading |
| test_description_generator.py | ~172 | Haiku description generation #Unverified (line count) |

Server tests live under `tests/server/` and `tests/test_app.py` (no monolithic `test_server.py`).

**Strengths:**
- 58 test files covering sync, install, pipeline, store, progress, watcher, router, server routes, MCP, ACL, platform, key manager, telemetry, and more
- Protocol-typed backends enable clean mock injection
- Error paths explicitly tested (missing FTS index, invalid collection names, crash recovery)
- Coverage threshold enforced in CI: `pyproject.toml` `addopts` sets `--cov-fail-under=85`

**Weaknesses:**
- Parser thread-safety has not been verified explicitly in source #Unverified
- Integration tests with real LanceDB require tmp_path (no hermetic in-memory mode for LanceDB)

**Score: 9/10**

### Marveen
- **Test files:** ~10 Vitest test files
- **Coverage enforcement:** None — no `--coverage` flag, no threshold
- **Untested paths:** Hybrid search pipeline, vault encryption/decryption, schedule runner, message router, all web routes, heartbeat, agent scaffold generation
- **Security-critical paths:** Prompt injection wrapping and trust graph have good test coverage

**Strengths:**
- `prompt-safety.test.ts` explicitly tests injection attack vectors — rare and commendable
- `team-trust.test.ts` tests the graph logic as a pure module
- `pane-state.test.ts` covers the tmux state detection heuristics

**Weaknesses:**
- No coverage measurement or enforcement
- ~40+ routes in web.ts with zero integration tests
- Hybrid search, vault, and schedule runner — all critical paths — have no tests
- God-object `db.ts` is hard to unit-test in isolation

**Score: 3/10**

---

## Dimension 9 — Performance / Scalability

### Archon

| Metric | Value |
|--------|-------|
| Vector search type | ANN (LanceDB IVF-PQ) |
| Vector search complexity | Sub-linear (indexed) |
| Memory per chunk | ~1.5 KB (384-dim float32) #Unverified |
| Max parallel collections | 3 (configurable) |
| Async throughout | Yes (`asyncio.to_thread` for CPU-bound ops) |
| FTS rebuild | Full rebuild per `ingest_directory()` call |
| Fetch size | `max(top_k * 3, 20)` adaptive |

**Strengths:**
- LanceDB ANN is designed to scale, but no benchmarks in this repo validate million-document latency claims; the `eval/` harness explicitly notes p50/p95 is a regression guard, not a production SLA #Unverified
- `asyncio.to_thread()` keeps embedding non-blocking on the event loop
- `max_parallel_collections` config allows tuning for available resources (default 3)
- Adaptive fetch sizing balances recall vs. speed

**Weaknesses:**
- Full FTS rebuild on every `ingest_directory()` is O(n) over the entire collection
- No streaming ingest — entire directory loaded before FTS rebuild
- Single-server architecture limits horizontal scaling

**Score: 8/10**

### Marveen

| Metric | Value |
|--------|-------|
| Vector search type | Brute-force O(n) in-process JS |
| Vector search complexity | O(n) — linear |
| Memory per entry at 10K | ~30 MB in JS heap |
| Parallelism | Single-threaded event loop |
| Embedding generation | Fire-and-forget, 100 ms gap between calls |
| FTS | SQLite FTS5, scales to millions of rows |
| Practical ceiling | ~1,000–5,000 memories before vector search degrades |

**Strengths:**
- SQLite FTS5 is genuinely fast and scales well
- For < 1,000 memories, brute-force is fine and avoids ANN complexity

**Weaknesses:**
- O(n) vector search will noticeably degrade at 5,000+ memories
- No worker threads or clustering
- No `sqlite-vss` / `sqlite-vec` extension for ANN in SQLite
- 100 ms sleep between embedding calls throttles ingestion throughput

**Score: 3/10**

---

## Dimension 10 — Unique Features / Innovations

### Archon

| Feature | Description |
|---------|-------------|
| Multi-collection routing | Centroid pre-ranking → confidence gating → Decomposer routing; 3-tier strategy based on collection count |
| Auto-description generation | Haiku samples 20 random chunks → generates collection description; re-runs on 20%+ doc change |
| Crash-recovery state machine | Per-collection state: PENDING → IN_PROGRESS → DONE/FAILED; resets stale IN_PROGRESS on restart |
| Context-window enrichment | `search_with_context()` fetches adjacent chunks by sequential chunk_id to recover sentence/paragraph context |
| Filesystem watcher | watchdog-based live sync with per-collection debounce (5s) |
| Pinned collections | Always searched regardless of routing decision |
| Auto-reindex on chunk size change | Detects `chunk_size` config change and triggers re-ingest |

**Score: 8/10**

### Marveen

| Feature | Description |
|---------|-------------|
| Prompt injection defense | `wrapUntrusted()` / `wrapTrustedPeer()` with runtime-random sentinel to prevent nested injection |
| Trust graph | Graph-based symmetric trust for inter-agent message routing |
| Salience decay | 0.5%/day after 7 days, floor 0.01; access boost salience +0.1 (capped 5.0) |
| Hot/warm/cold/shared tiers | AI-categorized at import time; maps to temporal/organizational relevance |
| AES-256-GCM vault | Per-secret scrypt key derivation; vault bindings sync into MCP `.env` configs |
| Self-learning skill system | `PreCompact` hook triggers skill extraction before context compaction |
| Tmux pane state detection | Double-sampled snapshot with spinner/token-count/interrupt-marker detection before injection |
| Security profiles | Per-agent filesystem allow/deny lists mapped to Claude Code's permission engine |

**Score: 9/10** *(most innovations are in agent management, not search — but transferable ideas)*

---

## Scorecard Summary

| Dimension | Archon | Marveen |
|-----------|:------:|:-------:|
| Architecture & Design | **8** | 5 |
| Indexing / Ingestion Pipeline | **9** | 2 |
| Search Quality | **9** | 4 |
| Embedding Model Choices | **8** | 3 |
| Storage Backend | **9** | 5 |
| API / Integration Surface | **7** | **7** |
| Operational Concerns | **8** | 5 |
| Test Coverage & Code Quality | **9** | 3 |
| Performance / Scalability | **8** | 3 |
| Unique Features / Innovations | 8 | **9** |
| **Total** | **83/100** | **46/100** |

---

## Verdict

**Archon's search subsystem is a production-grade RAG system. Marveen's is a lightweight memory store.** The score gap (83 vs 46) reflects that difference — it is not a fair head-to-head fight on document search.

---

## Opportunities to Build the Best Search System in the World

The most valuable takeaways from Marveen are ideas Archon currently lacks.

### 1. Salience / Temporal Decay on Search Results
Marveen's hot/warm/cold/shared tiers + access-boost decay model is simple and powerful. Archon has no concept of document or chunk "recency" or "frequently accessed" weighting. Adding a `salience` score to `ChunkRecord` that decays over time and is boosted on retrieval access would significantly improve result relevance for long-lived collections.

### 2. Semantic Memory Tiers
Marveen distinguishes `semantic` vs `episodic` memory sectors. Archon treats all chunks identically. Introducing a metadata tag (`recent_session`, `permanent_knowledge`, `pinned`) and weighting tier in the final ranking formula would let users control what always surfaces vs. what fades.

### 3. Metadata Filters at Search Time
Neither system has metadata filters at query time. The highest-value missing feature in Archon is `filter_by={"source_path": "*.py", "indexed_after": "2026-01-01"}` on `hybrid_search()`. LanceDB supports predicate pushdown natively.

### 4. Replace the O(n) Vector Scan with sqlite-vec (for Marveen)
If Marveen wants better vector search without migrating to LanceDB, the `sqlite-vec` extension (pure C, no dependency) gives ANN inside SQLite with minimal code change.

### 5. Per-Collection Embedding Models
Archon should support a per-collection `embedding_model` config override. Multi-domain collections (code vs. prose vs. images) benefit from domain-specific models. The `embedding_model` field already exists in `CollectionMeta` — wire it into the ingest and validation paths.

### 6. Query Expansion / HyDE
Neither system does Hypothetical Document Embedding (HyDE): generate a hypothetical answer to the query → embed the answer → search with that vector. This is a well-proven technique for improving recall when user queries are short and under-specified. Add an optional `query_expansion=True` flag to `Pipeline.search()`.

### 7. Incremental FTS Rebuild
Archon currently does a full FTS rebuild on every `ingest_directory()` call. Switch to incremental FTS updates: LanceDB's `table.create_index(..., replace=False)` is reportedly additive #Unverified (LanceDB API semantics should be confirmed against current LanceDB docs before acting). The bottleneck is only in bulk directory ingestion — individual `ingest_file()` already skips the rebuild.

### 8. Chunk-Level Access Logging for Feedback Loop
Marveen's salience decay requires knowing which memories were accessed. Add an access-log table to Archon's LanceDB (`chunk_id`, `accessed_at`, `query`) and use access frequency to re-weight RRF scores. This turns Archon's search into a learning system.

### 9. Security Profiles for Search Collections
Marveen's per-agent filesystem allow/deny lists are a strong model. Archon should support per-collection access control: which Claude sessions can read which collections. The current model is all-or-nothing.

### 10. Streaming Search Results
Neither system supports streaming. For large reranker runs, returning the first `top_k_return` results as they score (rather than waiting for full cross-encoder pass) would improve perceived latency significantly.
