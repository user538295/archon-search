**Purpose**: Competitive analysis of `archon-search` versus major local and self-hosted search/RAG systems to identify strategic gaps.
**Audience**: Maintainers planning future Search architecture and roadmap work.
**Status**: Draft — 12-system comparison integrated across all dimensions on 2026-05-21; Archon scores updated 2026-06-28
**Last reviewed**: 2026-06-28 / **Next review**: 2026-12-28

> **Caveat**: Archon claims have been verified against `archon_search/` source. Rows inherited from the 2026-04-29 comparison (AnythingLLM, PrivateGPT, Kotaemon, mem0, R2R) still contain some #Unverified upstream version/default/throughput/test-count claims from that earlier pass. Rows source-refreshed on 2026-05-21 (`context-engine`, `abmind`, `TencentDB-Agent-Memory`, `MARM-Systems`, `docmost`, `agentmemory`) were checked against GitHub API metadata plus pinned source clones. Scores are comparative planning judgments, not reproduced benchmark results.

# Search System Deep Comparison: Archon vs. the Field

> **Date:** 2026-04-29; extended-system integration 2026-05-21
> **Scope:** Archon search system benchmarked against eleven active systems spanning local RAG, AI memory layers, agent-memory servers, collaborative knowledge workspaces, and production RAG frameworks.
> **Systems compared:** Archon · AnythingLLM (v1.12.1) · PrivateGPT (v0.6.2) · Kotaemon · mem0 · R2R (v3.6.6) · Emmimal/context-engine · aksika/abmind · Tencent/TencentDB-Agent-Memory · Lyellr88/MARM-Systems · docmost/docmost · rohitg00/agentmemory

---

## Framing

The systems occupy different parts of the design space:

| System | Primary use case | Language | Core retrieval / memory stack |
|--------|-----------------|----------|-----------------|
| **Archon** | Standalone hybrid retrieval + routing server (REST + MCP) over local file collections | Python / asyncio | LanceDB + fastembed + chonkie + docling/markitdown/trafilatura (custom pipeline) |
| **AnythingLLM** | Desktop/self-hosted chat over documents; broad LLM provider matrix | Node.js | LangChain TextSplitter + LanceDB (or 9 alt vector DBs) |
| **PrivateGPT** | Offline-first, OpenAI-compatible RAG server | Python | LlamaIndex 0.11 |
| **Kotaemon** | Research-oriented document QA with multi-modal PDF and GraphRAG | Python | LlamaIndex + theflow (custom pipeline) |
| **mem0** | AI agent memory layer; stores extracted facts, not raw documents | Python / TypeScript | Custom + pluggable vector store |
| **R2R** | Production-grade self-hosted RAG API; multi-tenant, full-stack | Python / asyncio | Custom FastAPI + pgvector |
| **context-engine** | Lightweight prompt-context assembly library with retrieval, memory, compression, and token budgeting | Python | Custom TF-IDF / keyword / optional sentence-transformers pipeline |
| **abmind** | Persistent agent-memory system with SQLite recall stages, lifecycle maintenance, and safety gates | TypeScript / Node.js | SQLite FTS/trigram/embedding/signature/entity-graph recall |
| **TencentDB Agent Memory** | Layered agent memory with raw conversations, atoms, scenarios, persona, and task-context offload | TypeScript / Node.js | SQLite + `sqlite-vec` + FTS5 + LLM extraction |
| **MARM-Systems** | MCP-native persistent memory server with sessions, logs, notebooks, dashboard, and response budgeting | Python | SQLite + sentence-transformers semantic recall + text fallback |
| **docmost** | Collaborative wiki/knowledge workspace with permission-aware search and real-time document lifecycle events | TypeScript / Node.js | PostgreSQL FTS; enterprise-referenced semantic/AI paths |
| **agentmemory** | Coding-agent memory server with hooks, MCP/REST, hybrid search, graph retrieval, viewer, lifecycle, and audit | TypeScript / Node.js | BM25 + vector + graph retrieval with RRF |

Fair comparison requires acknowledging mem0's structural difference: it stores LLM-extracted atomic facts, not document chunks. Its retrieval pipeline is evaluated on its own terms where relevant, and the apples-to-oranges gap is called out.

## All Products At A Glance

This table is the quick scan across all twelve systems. It is intentionally row-oriented so every newly added system can be compared against the original field without hunting through a separate appendix.

| System | Product type | Retrieval / memory shape | Agent / API surface | Strongest differentiator | Weakest verified or flagged point | Highest-value Archon idea |
|---|---|---|---|---|---|---|
| **Archon** | Standalone local hybrid retrieval + routing server | LanceDB vector + FTS + RRF + cross-encoder rerank; collection routing; HyDE/RAG Fusion optional via Anthropic API | REST + MCP (17 tools) + CLI; key management with rotation | Per-collection embedding model, HyDE, RAG Fusion, multilingual with fasttext, code-symbol context via tree-sitter, heading/section/page-number extraction, key rotation, scheduled backup, maintenance loop, container (CPU+GPU GHCR) | Still no streaming, SDK, or UI; LanceDB single-node hard limit; HyDE/RAG Fusion require a paid Anthropic API key; E0e multi-collection filters not yet shipped | Keep the retrieval core; add higher-level context, memory, graph, and explainability surfaces |
| **AnythingLLM** | Desktop/self-hosted document chat | Vector search over workspaces; no verified native FTS in this document | OpenAI-compatible API, UI, many LLM providers, MCP client consumption | Product UX and provider breadth | Search subsystem is weaker than its UI/product shell | Borrow the broad provider/onboarding polish without copying search architecture |
| **PrivateGPT** | Offline-first OpenAI-compatible RAG server | LlamaIndex vector retrieval; no native hybrid search in this comparison | OpenAI-compatible REST + Gradio UI | Simple local/offline deployment posture | Low default retrieval depth and sync HTTP handlers are quality/perf risks | Preserve offline-first install simplicity while avoiding weak retrieval defaults |
| **Kotaemon** | Research document QA and multimodal PDF app | Vector + optional FTS/GraphRAG variants; layered rerankers | Gradio UI; MCP client consumption | Rich document QA features, multimodal PDF, citation panel, GraphRAG options | Sparse tests and naive FTS/vector fusion in the compared path | Add optional GraphRAG and richer evidence/citation UX after core explainability |
| **mem0** | Agent memory layer, not document RAG | LLM-extracted facts, vector/keyword/entity graph, mutation history | Python/TS SDKs, hosted/self-hosted MCP, webhooks | Memory correctness, scoping, TTL, audit/versioning | Not a document-ingestion system; health/ops surface is thin in OSS comparison | Add memory lifecycle metadata and audit/history to Archon memory collections |
| **R2R** | Production self-hosted RAG platform | Postgres/pgvector + FTS + configurable RRF, HyDE, RAG Fusion, GraphRAG | FastAPI, SDKs, CLI, dashboard, streaming | Production architecture, graph/hybrid strategies, multi-tenancy, task queue | No MCP server; Postgres-only storage model | Add HyDE/RAG Fusion and production explainability while keeping local-first deployment |
| **context-engine** | Prompt-context assembly library | Keyword/TF-IDF/hybrid retrieval, memory mix-in, compression, token slots | Python library only | Returns a context packet with diagnostics, not only hits | No tests/package metadata found; in-process memory; random embedding fallback if optional model missing | Build a budget-aware `context_packet` endpoint above `search_with_context` |
| **abmind** | Persistent agent-memory system | SQLite FTS/trigram/original-language/embedding/signature/summary/entity-graph recall | CLI, library, MCP server, host integrations | Typed memories, trust/integrity/credibility, sleep maintenance, injection scanning | Young project; docs/code drift; some integrations alpha/unverified | Add lifecycle jobs, fuzzy recall stages, classification, safety gates, and auditable memory maintenance |
| **TencentDB Agent Memory** | Layered symbolic agent memory | L0 raw conversations -> L1 atoms -> L2 scenarios -> L3 persona; SQLite hybrid recall | Gateway/API, host hooks, OpenClaw/Hermes-oriented integration | Inspectable memory layers and Mermaid task-context offload | Benchmark claims not reproduced; host-patch/offload behavior is environment-sensitive | Add layered memory artifacts with deterministic drill-down to raw evidence |
| **MARM-Systems** | MCP-native memory server | SQLite sessions/logs/notebooks + semantic recall + text fallback | Compact MCP tool surface, dashboard, API | Lean MCP UX, notebooks, local dashboard, response-size controls | Vector recall scans recent rows rather than a dedicated vector index; keyword classification | Keep MCP tools compact; add response-budget metadata and a local admin/debug UI |
| **docmost** | Collaborative wiki / knowledge workspace | Permission-aware Postgres FTS over pages/attachments; lifecycle indexing events | Web app, collaboration gateway, enterprise AI/search hooks | Real-time editing, permissions, snippets, history, backlinks/transclusions | Public OSS semantic/AI search path not fully verifiable; English FTS default | Make ACLs, content lifecycle events, and graph edges first-class retrieval contracts |
| **agentmemory** | Coding-agent memory server | BM25 + vector + graph retrieval with RRF, diversification, optional rerank | Agent hooks, MCP/REST, viewer, audit/provenance tools | Agent-native capture, graph/provenance retrieval, leases/signals, benchmarks as product story | Benchmark claims not reproduced; large tool/API surface and `iii-engine` coupling | Add agent capture adapters, graph/provenance as a third stream, and `verify_result` endpoints |

## All-Product Dimension Matrix

This is the primary comparison table for planning. It compares every product on the same dimensions, and the dimension sections below repeat that all-product structure with deeper per-dimension detail.

| System | Architecture | Ingestion | Search / recall quality | Storage | API / agent surface | Ops / quality | Memory / lifecycle | What Archon should copy |
|---|---|---|---|---|---|---|---|---|
| **Archon** | Layered Python service with parser → chunker → embedder → store → reranker → pipeline plus router/server/CLI | Strong local file parsing, streaming chunking, 8 new file types (E0a), file size guard (E0d), watch, crash recovery | Strong vector + FTS + RRF + default cross-encoder rerank + collection routing; HyDE (C4) and RAG Fusion (C5) opt-in via Anthropic API; multilingual (C2); metadata filters (A2) | LanceDB embedded; local-first but single-node; backup/export/import/restore (D2); schema migration (D3) | REST, 17 MCP tools (D9), CLI; key rotation + revocation (D7) | Key rotation, maintenance loop, scheduled backup, schema migration, background model validation, container+GHCR, hashed telemetry — strong ops story | Collection-level, not agent-memory-native yet | Keep core; add context packets, graph/provenance, lifecycle memory |
| **AnythingLLM** | Product monorepo centered on chat/workspaces/providers | Broad connector/file coverage | Weaker search core; vector-first, no verified native hybrid search here | Many vector DB choices | UI, OpenAI-compatible API, MCP client | Product-friendly install/UI, weak search test evidence | Workspace conversation context | Provider matrix and onboarding polish |
| **PrivateGPT** | Clean Python/LlamaIndex service split | Offline document ingest | Simple vector RAG; weak default retrieval depth | Pluggable local/remote stores | OpenAI-compatible API, Gradio | Offline-friendly, but sync routes and Python cap | Minimal | Offline deployment simplicity without weak defaults |
| **Kotaemon** | Research app + headless library with custom pipelines | Rich documents, OCR/multimodal PDF options | Strong feature breadth: GraphRAG, rerankers, citation UX; fusion quality flagged | Chroma/LanceDB/Milvus/Qdrant options | Gradio, MCP client | Sparse tests and single-app scaling limits | Conversation-aware query reformulation | GraphRAG and evidence UX |
| **mem0** | Agent memory library/platform, not document RAG | Fact extraction, not full document ingestion | Strong memory recall domain: vector/keyword/entity graph, conflict handling | Many vector/graph backends | SDKs, hosted/self-hosted MCP, webhooks | Limited OSS ops surface | Excellent memory scoping, TTL, history, audit | Memory lifecycle, audit, scoped recall |
| **R2R** | Production FastAPI/Postgres platform | Strong document ingestion and distributed jobs | Strong hybrid RRF, HyDE, RAG Fusion, GraphRAG | PostgreSQL + pgvector only | REST, SDKs, CLI, dashboard, streaming | Strong production posture | Conversation sessions and agents | HyDE/RAG Fusion, graph, production workflows |
| **context-engine** | Small Python library, not a server | No durable document ingestion | Context assembly over keyword/TF-IDF/hybrid retrieval; compression is the core value | In-process only | Python library | No tests/package metadata found | In-process memory with decay | Token-budgeted context packet diagnostics |
| **abmind** | TypeScript agent-memory system | Stores typed memories, not documents | Multi-stage recall: FTS/trigram/original-language/embedding/signature/summary/entity graph | SQLite | CLI, library, MCP, host hooks | Young project; docs/code drift | Strong lifecycle, classification, trust/integrity, injection scanning | Fuzzy recall, safety gates, auditable maintenance |
| **TencentDB Agent Memory** | Layered symbolic memory architecture | Captures conversations/tool context into memory layers | Hybrid local recall plus L0→L3 drill-down | SQLite + `sqlite-vec` + FTS5 | Gateway/API and host hooks | Benchmark claims not reproduced; host patch sensitivity | Strong layered memory and persona/scenario abstraction | Raw→fact→scenario→profile layers with provenance |
| **MARM-Systems** | MCP-first memory server | Sessions/logs/notebooks, not document pipeline | Semantic recall with text fallback; simple vector path | SQLite | Compact MCP tools, API, dashboard | Practical response limits; vector index is simple | Good session/log/notebook model | Lean MCP surface, response budget, local admin UI |
| **docmost** | Mature collaborative workspace | Pages, attachments, history, collaboration events | Permission-aware Postgres FTS; semantic/AI public behavior not fully verifiable | PostgreSQL | Web app/collaboration gateway; enterprise AI hooks | Mature app posture | Knowledge lifecycle, backlinks, transclusions | ACL-aware retrieval and lifecycle-triggered indexing |
| **agentmemory** | Agent-native memory server | Captures coding-agent sessions, tools, decisions | BM25 + vector + graph with RRF, diversification, optional rerank | `iii-engine`-backed state; graph/vector concepts | Hooks, MCP/REST, viewer, audit tools | Large surface; benchmarks not reproduced | Excellent lifecycle, provenance, leases/signals | Agent capture, graph/provenance stream, `verify_result` |

---

The dimension-specific tables below compare all twelve systems directly. Rows are products, not attributes, so every section remains readable while still covering the complete field.

## Dimension 1 — Architecture & Design

| System | Architecture pattern | Strength | Design caveat | Score |
|---|---|---|---|---:|
| **Archon** | Layered Python pipeline with server, router, CLI, jobs, ACL, and telemetry modules around the retrieval core. | Clean separation of parser/chunker/embedder/store/reranker/pipeline makes the search core testable and replaceable. | LanceDB embedded storage keeps the system local-first but constrains concurrent writers and horizontal scaling. | 8 |
| **AnythingLLM** | Node.js product monorepo around collector, server, and frontend services. | Strong product shell and broad provider architecture. | No TypeScript, factory/switch-heavy extension points, and weaker dependency-injection discipline. | 5 |
| **PrivateGPT** | Python service using LlamaIndex and dependency-injected components. | Clean component/service split and OpenAI-compatible mental model. | Tightly coupled to LlamaIndex version constraints and sync handlers on FastAPI paths. | 7 |
| **Kotaemon** | Research app plus headless library using LlamaIndex and `theflow` pipelines. | Flexible experiment surface for GraphRAG, multimodal, and document QA workflows. | Some long pipeline modules and framework-specific abstractions make production hardening harder. | 6 |
| **mem0** | Agent-memory library/platform with pluggable LLM, embedder, vector store, and history store. | Adapter-driven memory design is clean and portable across backends. | It is structurally a memory system, not a full document search service. | 7 |
| **R2R** | Layered FastAPI/Postgres platform with providers, services, routers, task queue, and SDKs. | Most credible production architecture competitor to Archon. | Postgres-only design is powerful but less local-first and less storage-pluggable. | 8 |
| **context-engine** | Small Python library that builds prompt context packets from retriever, memory, reranker, and compressor components. | Very simple architecture around the exact product primitive agents need: assembled context. | Not a service, no packaging metadata found, no durable storage boundary. | 4 |
| **abmind** | TypeScript agent-memory system around SQLite, recall engine, sleep maintenance, MCP, and integrations. | Strong separation between storage, recall, safety, and maintenance concepts. | Young project with documented code/docs drift and integration maturity caveats. | 6 |
| **TencentDB Agent Memory** | Layered symbolic memory system: raw conversations, atoms, scenarios, persona, and offload maps. | Clear memory abstraction stack with drill-down intent. | Host hooks/runtime patching make part of the value environment-sensitive. | 7 |
| **MARM-Systems** | Python MCP-first memory server with SQLite-backed core and dashboard. | Focused, understandable architecture with a compact tool surface. | Search/indexing internals are simpler than product positioning suggests. | 6 |
| **docmost** | Mature TypeScript collaborative workspace with server, client, collaboration gateway, permissions, and search. | Strong workspace architecture and lifecycle modeling. | Public OSS search architecture is mostly FTS; semantic/AI paths are enterprise-referenced. | 8 |
| **agentmemory** | TypeScript coding-agent memory server with hooks, MCP/REST, viewer, graph/vector/BM25 retrieval, and lifecycle modules. | Best agent-native architecture pattern in the field. | Large surface and `iii-engine` coupling increase maintenance/adoption risk. | 7 |

---

## Dimension 2 — Indexing / Ingestion Pipeline

| System | Ingestion model | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | Local file ingestion with parser, chunker, embedding, LanceDB insert, state tracking, watcher, and config-driven chunking. | Strongest local ingest reliability in this field: idempotent file hashes, watch mode, crash recovery, auto-reindex on chunk-size changes. Streaming/incremental chunking (D4) avoids full-document materialisation. 8 new file types via markitdown (E0a): .doc, .xls, .ppt, .odt, .rtf, .epub, .eml, .msg. File size guard (E0d): configurable `max_file_mb`; REST returns 413, MCP returns `code="file_too_large"`, CLI exits non-zero, watcher continues. | No native web/GitHub/YouTube connector layer and no VLM PDF path. | 9 |
| **AnythingLLM** | Broad document and connector ingestion through product workspace flows. | Best connector breadth among the document-chat products in this comparison. | Vector cache is content-blind in the compared notes; ingestion state recovery is weaker. | 7 |
| **PrivateGPT** | Offline document ingestion through LlamaIndex readers. | Straightforward offline ingest story. | Defaults are less configurable and duplicate/reingest handling is weaker. | 5 |
| **Kotaemon** | Manual/index-management ingestion with rich document readers, OCR, and multimodal PDF options. | Strong document-type breadth and research-grade parsing options. | Synchronous/manual workflow and weaker crash recovery. | 7 |
| **mem0** | Stores extracted memories/facts rather than ingesting full documents. | Good dedup and memory add semantics for its domain. | Not a document-ingestion pipeline; score is intentionally low for this dimension. | 2 |
| **R2R** | Production document ingestion with task queue, batch embedding, configurable chunking, and broad format support. | Strongest production ingestion runner and job orchestration. | Chunk-level content-hash dedup is less central than in Archon. | 8 |
| **context-engine** | No durable ingestion pipeline; caller supplies retriever documents/memory. | Useful as an in-process context assembly example. | No document ingestion, no storage import path, no watcher/recovery. | 1 |
| **abmind** | Ingests typed memory records with classification, signatures, embeddings, and safety metadata. | Good memory ingestion model with security/lifecycle fields. | Not a document corpus ingester; integration docs are still young. | 2 |
| **TencentDB Agent Memory** | Captures conversations/tool context into layered memory artifacts. | Strong event-to-memory abstraction. | Not a general file ingestion system; depends on host capture hooks. | 2 |
| **MARM-Systems** | Stores sessions, logs, notebooks, and memories in SQLite. | Practical memory capture and notebook promotion workflow. | Not a full document indexing pipeline. | 2 |
| **docmost** | Ingests pages, attachments, collaboration saves, history, and lifecycle events. | Best workspace-content lifecycle ingestion; attachments and page saves trigger downstream work. | It is workspace-native, not a standalone search ingester; semantic indexing is not fully verifiable in OSS. | 7 |
| **agentmemory** | Captures coding-agent sessions, tool calls, decisions, lessons, and memory observations through hooks. | Best agent-event ingestion model. | Not a general document parser; large hook surface adds maintenance cost. | 3 |

---

## Dimension 3 — Search Quality

| System | Search / recall method | Strong feature | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | LanceDB vector + FTS with RRF, default cross-encoder rerank, context-window retrieval, multi-collection routing (centroid default; description-embedding hybrid blend opt-in via `routing_strategy = "hybrid"`, B4), server-side embed-once multi-collection search (B3), HyDE (C4), RAG Fusion (C5), multilingual fasttext detection (C2), metadata filters (A2), and explain endpoint with per-stage timings (A4, B1). | Best local search core in the comparison. Chunk-level enrichment: heading/section extraction (C3a), page-number extraction (C3b), code-symbol context via tree-sitter (C3c). | HyDE and RAG Fusion require a paid Anthropic API key and are opt-in extras; no GraphRAG; E0e multi-collection metadata filters not yet shipped; no graph/provenance stream. | 9 |
| **AnythingLLM** | Workspace vector search with optional reranking. | Good enough for product chat UX. | No verified native FTS/hybrid path in this document. | 4 |
| **PrivateGPT** | LlamaIndex vector search with sentence-window context. | Simple offline RAG retrieval. | Low default `similarity_top_k` and no hybrid search weaken recall. | 5 |
| **Kotaemon** | Vector, optional FTS, rerankers, query decomposition, and GraphRAG variants. | Richest research search palette. | Fusion quality and production reliability are weaker than the feature list. | 8 |
| **mem0** | Memory recall over vector, BM25/keyword, and entity graph. | Good memory-domain recall with structured scopes. | Not comparable to document RAG on chunk retrieval; fusion details are less transparent. | 6 |
| **R2R** | Postgres vector + FTS with configurable RRF, HyDE, RAG Fusion, rerank, and GraphRAG. | Strongest production retrieval strategy set. | Reranking depends on external TEI configuration in the compared notes. | 9 |
| **context-engine** | Keyword, TF-IDF, and optional hybrid retrieval feeding compression/context assembly. | Query-aware context selection is valuable. | Retrieval is lightweight and demo-oriented; random embedding fallback is unsafe for production. | 4 |
| **abmind** | FTS, trigram, original-language fallback, embeddings, signatures, consolidated summaries, and entity graph. | Best fuzzy/human-memory recall stages. | Young implementation; docs and code disagree on exact layer count. | 7 |
| **TencentDB Agent Memory** | SQLite FTS/vector hybrid recall over layered memories. | Good drill-down from persona/scenario to raw evidence. | Benchmark claims were not reproduced. | 7 |
| **MARM-Systems** | Semantic recall with text fallback over memories/logs/notebooks. | Practical MCP recall for personal/team memory. | Vector recall scans recent rows rather than using a dedicated vector index. | 5 |
| **docmost** | Permission-aware Postgres FTS with ranking/highlight snippets over pages and attachments. | Strong ACL-aware search UX in a real workspace. | Public OSS semantic/vector behavior is not fully verifiable. | 6 |
| **agentmemory** | BM25 + vector + graph retrieval with RRF, session diversification, and optional rerank. | Best agent-memory retrieval stack among the new systems. | Benchmarks were not reproduced; large feature surface complicates trust. | 8 |

---

## Dimension 4 — Embedding Model Choices

| System | Embedding / reranking posture | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | fastembed local embeddings plus fastembed cross-encoder reranker; model configurable globally and per-collection (C1 fully wired). Tiered install profiles (C0): minimal/balanced/max with disk-space checks and Jina CC-BY-NC-4.0 license gate for multilingual. | Strong local CPU/GPU-friendly default, dimension guard, and now fully wired per-collection model support — ingest, search, and sync all consult `CollectionMeta.active_embedding_model`; mismatch raises `ModelValidationError`. | Only fastembed-supported models are available; no sentence-transformers path. | 9 |
| **AnythingLLM** | Xenova/Transformers default plus broad provider options. | Strong provider matrix for users. | Node ONNX path is CPU-bound; search quality still lacks hybrid foundation. | 6 |
| **PrivateGPT** | HuggingFace/LlamaIndex embedding model configuration. | Works well for offline embeddings. | Default dimension/config footguns are noted in earlier table. | 6 |
| **Kotaemon** | OpenAI/fastembed/Cohere/Voyage/TEI-oriented provider set. | Strongest embedding/reranker provider breadth. | Operational complexity follows from many external options. | 8 |
| **mem0** | OpenAI default with optional local/provider alternatives. | Flexible enough for memory use cases. | README itself flags default embedding as suboptimal for hybrid memory search. | 5 |
| **R2R** | LiteLLM embeddings and external TEI rerank support. | Production-configurable provider posture. | Manual dimension matching and external reranker service add operational risk. | 7 |
| **context-engine** | Optional sentence-transformers; otherwise lightweight lexical/TF-IDF behavior. | Does not require a model for basic demos. | Random embedding fallback when optional model is absent is not production safe. | 3 |
| **abmind** | Optional embedding recall alongside lexical, trigram, signatures, and summaries. | Embeddings are one stage rather than the only recall path. | Embedding path depends on optional local/service setup. | 6 |
| **TencentDB Agent Memory** | Embedding-backed SQLite vector recall with provider metadata and fallback posture. | Hybrid memory recall does not collapse if embeddings are unavailable. | Node `sqlite-vec` and provider setup are environment-sensitive. | 6 |
| **MARM-Systems** | sentence-transformers semantic embeddings with text fallback. | Simple local semantic recall. | No dedicated vector index and less provider sophistication. | 5 |
| **docmost** | Public OSS path is primarily Postgres FTS; AI/embedding settings appear enterprise-referenced. | FTS works without model dependency. | Public semantic/vector implementation was not fully verifiable. | 4 |
| **agentmemory** | Vector retrieval as one stream beside BM25 and graph; optional rerank. | Good multi-stream posture for agent memory. | Details depend on `iii-engine` configuration and advanced features may be gated. | 7 |

---

## Dimension 5 — Storage Backend

| System | Storage backend | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | LanceDB embedded plus collection metadata/state under local runtime directory. Export/import/backup/restore with manifest, schema-version check, archive rotation, and scheduled backup loop (D2). Schema migration tooling (D3) with documented rollback rules and `STORE_SCHEMA_VERSION` bump policy — no forced full re-ingest. Incremental FTS maintenance (C6): `optimize_fts()` is O(delta), replacing O(collection) full rebuilds. | Fast local vector/FTS store with simple deployment, real backup/export story, and migration safety. | LanceDB embedded — no concurrent writers and no horizontal scale. | 8 |
| **AnythingLLM** | LanceDB default with many alternative vector DB options and SQLite product metadata. | Broad vector-store choice. | Default embedded/local posture still has multi-process and backup limitations. | 6 |
| **PrivateGPT** | Qdrant/Chroma/Postgres/Milvus/ClickHouse options. | Good backend flexibility. | Search quality does not exploit hybrid storage as strongly as Archon/R2R. | 7 |
| **Kotaemon** | Chroma/LanceDB/Milvus/Qdrant/in-memory plus optional graph stores. | Good experimentation storage breadth. | Production safety depends heavily on chosen backend. | 6 |
| **mem0** | Many vector backends plus graph stores and SQLite history in OSS paths. | Strongest memory storage flexibility. | Backend breadth increases operational/test matrix complexity. | 9 |
| **R2R** | PostgreSQL + pgvector + FTS + graph tables. | Best production-operable storage model and standard backup story. | No alternative store path; heavier than Archon's local daemon model. | 8 |
| **context-engine** | In-process memory/doc lists only. | Simple for library demos. | No durable index/storage layer. | 1 |
| **abmind** | SQLite schema with FTS/trigram/signature/embedding/entity memory data. | Strong local inspectable memory schema. | Local SQLite limits multi-user production scale without more architecture. | 6 |
| **TencentDB Agent Memory** | SQLite + `sqlite-vec` + FTS5 for layered memories. | Good local-first layered memory persistence. | Environment-sensitive extension availability. | 6 |
| **MARM-Systems** | SQLite sessions/logs/notebooks/memories with WAL posture. | Easy local inspectability and backup. | Dedicated vector indexing is weak. | 5 |
| **docmost** | PostgreSQL for pages, attachments, permissions, transclusions, and FTS. | Strong mature workspace storage and permission model. | Semantic/vector store behavior is not fully visible in OSS. | 8 |
| **agentmemory** | Engine-backed state with memory, graph, vector, audit, and session concepts. | Good provenance/audit-oriented memory storage. | Runtime dependency coupling makes portability less clear. | 7 |

---

## Dimension 6 — API / Integration Surface

| System | API / integration surface | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | FastAPI REST, FastMCP server (17 tools, D9), CLI, Bearer auth. 17 MCP tools: `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`, `export_collection`, `import_collection`, `create_key`, `list_keys`, `revoke_key`, `rotate_key`. Key management REST: `POST /keys`, `GET /keys`, `DELETE /keys/{id}`, `POST /keys/rotate`. Namespace auth flows per-request into every MCP tool closure. `expansion_used`/`expansion_warning` on SearchResponse (E0b). Cursor-paginated document listing (E0c). | Strong machine-facing search/API surface with full key management and namespace-aware MCP. | No web UI, no streaming endpoint, no Python SDK, no TypeScript SDK. | 8 |
| **AnythingLLM** | Web UI, OpenAI-compatible API, many providers, MCP client consumption. | Best user-facing integration product. | Does not expose itself as an MCP search server in this comparison. | 8 |
| **PrivateGPT** | OpenAI-compatible REST and Gradio. | Easy drop-in API story. | Less broad integration surface than AnythingLLM/R2R. | 6 |
| **Kotaemon** | Gradio app, settings UI, MCP client use in agents. | Good interactive research app surface. | No stable consumer REST API emphasized in the comparison. | 5 |
| **mem0** | Python/TS SDKs, hosted/self-hosted MCP, REST platform, webhooks. | Strong memory API and webhook story. | OSS/local ops/API health surface is thinner. | 7 |
| **R2R** | REST, SDKs, CLI, dashboard, streaming RAG/agent endpoints. | Strongest production API suite. | No MCP server. | 8 |
| **context-engine** | Python library only. | Easy to embed in application code. | No API, MCP, CLI, auth, or service surface. | 1 |
| **abmind** | Library, CLI, MCP server, Gemini/Claude/OpenClaw-style integrations. | Good agent-facing memory tool surface. | Some integrations are alpha/unverified in docs. | 7 |
| **TencentDB Agent Memory** | Gateway/API plus host hooks/integrations. | Good host-neutral core direction. | Integration reliability can depend on host internals. | 7 |
| **MARM-Systems** | Compact MCP tools, API, dashboard. | Excellent small MCP memory surface. | Less broad than agentmemory/mem0. | 7 |
| **docmost** | Full web app, collaboration gateway, workspace APIs, enterprise AI/search hooks. | Strongest human workspace surface. | Search-as-a-service integration is secondary to wiki product. | 7 |
| **agentmemory** | Hooks, MCP, REST, viewer, audit/provenance tools. | Best coding-agent integration surface. | Very large tool/API surface can overwhelm users and maintainers. | 8 |

---

## Dimension 7 — Operational Concerns

| System | Operational posture | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | `uv` dev flow, config TOML, API key bootstrap, service install paths, health/status, telemetry, job state. Key rotation with grace period (D7). Maintenance loop with three configurable policies: FTS optimization, orphan cleanup, failed-ingest retry with `FAILED_EXPIRED` terminal state (D5). Scheduled backup/restore with rotation (D2). Schema migration tooling (D3). Background model validation non-blocking at startup (D6); result in `GET /status` + `GET /ready`. Container support (C9): CPU (`:latest`) and NVIDIA GPU (`:gpu`) GHCR images; all runtime state on one volume; env-var driven config. Hashed doc_id telemetry with HMAC-SHA256 salt (D8). | Key rotation, maintenance, backup automation, schema migration, model validation diagnostics, container+GHCR, and hashed telemetry combine into a genuinely strong ops story. | No admin/debug UI; no self-update/doctor command; single-node. | 9 |
| **AnythingLLM** | Docker/Desktop/manual product deployment with admin UI settings. | Best end-user install/product posture. | Deep health/metrics and ingestion recovery are weaker. | 6 |
| **PrivateGPT** | Poetry/Docker offline profiles. | Simple offline deployment. | Limited health/monitoring and route blocking concerns. | 5 |
| **Kotaemon** | Docker/conda/uv research-app deployment. | Easy to run as an app. | Weak service/monitoring/crash-recovery posture. | 4 |
| **mem0** | Library/platform install with provider config. | Easy to embed in apps. | OSS memory library has limited health/ops surface. | 4 |
| **R2R** | Docker/Kubernetes, migrations, task queue, health/status, Sentry/logging hooks. | Strongest production ops posture. | Heavier stack than local-first Archon. | 8 |
| **context-engine** | Library-only. | Almost no operational burden. | Also no service operations, health, config, recovery, or packaging maturity. | 1 |
| **abmind** | CLI/MCP/local DB plus sleep maintenance. | Useful self-maintenance concepts. | Young ops story and optional dependency paths need hardening. | 5 |
| **TencentDB Agent Memory** | Local gateway/hooks/storage with degraded-mode thinking. | Good timeout/degraded-mode posture in design. | Host-specific behavior and local extension setup complicate ops. | 5 |
| **MARM-Systems** | MCP server with dashboard, SQLite, rate limiting, response-size controls. | Practical local operations and admin inspection. | Less robust indexing/search ops than Archon/R2R. | 6 |
| **docmost** | Mature web-app deployment with permissions/collaboration/workspace lifecycle. | Strong app operations and workspace admin model. | Search-specific diagnostics are not the product center in OSS. | 8 |
| **agentmemory** | Server, hooks, viewer, audit, benchmarks, large API/tool surface. | Good observability/product operations for agent memory. | Surface area and engine coupling increase ops risk. | 7 |

---

## Dimension 8 — Test Coverage & Code Quality

| System | Quality evidence | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | Project mandate for TDD/85% coverage; existing tests cover pipeline/store/server/jobs. | Strongest verified local code-quality posture in this document. | Some earlier line-count claims remain #Unverified. | 9 |
| **AnythingLLM** | Product codebase with linting, but test evidence in this comparison is weak. | Mature product surface. | Search-specific test coverage appears very weak in the earlier review. | 1 |
| **PrivateGPT** | Python quality tooling and route-level tests. | Cleaner than most product apps. | Coverage/quality still not search-depth focused enough. | 6 |
| **Kotaemon** | Declared Python testing/formatting tools. | Research features are extensive. | Sparse tests for the feature breadth. | 3 |
| **mem0** | `pytest`/`ruff` posture and CLI integration tests in prior notes. | Reasonable memory-library quality controls. | No published strict coverage gate in this comparison. | 5 |
| **R2R** | Integration-heavy suite, migrations, SDK tests, typed FastAPI/Pydantic code. | Strong integration testing direction. | Unit coverage and some code cleanup issues remain flagged. | 5 |
| **context-engine** | No tests or packaging metadata found in source review. | Small codebase is easy to audit manually. | Very weak formal quality signal. | 1 |
| **abmind** | Active TypeScript project with substantial modules. | Security/lifecycle ideas are explicit in code. | Young project and docs/code drift reduce confidence. | 5 |
| **TencentDB Agent Memory** | Active TypeScript repo with structured modules. | Clear architecture and source evidence for key concepts. | Benchmarks not reproduced; maturity is still early. | 5 |
| **MARM-Systems** | Active Python MCP server with docs/dashboard. | Focused scope helps maintainability. | Vector/search internals are simple; test depth not established here. | 4 |
| **docmost** | Large mature app with active development and structured server/client packages. | Strongest maturity signal among new systems. | Search-specific AI/semantic quality cannot be fully audited in OSS. | 6 |
| **agentmemory** | Large active codebase with benchmarks/docs/source modules for hooks, retrieval, audit. | Strong productized memory architecture. | Benchmark claims not reproduced; broad surface raises regression risk. | 6 |

---

## Dimension 9 — Performance & Scalability

| System | Performance / scalability posture | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | Async Python plus local LanceDB ANN/FTS and threaded CPU-bound embedding/rerank work. | High local performance with simple deployment. | Single-node embedded storage limits horizontal scale. | 8 |
| **AnythingLLM** | Node product process plus vector store provider choices. | Usable for workspace chat. | Single-process/CPU-bound parts and weak hybrid search limit serious retrieval throughput. | 4 |
| **PrivateGPT** | Backend-dependent vector scale with sync route caveats. | Can lean on Qdrant/Postgres backends. | Sync HTTP paths and low retrieval defaults limit quality/perf. | 5 |
| **Kotaemon** | Research app performance depends on chosen pipeline/backend. | Can use external APIs/backends for heavy features. | Single app process and synchronous paths constrain scaling. | 4 |
| **mem0** | Memory retrieval can scale through remote vector/graph stores. | Strong enough for memory-layer workloads. | Extra search-before-add and memory extraction costs matter at high write volume. | 7 |
| **R2R** | Stateless FastAPI plus Postgres/pgvector/FTS, task queue, and binary quantization. | Strongest production scalability story. | Heavier infra footprint. | 9 |
| **context-engine** | In-process library over small local document lists. | Low overhead for small prompt contexts. | No production indexing/scaling path. | 3 |
| **abmind** | SQLite local memory recall with multiple recall stages. | Good personal/local memory performance potential. | SQLite plus many stages need careful scaling tests. | 5 |
| **TencentDB Agent Memory** | Local SQLite/vector/FTS plus background/degraded behavior. | Good bounded local memory recall pattern. | Extension/runtime setup and host hooks affect performance. | 5 |
| **MARM-Systems** | SQLite memory server with semantic scan/fallback. | Fine for small-to-medium memory sets. | Recent-row vector scanning limits scale. | 4 |
| **docmost** | PostgreSQL-backed workspace search and collaboration lifecycle. | Mature database-backed app scaling pattern. | FTS is strong operationally but semantic retrieval scale is not verifiable in OSS. | 7 |
| **agentmemory** | Multi-stream search plus viewer/hooks/server. | Strong retrieval architecture for coding-agent memory. | Benchmarks not reproduced and broad feature path increases runtime complexity. | 7 |

---

## Dimension 10 — Unique Features & Innovations

| System | Distinctive innovation | Why it matters | Archon implication | Score |
|---|---|---|---|---:|
| **Archon** | Multi-collection centroid routing, pinned collections, default reranking, crash recovery, MCP search server. Plus: HyDE (C4), RAG Fusion (C5), multilingual retrieval with fasttext detection (C2), per-collection embedding models fully wired (C1), code-symbol context via tree-sitter (C3c), heading/section extraction (C3a), page-number extraction (C3b), cursor-paginated document listing (E0c), key rotation with grace period (D7), maintenance loop with policy config (D5), scheduled backup with rotation (D2), schema migration tooling (D3), container support with CPU+GPU GHCR images (C9), tiered install profiles (C0), real-model latency benchmark suite, incremental FTS/centroid maintenance (C6). | Qualitatively different feature set from a year ago: query expansion, multilingual, code-aware chunking, container, key rotation, maintenance automation, incremental FTS. | No GraphRAG; no streaming; no SDK; no UI; no agent capture adapters; no memory lifecycle/salience/decay. | 9 |
| **AnythingLLM** | Polished document-chat product, provider matrix, OpenAI-compatible endpoints, agent/plugin UX. | Shows user expectations around setup and breadth. | Improve product packaging and provider/onboarding experience. | 7 |
| **PrivateGPT** | Offline-first OpenAI-compatible local RAG. | Simplicity is a product feature. | Keep Archon simple to run even as advanced features grow. | 5 |
| **Kotaemon** | Multimodal PDF, citation panel, GraphRAG variants, sub-question pipelines. | Best research/evidence UX ideas. | Add evidence-rich UI and optional graph/multimodal modules. | 9 |
| **mem0** | LLM memory extraction, versioning, TTL, scopes, graph-backed memory. | Best established memory-domain ideas. | Add memory lifecycle/audit as first-class search metadata. | 9 |
| **R2R** | HyDE, RAG Fusion, GraphRAG, streaming citations, binary quantization, production RAG agents. | Best advanced production retrieval roadmap. | Adopt query expansion, graph, streaming, and quantization where they fit local-first constraints. | 9 |
| **context-engine** | Context packet with token-budget slots, compression, history/memory blend, diagnostics. | Converts search hits into prompt-ready context. | Add `context_packet` endpoint with budget accounting and dropped-context reasons. | 7 |
| **abmind** | Multi-stage fuzzy memory recall, trust/integrity/credibility, sleep maintenance, injection scanning. | Shows memory quality and safety are product features. | Add fuzzy fallback stages and pre-ingest memory safety checks. | 8 |
| **TencentDB Agent Memory** | L0-L3 memory layers and Mermaid task-context offload. | Shows how to compress long agent sessions without losing drill-down. | Add raw->fact->scenario->profile layers and source traceability. | 9 |
| **MARM-Systems** | Compact MCP memory tools, notebooks, dashboard, response-size limits. | Shows a lean MCP-first memory UX. | Keep MCP tools compact and add local admin/debug UI. | 7 |
| **docmost** | Real-time collaborative docs, permission-aware snippets, page history, transclusions/backlinks. | Shows search should respect human knowledge workflows and ACLs. | Make ACL decisions and content lifecycle events visible in search explanations. | 8 |
| **agentmemory** | Agent hooks, BM25+vector+graph RRF, viewer, leases/signals, audit/provenance verification. | Best complete agent-memory product pattern. | Add agent capture adapters, graph/provenance retrieval, and `verify_result`. | 9 |

---

## Dimension 11 — Memory / Agent Integration

This dimension is not present in most RAG systems but is core to how these systems interact with AI agents and LLM sessions.

| System | Memory / agent integration | Strength | Gap / caveat | Score |
|---|---|---|---|---:|
| **Archon** | MCP tools expose search/ingest/list/delete to capable agent clients. | Good agent access to search collections. | No conversation history, lifecycle memory, agent capture, or result verification yet. | 8 |
| **AnythingLLM** | Agent framework and workspace conversation behavior. | Strong chat-product agent UX. | Memory is tied to workspace/chat product, not general agent memory. | 6 |
| **PrivateGPT** | External clients call REST APIs; minimal agent memory. | Simple API compatibility. | Little native memory/lifecycle integration. | 3 |
| **Kotaemon** | ReAct/ReWoo agents, MCP client usage, conversation-aware query reformulation. | Useful research-agent workflows. | Memory is not the primary product model. | 6 |
| **mem0** | Purpose-built user/agent/app/run scoped memory with history, TTL, extraction, graph, SDKs/MCP. | Best memory lifecycle baseline. | Not a document search server. | 10 |
| **R2R** | Conversation sessions, RAG Agent, Research Agent, collections. | Strong agentic RAG workflows. | Less specialized than mem0/agentmemory for long-term memory lifecycle. | 7 |
| **context-engine** | In-process short/long memory blend with decay feeding context packets. | Good prompt-context memory pattern. | No durable/shared agent memory service. | 5 |
| **abmind** | Typed memories, recall stages, sleep maintenance, classification, injection safety, MCP/CLI. | Strong long-term memory lifecycle design. | Young and integration maturity is not fully proven. | 9 |
| **TencentDB Agent Memory** | L0-L3 layered memories, persona/scenario abstractions, host capture/offload. | Strong layered agent-memory model. | Depends on host hooks for full value. | 9 |
| **MARM-Systems** | MCP-native memories, sessions, logs, notebooks, summaries, dashboard. | Good lightweight shared memory model. | Recall/index sophistication is lower than mem0/agentmemory. | 8 |
| **docmost** | Workspace knowledge, permissions, history, backlinks/transclusions, comments. | Strong human knowledge lifecycle that can inform agent retrieval. | Not an agent-memory product. | 4 |
| **agentmemory** | Agent hooks, sessions, memories, consolidation, decay, leases/signals, audit/provenance. | Best coding-agent memory integration. | Large surface and benchmark claims need independent reproduction. | 10 |

---

## Extended Source Evidence

These are pinned-source evidence notes for systems source-refreshed on 2026-05-21. They are not a separate comparison group; their findings are integrated into the all-product tables above, the all-product scorecard below, and the unified opportunity table.

Each repository was assigned to one background investigator, then cross-checked against a shallow source clone and GitHub API metadata on 2026-05-21. Benchmark and adoption claims below are treated as upstream claims unless explicitly marked as reproduced; no competitor benchmark suite was run locally.

### Verification snapshot

| Repository | Pinned source snapshot | Verified public signals on 2026-05-21 |
|---|---|---|
| [`Emmimal/context-engine`](https://github.com/Emmimal/context-engine) | [`e0e84cb`](https://github.com/Emmimal/context-engine/tree/e0e84cbb33fb9cf2bc48df98517b1609435db27f) | 189 stars, 28 forks, MIT, pushed 2026-05-18 |
| [`aksika/abmind`](https://github.com/aksika/abmind) | [`cf74190`](https://github.com/aksika/abmind/tree/cf741906019e9075739a804d59b338e376fb61e3) | 15 stars, 3 forks, Apache-2.0, pushed 2026-05-19 |
| [`Tencent/TencentDB-Agent-Memory`](https://github.com/Tencent/TencentDB-Agent-Memory) | [`bfddda6`](https://github.com/Tencent/TencentDB-Agent-Memory/tree/bfddda6d3ef479ce727f291bc2c5990bd026bd34) | 3,726 stars, 298 forks, MIT in repo `LICENSE` (GitHub API returned `NOASSERTION`), pushed 2026-05-21 |
| [`Lyellr88/MARM-Systems`](https://github.com/Lyellr88/MARM-Systems) | [`8cb6918`](https://github.com/Lyellr88/MARM-Systems/tree/8cb69186f6e0db87bd9e8bab1eae6528e3af89d0) | 290 stars, 50 forks, MIT, pushed 2026-05-21 |
| [`docmost/docmost`](https://github.com/docmost/docmost) | [`13a7f13`](https://github.com/docmost/docmost/tree/13a7f1372fe9987fcee656d367cf2115aefcb91e) | 20,307 stars, 1,302 forks, AGPL-3.0 core with enterprise subdirectories, pushed 2026-05-21 |
| [`rohitg00/agentmemory`](https://github.com/rohitg00/agentmemory) | [`bc64107`](https://github.com/rohitg00/agentmemory/tree/bc641077913c0ac043e702a8f6519189e89b1721) | 15,555 stars, 1,289 forks, Apache-2.0, pushed 2026-05-21 |

### Evidence notes for extended systems

| System | Verified strong features | High-value idea for `archon-search` | Caveats |
|---|---|---|---|
| **context-engine** | A small Python context-assembly pipeline: retrieve, rerank, compress, enforce token slots, mix memory, and return a `ContextPacket` rather than only ranked hits. Evidence: [`README.md` components](https://github.com/Emmimal/context-engine/blob/e0e84cbb33fb9cf2bc48df98517b1609435db27f/README.md#L20-L36), [`ContextEngine.build()`](https://github.com/Emmimal/context-engine/blob/e0e84cbb33fb9cf2bc48df98517b1609435db27f/context_engineering.py#L112-L153), [`TokenBudget`](https://github.com/Emmimal/context-engine/blob/e0e84cbb33fb9cf2bc48df98517b1609435db27f/compressor.py#L247-L276). | Add a budget-aware `context_packet` / `answer_context` API above `search_with_context`: selected chunks, compressed text, slot accounting, diagnostics, and dropped-context reasons. This turns retrieval into prompt-ready context assembly. | No tests or packaging metadata found. Memory is in-process only, token counting is approximate, and hybrid retrieval falls back to random embeddings when `sentence-transformers` is absent ([source](https://github.com/Emmimal/context-engine/blob/e0e84cbb33fb9cf2bc48df98517b1609435db27f/retriever.py#L68-L76)). |
| **abmind** | SQLite-backed agent memory with typed memories, classification/trust/integrity/credibility, 4+ recall stages (FTS/trigram/original-language/embedding/signature/consolidated summaries/entity graph), sleep maintenance, prompt-injection scanning, and context compression. Evidence: [`README.md` architecture/features](https://github.com/aksika/abmind/blob/cf741906019e9075739a804d59b338e376fb61e3/README.md#L49-L110), [`recallSearch()`](https://github.com/aksika/abmind/blob/cf741906019e9075739a804d59b338e376fb61e3/src/recall-engine.ts#L226-L460), [`memory-db.ts`](https://github.com/aksika/abmind/blob/cf741906019e9075739a804d59b338e376fb61e3/src/memory-db.ts#L46-L107), [`injection-scanner.ts`](https://github.com/aksika/abmind/blob/cf741906019e9075739a804d59b338e376fb61e3/src/injection-scanner.ts#L1-L176). | Add a memory lifecycle layer: recall counts, last-recalled timestamps, promote/demote/merge/forget policies, validity windows for contradictions, and auditable maintenance jobs. Also add fuzzy recall paths (trigram, typo/keyboard fallback, original-language search) as explicit stages before semantic fallback. | Very young project, small public footprint, and docs drift from code (README says 4-layer recall; code includes signature and entity-graph stages). Some integrations are explicitly alpha/unverified in docs. |
| **TencentDB Agent Memory** | Layered, inspectable agent memory: L0 raw conversations, L1 atoms, L2 scenarios, L3 persona; lower layers preserve evidence while upper layers preserve structure. It also adds Mermaid task maps for short-term context offload and local SQLite + `sqlite-vec` hybrid recall. Evidence: [`README.md` highlights/layering](https://github.com/Tencent/TencentDB-Agent-Memory/blob/bfddda6d3ef479ce727f291bc2c5990bd026bd34/README.md#L27-L79), [`README.md` symbolic memory](https://github.com/Tencent/TencentDB-Agent-Memory/blob/bfddda6d3ef479ce727f291bc2c5990bd026bd34/README.md#L85-L105), [`README.md` config/features](https://github.com/Tencent/TencentDB-Agent-Memory/blob/bfddda6d3ef479ce727f291bc2c5990bd026bd34/README.md#L255-L340), [`memory-search.ts`](https://github.com/Tencent/TencentDB-Agent-Memory/blob/bfddda6d3ef479ce727f291bc2c5990bd026bd34/src/core/tools/memory-search.ts#L1-L82). | Add optional layered memory artifacts above collections: raw evidence -> extracted facts -> task/scenario summaries -> profile/persona. Every synthesized layer should keep deterministic source IDs so users and agents can drill back down. | README benchmark claims were not reproduced. Some value depends on host-specific hooks or runtime patches, especially context offload. Node `sqlite-vec` / FTS availability is environment-sensitive. |
| **MARM-Systems** | MCP-native persistent memory server with a compact 8-tool surface, session/log/notebook storage, semantic recall with text-search fallback, local dashboard, SQLite WAL posture, rate limiting, and MCP response-size controls. Evidence: [`README.md` purpose/features](https://github.com/Lyellr88/MARM-Systems/blob/8cb69186f6e0db87bd9e8bab1eae6528e3af89d0/README.md#L39-L58), [`README.md` MCP tools](https://github.com/Lyellr88/MARM-Systems/blob/8cb69186f6e0db87bd9e8bab1eae6528e3af89d0/README.md#L176-L209), [`README.md` dashboard/operations](https://github.com/Lyellr88/MARM-Systems/blob/8cb69186f6e0db87bd9e8bab1eae6528e3af89d0/README.md#L213-L297), [`memory.py`](https://github.com/Lyellr88/MARM-Systems/blob/8cb69186f6e0db87bd9e8bab1eae6528e3af89d0/marm-mcp-server/marm_mcp_server/core/memory.py#L213-L410). | Keep the MCP surface lean while adding richer behavior behind typed parameters. Add a local admin/debug UI for memories, collections, jobs, logs, and search diagnostics. Enforce response budgets with structured truncation metadata on MCP/REST context endpoints. | Its vector recall scans recent embedded memories rather than using a dedicated vector index; auto-classification is keyword-based. Dashboard direct DB writes are useful but create schema-drift/event-bypass risk. |
| **docmost** | Mature collaborative knowledge workspace: real-time editing, spaces, permissions, groups, comments, page history, attachments, embeds, translations, and search. OSS search uses permission-aware Postgres FTS with rank/highlight snippets and workspace/space/share/page filtering. Collaboration persistence queues page-history and AI/search update events. Evidence: [`README.md` features](https://github.com/docmost/docmost/blob/13a7f1372fe9987fcee656d367cf2115aefcb91e/README.md#L17-L29), [`search.service.ts`](https://github.com/docmost/docmost/blob/13a7f1372fe9987fcee656d367cf2115aefcb91e/apps/server/src/core/search/search.service.ts#L37-L139), [`persistence.extension.ts`](https://github.com/docmost/docmost/blob/13a7f1372fe9987fcee656d367cf2115aefcb91e/apps/server/src/collaboration/extensions/persistence.extension.ts#L98-L202), [`collaboration.gateway.ts`](https://github.com/docmost/docmost/blob/13a7f1372fe9987fcee656d367cf2115aefcb91e/apps/server/src/collaboration/collaboration.gateway.ts#L48-L80). | Make ACL and lifecycle events first-class retrieval contracts: search should explain space/share/page permission gates, and ingestion should distinguish content update, permission update, attachment extraction, history snapshot, and relationship update. Also index graph-like edges (hierarchy, backlinks, transclusions, contributors) for context expansion. | Public OSS search appears mostly Postgres FTS. Semantic/vector/AI search paths are feature-gated or enterprise-referenced; exact ranking/RAG behavior was not verifiable from the public clone. Default FTS uses English query functions. |
| **agentmemory** | Coding-agent memory product with agent hooks, MCP/REST, real-time viewer, benchmark-led positioning, triple-stream retrieval (BM25 + vector + graph with RRF), session diversification, optional rerank, lifecycle consolidation/decay, leases/signals, audit, and provenance verification. Evidence: [`README.md` agent integrations](https://github.com/rohitg00/agentmemory/blob/bc641077913c0ac043e702a8f6519189e89b1721/README.md#L96-L201), [`README.md` benchmarks](https://github.com/rohitg00/agentmemory/blob/bc641077913c0ac043e702a8f6519189e89b1721/README.md#L205-L247), [`hybrid-search.ts`](https://github.com/rohitg00/agentmemory/blob/bc641077913c0ac043e702a8f6519189e89b1721/src/state/hybrid-search.ts#L20-L240), [`README.md` lifecycle/search/viewer](https://github.com/rohitg00/agentmemory/blob/bc641077913c0ac043e702a8f6519189e89b1721/README.md#L759-L955). | Add agent-native capture integrations for Codex/Claude/Cursor/MCP clients; add graph/provenance as a third retrieval stream; add `verify_result` / `verify_memory` endpoints that trace an answer or memory back to source chunks, sessions, timestamps, confidence, and mutation history. | Benchmark claims were not reproduced. The public surface is large (many tools/endpoints), and advanced features are config-gated. Architecture is tied to `iii-engine`; Archon should copy product patterns, not the runtime dependency. |

### Cross-system synthesis

The highest-value ideas from the extended systems are product-surface and lifecycle ideas, not a replacement for Archon's existing vector + FTS + rerank core.

1. **Context packets, not just hits.** `context-engine` and MARM show the value of returning prompt-ready context with token budgets, compression, diagnostics, and truncation metadata. This should become a higher-level Archon endpoint above `search_with_context`.
2. **Layered memory with drill-down.** TencentDB Agent Memory, abmind, and agentmemory converge on the same pattern: raw evidence remains durable, extracted facts and summaries are convenience layers, and every layer needs provenance back to source.
3. **Memory lifecycle as ranking signal.** Recall counts, last-recalled timestamps, decay, promotion/demotion, contradiction invalidation, TTL, and merge/forget policies should become explicit metadata/ranking inputs rather than hidden heuristics.
4. **Agent-native capture.** agentmemory, TencentDB Agent Memory, abmind, and MARM all win adoption by fitting into agent lifecycle hooks/MCP/CLI flows. Archon should add optional capture adapters for coding-agent sessions, tool outputs, decisions, and task summaries.
5. **Recall stages beyond vector + FTS.** Archon's core hybrid retrieval is stronger than many competitors, but abmind and agentmemory show useful adjunct stages: trigram/fuzzy matching, original-language fallback, consolidated-summary search, graph traversal, and session diversification.
6. **Safety before storage.** abmind's prompt-injection scanner, secret redaction, classification-aware storage, and encrypted high-sensitivity memories map directly to agent-memory ingestion hardening.
7. **Human-operable search.** MARM, agentmemory, and docmost all provide inspectability surfaces: dashboards, viewers, source cards, history, replay, or permission-aware snippets. A world-class Archon needs a local debug/admin surface after `/explain` stabilizes.
8. **Benchmarks as product artifacts.** agentmemory's README turns reproducible metrics into positioning. Archon's eval harness should graduate from maintainer-only gate to published scorecards for retrieval, routing, context-packet quality, memory recall, and latency.

---

## Overall Scorecard

This scorecard compares all twelve systems on the same 11 dimensions. Rows inherited from the 2026-04-29 comparison remain #Unverified where upstream source facts were not refreshed; rows refreshed on 2026-05-21 are provisional source-review judgments. No competitor benchmark suite was reproduced locally.

| System | Category | Design | Ingest | Search | Embed | Store | API | Ops | Tests | Perf | Unique | Memory | Total / 110 | Read |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| **Archon** | Local hybrid search server | 8 | 9 | 9 | 9 | 8 | 8 | 9 | 9 | 8 | 9 | 8 | **94** | Best current local-first retrieval core |
| **R2R** | Production RAG platform | 8 | 8 | 9 | 7 | 8 | 8 | 8 | 5 | 9 | 9 | 7 | **86** | Best production RAG comparator |
| **agentmemory** | Coding-agent memory | 7 | 3 | 8 | 7 | 7 | 8 | 7 | 6 | 7 | 9 | 10 | **79** | Best agent-native memory/product lesson |
| **docmost** | Knowledge workspace | 8 | 7 | 6 | 4 | 8 | 7 | 8 | 6 | 7 | 8 | 4 | **73** | Best ACL/workspace lifecycle lesson |
| **mem0** | Agent memory layer | 7 | 2 | 6 | 5 | 9 | 7 | 4 | 5 | 7 | 9 | 10 | **71** | Best fact-memory lifecycle comparator |
| **TencentDB Agent Memory** | Layered agent memory | 7 | 2 | 7 | 6 | 6 | 7 | 5 | 5 | 5 | 9 | 9 | **68** | Best layered-memory/provenance lesson |
| **Kotaemon** | Research document QA | 6 | 7 | 8 | 8 | 6 | 5 | 4 | 3 | 4 | 9 | 6 | **66** | Best multimodal/GraphRAG evidence UX lesson |
| **abmind** | Persistent agent memory | 6 | 2 | 7 | 6 | 6 | 7 | 5 | 5 | 5 | 8 | 9 | **66** | Best fuzzy recall and safety-gated memory lesson |
| **AnythingLLM** | Document chat product | 5 | 7 | 4 | 6 | 6 | 8 | 6 | 1 | 4 | 7 | 6 | **60** | Best provider/UI/onboarding comparator |
| **PrivateGPT** | Offline RAG server | 7 | 5 | 5 | 6 | 7 | 6 | 5 | 6 | 5 | 5 | 3 | **60** | Best offline/OpenAI-compatible simplicity lesson |
| **MARM-Systems** | MCP memory server | 6 | 2 | 5 | 5 | 5 | 7 | 6 | 4 | 4 | 7 | 8 | **59** | Best lean MCP memory/admin UI lesson |
| **context-engine** | Context assembly library | 4 | 1 | 4 | 3 | 1 | 1 | 1 | 1 | 3 | 7 | 5 | **31** | Best token-budgeted context-packet pattern |

---

## Verdict

**Archon and R2R remain the strongest full RAG/search platforms, but the new systems change the roadmap priorities.** The highest-value gaps are no longer just HyDE, filters, GraphRAG, and storage scaling. The field now shows that a world-class search product also needs context packaging, memory lifecycle, provenance, safety gates, agent capture, and human-operable debugging.

| Category | Leading systems | Why they matter for Archon |
|---|---|---|
| **Full search/RAG platforms** | Archon, R2R | Archon has the strongest local-first hybrid core; R2R shows production-grade query expansion, graph, streaming, and task orchestration patterns. |
| **Document chat/workspace products** | AnythingLLM, PrivateGPT, Kotaemon, docmost | These systems show what users expect around UI, source snippets, collaboration, permissions, citations, and offline simplicity. |
| **Agent-memory systems** | mem0, agentmemory, TencentDB Agent Memory, abmind, MARM-Systems | These systems expose the missing Archon product layer: capture events, store memories, age/promote/verify them, and return explainable context. |
| **Context assembly** | context-engine, MARM-Systems, agentmemory | These show that ranked chunks are not enough; the product should return budget-aware, prompt-ready context with diagnostics and truncation reasons. |

The practical roadmap implication is direct: keep Archon's retrieval core as the base, then add the product layers competitors prove valuable: `context_packet`, `/explain`, graph/provenance retrieval, lifecycle memory metadata, agent capture adapters, safety gates, and a local debug/admin UI.

---

## Opportunities for Archon

The gap analysis identifies specific capabilities present in competitors that Archon currently lacks.

### Unified high-value gaps (implement soon)

| # | Gap | Best example | What to build |
|---|-----|-------------|---------------|
| 1 | ~~**HyDE / query expansion**~~ **SHIPPED (C4)** | R2R `hyde` strategy | ~~Add optional `query_expansion=True` flag to `Pipeline.search()`~~ Shipped: `hyde=true` on `/search`, `/explain`, and MCP tools. Requires `archon-search[hyde]` extra + Anthropic API key. Mutually exclusive with RAG Fusion. Silent fallback on LLM failure. |
| 2 | ~~**Metadata filters at search time**~~ **SHIPPED (A2)** | R2R `filters` on `metadata` JSONB; mem0 structured `filters` | ~~Add `filter_by=...` to `hybrid_search()`~~ Shipped: source-path prefix/glob, `indexed_after/before`, file-type filters on REST `/search`, MCP `search`, and `/explain`. Residual gap: E0e multi-collection filters (`search_many()`) not yet shipped. |
| 3 | ~~**Per-collection embedding model override**~~ **SHIPPED (C1)** | (None does this fully, but `CollectionMeta.embedding_model` field already existed in Archon) | ~~Wire `embedding_model` from `CollectionMeta` into ingest and search~~ Shipped: `CollectionMeta.active_embedding_model` fully wired into ingest, search, and sync; mismatch raises `ModelValidationError`. |
| 4 | ~~**Incremental FTS rebuild**~~ **SHIPPED (C6)** | R2R stored `GENERATED ALWAYS AS` tsvector | ~~Switch from full FTS rebuild to incremental updates~~ Shipped: `optimize_fts()` replaces `rebuild_fts_index()` at all call sites. O(delta) vs O(collection). |
| 5 | ~~**RAG Fusion (sub-query decomposition)**~~ **SHIPPED (C5)** | R2R `rag_fusion` strategy; Kotaemon `FullDecomposeQAPipeline` | ~~Add multi-sub-query strategy~~ Shipped: `rag_fusion=true` decomposes query into N variants via Anthropic API, parallel search, second-pass RRF. Requires `archon-search[rag_fusion]` extra + Anthropic API key. Mutually exclusive with HyDE. |
| 6 | **Budget-aware context packet endpoint** | context-engine `ContextPacket`; MARM response limiter | Add a higher-level endpoint over `search_with_context` that accepts a token budget and returns selected chunks, compressed context, dropped-context reasons, per-slot token usage, and stage diagnostics. |
| 7 | **Layered memory artifacts with provenance** | TencentDB Agent Memory L0→L1→L2→L3; agentmemory consolidation | Add optional memory collections where raw evidence, extracted facts, summaries, and profiles are separate layers. Every synthesized layer must keep stable source IDs back to raw chunks/sessions. |
| 8 | **Memory lifecycle and salience metadata** | mem0 history/TTL; abmind sleep/darwinism; agentmemory decay/reinforcement | Track recall count, last recalled at, confidence, validity windows, TTL, supersession, merge/forget decisions, and audit history. Use these as explainable ranking features, not hidden magic. |
| 9 | **Agent-native capture adapters** | agentmemory hooks; TencentDB/OpenClaw capture; abmind MCP/CLI | Provide optional adapters for Codex/Claude/Cursor/MCP clients that ingest session starts, prompts, tool calls, decisions, summaries, and task outcomes into Archon-owned collections. |
| 10 | **Graph/provenance as a third retrieval stream** | agentmemory BM25 + vector + graph; R2R GraphRAG; docmost transclusions/backlinks | Keep vector + FTS + rerank as the core, then add optional entity/relationship traversal and relationship-aware context expansion with provenance shown in `/explain`. |
| 11 | **Pre-ingest safety gates for agent memory** | abmind injection scanner and secret handling | Before storing agent-generated memory, run prompt-injection pattern checks, secret redaction, classification assignment, and optional encryption for high-sensitivity entries. |
| 12 | **Human debug/admin surface** | MARM dashboard; agentmemory viewer; docmost source cards/history; R2R dashboard | After `/explain`, build a local UI for browsing collections, jobs, telemetry, ACL decisions, result provenance, memory lifecycle state, and context-packet assembly. |

### Medium-value gaps (consider)

| # | Gap | Best example | What to build |
|---|-----|-------------|---------------|
| 13 | ~~REST API alongside MCP~~ | — | **Already shipped**: `archon_search/server/app.py` is a FastAPI control plane with routes for health, state, status, search, route, collections, jobs, telemetry; `GET /openapi.json` is authoritative. Item removed. |
| 14 | **Streaming search results** | R2R SSE for `rag` and `agent` | Return first `top_k_return` results as they score rather than waiting for full cross-encoder pass. Reduces perceived latency on large reranker runs. |
| 15 | **Chunk-level access logging** | mem0 salience / Marveen access boost | Add `(chunk_id, accessed_at, query)` access log to LanceDB. Use access frequency to re-weight RRF scores. Turns search into a learning system that surfaces frequently-relevant chunks. |
| 16 | **Binary quantization** | R2R `INT1` bit vector column | Add `vec_binary bit(N)` column to LanceDB schema for two-stage retrieval: Hamming distance coarse pass (32x faster) → exact float re-rank. Meaningful for collections > 500k chunks. |
| 17 | **GraphRAG / knowledge graph** | Kotaemon (3 variants), R2R (Leiden), mem0 (Neo4j/Kuzu) | Add optional entity/relationship extraction pass at ingest (Haiku-based). Enables global queries about document corpora ("What are the main architectural patterns across all design docs?"). |

### Observational gaps (low priority)

| # | Gap | Notes |
|---|-----|-------|
| 18 | **Multi-modal PDF ingestion** | Kotaemon (Adobe/Azure DI + VLM), R2R (zerox) — both require external API keys or VLM endpoint. High cost per page; suitable only for high-value document collections. |
| 19 | **Horizontal scaling** | R2R achieves this via stateless FastAPI + Postgres. Archon's LanceDB embedded storage would need to be replaced or fronted by a proxy to enable this. Not needed for current single-user daemon model. |
| 20 | **Multi-tenancy** | R2R (schema-per-project), mem0 (4-level scoping). Archon supports namespaces + ACL filtering but shares a single LanceDB on disk; broader multi-tenancy would need storage-level isolation. |
| 21 | **Memory versioning / audit trail** | mem0's `history(memory_id)` with full mutation log is a strong operational feature. Archon could add a lightweight mutation log to `CollectionMeta` for collection-level changes (description updates, chunk size changes, ingest timestamps). |
