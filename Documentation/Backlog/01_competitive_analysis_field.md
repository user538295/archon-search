**Purpose**: Competitive analysis of `archon-search` versus major local and self-hosted search/RAG systems to identify strategic gaps.
**Audience**: Maintainers planning future Search architecture and roadmap work.
**Status**: Draft — partial factual corrections applied 2026-05-20; per-dimension scores not yet rescored
**Last reviewed**: 2026-05-20 / **Next review**: 2026-10-30

> **Caveat**: All competitor-specific claims (AnythingLLM, PrivateGPT, Kotaemon, mem0, R2R version strings, defaults, line counts, throughput numbers) are #Unverified against upstream sources at the time of this revision. Archon claims have been verified against `archon_search/` source.

# Search System Deep Comparison: Archon vs. the Field

> **Date:** 2026-04-29
> **Scope:** Archon search system benchmarked against five active systems spanning local RAG, AI memory layers, and production RAG frameworks.
> **Systems compared:** Archon · AnythingLLM (v1.12.1) · PrivateGPT (v0.6.2) · Kotaemon · mem0 · R2R (v3.6.6)

---

## Framing

The six systems occupy different parts of the design space:

| System | Primary use case | Language | Core RAG library |
|--------|-----------------|----------|-----------------|
| **Archon** | Standalone hybrid retrieval + routing server (REST + MCP) over local file collections | Python / asyncio | LanceDB + fastembed + chonkie + docling/markitdown/trafilatura (custom pipeline) |
| **AnythingLLM** | Desktop/self-hosted chat over documents; broad LLM provider matrix | Node.js | LangChain TextSplitter + LanceDB (or 9 alt vector DBs) |
| **PrivateGPT** | Offline-first, OpenAI-compatible RAG server | Python | LlamaIndex 0.11 |
| **Kotaemon** | Research-oriented document QA with multi-modal PDF and GraphRAG | Python | LlamaIndex + theflow (custom pipeline) |
| **mem0** | AI agent memory layer; stores extracted facts, not raw documents | Python / TypeScript | Custom + pluggable vector store |
| **R2R** | Production-grade self-hosted RAG API; multi-tenant, full-stack | Python / asyncio | Custom FastAPI + pgvector |

Fair comparison requires acknowledging mem0's structural difference: it stores LLM-extracted atomic facts, not document chunks. Its retrieval pipeline is evaluated on its own terms where relevant, and the apples-to-oranges gap is called out.

---

## Dimension 1 — Architecture & Design

### Summary table

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Primary language** | Python 3.12+ | Node.js ≥ 18 | Python ≥ 3.11, < 3.12 | Python 3.10+ | Python / TypeScript | Python 3.10–3.12 |
| **Async model** | asyncio throughout | Event loop (single-threaded JS) | Sync routes on async FastAPI | Thread-based parallelism | asyncio (`AsyncMemory`) | asyncio + asyncpg |
| **Architecture style** | Layered pipeline: parser → chunker → embedder → store → reranker → pipeline, with router/server/cli on the side | Monorepo (collector + server + frontend microservices); switch-case factories | DI container (injector); clean component/service split; LlamaIndex abstractions | Two-library monorepo (kotaemon headless lib + ktem Gradio app); theflow pipeline framework | Adapter pattern; 4 pluggable subsystems (llm/embedder/vector_store/history_store) | Layered FastAPI: providers → services → routers; Hatchet task queue for production |
| **Extensibility mechanism** | Protocol-typed backends (`EmbedderBackend`, `RerankerBackend`) | Switch-case factory (`getLLMProvider`), base class ABCs | DI-injected `@singleton` components | `BaseComponent` + `@Param.auto` declarative graph | `Memory.from_config()` config-driven adapter selection | Provider ABCs in `base/providers/`; concrete impls in `providers/` |
| **Separation of concerns** | Strong; each layer independently testable | Moderate; server mixes vector DB, agent framework, scheduler | Strong; service layer never touches DB directly | Good at library level; `ktem/index/file/pipelines.py` (600+ lines) violates SRP | Strong; swap vector store with one config change | Strong; service orchestrates providers, never touches DB directly |
| **Coupling to LlamaIndex** | None | None | High (locked to 0.11.x) | Medium (uses LlamaIndex abstractions, but behind theflow) | None | None |
| **Notable design weakness** | LanceDB embedded backend limits multi-process write concurrency; single-node only | No TypeScript; no dependency injection; console.log in production code | Python `< 3.12` hard upper bound; sync route handlers block uvicorn event loop | `theflow` is undocumented outside Kotaemon; `print()` in production code | No health check API; `user_id + agent_id` AND filter silently returns empty | `AgentFactory` has dead commented-out XML code; `FIXME` in agent base |

**Scores: Archon 8 · AnythingLLM 5 · PrivateGPT 7 · Kotaemon 6 · mem0 7 · R2R 8**

---

## Dimension 2 — Indexing / Ingestion Pipeline

### Format support matrix

| Format category | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------------|--------|-------------|------------|----------|------|-----|
| **Plain text / Markdown** | ✅ | ✅ | ✅ | ✅ | ✅ (short snippets only) | ✅ |
| **PDF (text)** | ✅ docling | ✅ pdf-parse + Tesseract fallback | ✅ pdfminer | ✅ pypdf / OCR / multimodal | ❌ (no doc ingest) | ✅ 38 formats |
| **PDF (OCR)** | ✅ docling | ✅ Tesseract | ❌ | ✅ OCRReader | ❌ | ✅ MistralOCR / Unstructured |
| **PDF (VLM / multimodal)** | ❌ | ❌ | ❌ | ✅ Adobe / Azure DI | ❌ | ✅ zerox (vision LLM) |
| **Word / DOCX** | ✅ markitdown | ✅ mammoth | ✅ DocxReader | ✅ UnstructuredReader | ❌ | ✅ |
| **Excel / XLSX** | ✅ | ✅ node-xlsx | ✅ PandasCSVReader | ✅ PandasExcelReader | ❌ | ✅ |
| **PowerPoint** | ✅ markitdown | ✅ officeparser | ✅ PptxReader | ✅ UnstructuredReader | ❌ | ✅ |
| **HTML** | ✅ trafilatura | ✅ | ✅ | ✅ HtmlReader / MhtmlReader | ❌ | ✅ |
| **Code files** | ✅ (plain-text fallback) | ❌ | ❌ | ❌ | ❌ | ✅ .py .js .ts .css |
| **Audio / Video** | ❌ | ✅ Whisper | ✅ VideoAudioReader | ❌ | ❌ | ✅ Whisper |
| **Images (OCR)** | ✅ docling OCR | ✅ Tesseract | ✅ ImageReader | ✅ UnstructuredReader | ❌ | ✅ |
| **Web URL crawl** | ❌ | ✅ Puppeteer depth-crawl | ❌ | ✅ WebReader | ❌ | ❌ |
| **GitHub / GitLab** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **YouTube transcripts** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **ZIP container** | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| **Total parsed formats** | ~15 | ~37 | ~14 | ~15 | 0 (fact extraction only) | 38 |

### Ingestion pipeline quality

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Chunking strategy** | Chonkie `RecursiveChunker`, 512 tokens (GPT-2 tokenizer) | LangChain `RecursiveCharacterTextSplitter`, 1,000 chars (not tokens) | LlamaIndex `SentenceWindowNodeParser` (fixed defaults, no config) | `TokenSplitter` (tiktoken GPT-3.5), default 1,024 tokens / 256 overlap — configurable via env vars | None — LLM extracts atomic facts | `RecursiveCharacterTextSplitter` (1,024 chars / 512 overlap) or `CharacterTextSplitter`; configurable |
| **Chunk size configurable** | ✅ `chunk_size` in `config.toml` | ✅ `text_splitter_chunk_size` in `system_settings` DB | ❌ hardcoded to LlamaIndex defaults | ✅ env vars `FILE_INDEX_PIPELINE_SPLITTER_CHUNK_SIZE` | N/A | ✅ `chunk_size` in `r2r.toml` |
| **Deduplication** | ✅ SHA-256 content hash per file; idempotent re-ingest (delete-then-insert by `doc_id`) | ✅ File-path-based `uuidv5`; vector cache skips re-embedding unchanged files (content-blind) | ❌ None — re-ingest creates duplicates; manual delete required | ✅ SHA-256 of file content; URL dedup via SHA-256 of URL | ✅ MD5 hash dedup on extracted facts | ⚠️ ID-based upsert only; no content-hash dedup at chunk level |
| **Crash recovery** | ✅ `IN_PROGRESS → PENDING` on restart; per-collection state machine | ❌ None | ❌ None | ❌ None | ❌ None | ✅ Hatchet task queue with configurable retry policies |
| **Auto-description generation** | ✅ Haiku samples 20 random chunks; re-runs on > 20% doc change | ❌ | ❌ | ⚠️ `TitleExtractor` + `SummaryExtractor` available as optional doc parsers | ❌ | ✅ LLM document summary from first N chunks at ingest |
| **Filesystem watch** | ✅ watchdog-based per-collection watcher; 5s debounce; `watch = true` in config | ✅ Bree job every 1h for watched documents | ✅ `IngestWatcher` via watchdog; file create/modify only (not delete) | ❌ Manual upload only | ❌ | ❌ |
| **Auto-reindex on config change** | ✅ detects `chunk_size` change and triggers re-ingest | ❌ | ❌ | ❌ | N/A | ❌ |
| **Parallel ingestion** | ✅ `asyncio.to_thread()` for CPU-bound embedding | ❌ Sequential (or bounded concurrent chunks) | ✅ `simple`/`batch`/`parallel`/`pipeline` modes; multiprocessing pool | ❌ Synchronous | ✅ `asyncio.gather()` for concurrent `add()` | ✅ `ingestion_concurrency_limit=16` (Hatchet); batch size 128 |
| **Chunk-level enrichment** | ❌ | ❌ | ❌ | ✅ `TitleExtractor`, `SummaryExtractor` | ❌ | ✅ Optional LLM-based `chunk_enrichment()` via configurable prompt |

**Scores: Archon 9 · AnythingLLM 7 · PrivateGPT 5 · Kotaemon 7 · mem0 2 · R2R 8**

---

## Dimension 3 — Search Quality

### Retrieval method comparison

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Vector (semantic) search** | ✅ LanceDB ANN cosine | ✅ cosine ANN | ✅ cosine ANN | ✅ cosine ANN | ✅ HNSW cosine (Qdrant default) | ✅ pgvector cosine / L2 / inner product / Hamming / Jaccard — configurable |
| **Full-text search (FTS/BM25)** | ✅ LanceDB native FTS (tantivy-backed) | ❌ None | ❌ None | ✅ LanceDB FTS or Elasticsearch (k1=2.0, b=0.75 tunable) | ✅ BM25 per-provider keyword search across 15 vector store adapters | ✅ PostgreSQL `tsvector` FTS with `websearch_to_tsquery` |
| **Hybrid search** | ✅ Vector + FTS with RRF fusion (k=60) | ❌ | ❌ | ⚠️ Parallel vector + FTS but naive fusion (FTS hits hard-coded to score -1.0; no normalization) | ✅ Semantic + BM25 + entity graph (v3); score fusion weights not user-configurable | ✅ Semantic + FTS with configurable RRF (`semantic_weight`, `full_text_weight`, `rrf_k`) |
| **RRF / score fusion quality** | ✅ Proper RRF (k=60), scores from both paths | N/A | N/A | ❌ FTS hard-coded -1.0; reranker sees mixed scales | ❌ Fusion weights black-box | ✅ Proper weighted RRF; configurable weights |
| **Cross-encoder reranking** | ✅ `cross-encoder/ms-marco-MiniLM-L-6-v2` — full cross-encoder (query-document pair scoring) via fastembed `TextCrossEncoder` | ✅ `Xenova/ms-marco-MiniLM-L-6-v2` (CPU-only ONNX; ~5.2s/20 docs on i7) | ✅ `cross-encoder/ms-marco-MiniLM-L-2-v2` via SentenceTransformer; opt-in | ✅ Multiple: Cohere `rerank-v4.0-fast`, VoyageAI, LLM-as-judge, TEI endpoint; layered | ✅ Optional: Cohere, SentenceTransformer, HuggingFace, LLM reranker; off by default | ⚠️ Only via external HuggingFace TEI endpoint (`mxbai-rerank-large-v1`); no Cohere/Jina |
| **Reranker on by default** | ✅ | ❌ opt-in per workspace | ❌ opt-in | ❌ opt-in | ❌ opt-in | ❌ requires TEI server config |
| **Context-window enrichment** | ✅ `search_with_context()` fetches adjacent chunk_ids | ❌ source window backfill from conversation history (heuristic, not semantic) | ✅ `SentenceWindowNodeParser` + `MetadataReplacementPostProcessor`; sentence window stored as metadata at ingest | ❌ | ❌ | ❌ |
| **Query expansion / HyDE** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ HyDE (LLM generates N hypothetical answers, embeds each, merges with RRF) |
| **RAG fusion (sub-query)** | ❌ | ❌ | ❌ | ✅ `FullDecomposeQAPipeline` (sub-question decomposition) | ❌ | ✅ `rag_fusion` strategy (LLM rephrases into N sub-queries, parallel search, RRF) |
| **Multi-collection routing** | ✅ Centroid pre-ranking → confidence gating (cosine ≥ 0.30) → LLM decomposer routing; 3-tier strategy | ❌ Single workspace namespace | ❌ filter by doc IDs only | ❌ Single index | ✅ Scoped by `user_id`/`agent_id`/`app_id`/`run_id` | ✅ Collections with LLM-generated summaries; search filtered by `collection_ids` |
| **Metadata filtering** | ⚠️ No user-facing metadata filter API; internal `where` clauses and ACL/namespace filtering exist | ❌ | ✅ filter by `doc_id` list (`ContextFilter`) | ✅ by `file_id` in LanceDB | ✅ structured `filters` dict with AND/OR operators over scoping fields | ✅ full Postgres-style predicate filters on `metadata` JSONB |
| **Graph / knowledge graph search** | ❌ | ❌ | ❌ | ✅ MS GraphRAG, nano-graphrag, LightRAG (entity/relationship queries) | ✅ entity graph search (Neo4j / Memgraph / Kuzu); returns `relations` field | ✅ GraphRAG with entity + relationship + community (Leiden clustering); entity dedup |
| **default `top_k` retrieved** | 15 (configurable `top_k_retrieve`; `top_k_return=5` returned after reranking) | 4 per workspace | 2 (critically low default) | `top_k * 10` candidates pre-reranking | configurable; `threshold=0.1` in v3 | configurable; `limit` in `SearchSettings` |

**Scores: Archon 9 · AnythingLLM 4 · PrivateGPT 5 · Kotaemon 8 · mem0 6 · R2R 9**

---

## Dimension 4 — Embedding Model Choices

### Model and provider matrix

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Default model** | `BAAI/bge-small-en-v1.5` (384-dim, 33 MB ONNX) | `Xenova/all-MiniLM-L6-v2` (384-dim, 23 MB ONNX) | `nomic-ai/nomic-embed-text-v1.5` (768-dim, HuggingFace) | OpenAI `text-embedding-3-large` if API key set; `BAAI/bge-base-en-v1.5` (fastembed) otherwise | `openai/text-embedding-3-small` (1536-dim) — README calls this suboptimal for hybrid | `openai/text-embedding-3-small` (512-dim via LiteLLM) |
| **Default reranker model** | `cross-encoder/ms-marco-MiniLM-L-6-v2` (cross-encoder, ONNX via fastembed) | `Xenova/ms-marco-MiniLM-L-6-v2` (ONNX, CPU-only) | `cross-encoder/ms-marco-MiniLM-L-2-v2` | `cohere/rerank-v4.0-fast` (default when Cohere key present) | `cross-encoder/ms-marco-MiniLM-L-6-v2` (SentenceTransformer option) | `mixedbread-ai/mxbai-rerank-large-v1` (via TEI) |
| **Local CPU embedding** | ✅ fastembed (ONNX Runtime) | ✅ `@xenova/transformers` (ONNX, CPU-only Node.js) | ✅ HuggingFace `sentence-transformers` | ✅ `FastEmbedEmbeddings` (fastembed) | ⚠️ via Ollama only | ⚠️ via Ollama only |
| **GPU acceleration** | ✅ Auto-detected CUDA / CoreML via fastembed | ❌ No GPU in `@xenova/transformers` for Node.js | ⚠️ Implicit via HuggingFace device detection; not explicitly configurable | ✅ fastembed + TEI endpoint | ❌ (delegated to external Ollama) | ❌ (delegated to external Ollama) |
| **Model configurable without code change** | ✅ `embedding_model` in `config.toml` | ✅ env var `EMBEDDING_ENGINE` + model setting | ✅ `embedding.mode` + `model_name` in `settings.yaml` | ✅ `flowsettings.py` env vars | ✅ `config_dict` or `MemoryConfig` | ✅ `base_model` in `r2r.toml` |
| **OpenAI** | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ (LiteLLM) |
| **Azure OpenAI** | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ (LiteLLM) |
| **Ollama** | ❌ | ✅ | ✅ | ✅ (via OpenAI compat) | ✅ | ✅ |
| **Cohere** | ❌ | ✅ | ❌ | ✅ (reranker: `rerank-v4.0-fast`) | ✅ | ❌ |
| **VoyageAI** | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ |
| **HuggingFace TEI endpoint** | ❌ | ❌ | ❌ | ✅ `TeiEndpointEmbeddings` + `TeiFastReranking` | ❌ | ✅ (reranking only) |
| **Multilingual model out of box** | ❌ | ✅ `multilingual-e5-small` (487 MB) | ⚠️ configurable | ✅ `LCCohereEmbeddings embed-multilingual-v3.0` | ❌ | ❌ |
| **Per-collection model override** | ❌ `embedding_model` stored in `CollectionMeta` but not wired into ingest | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Embedding model validation on startup** | ⚠️ `install.py validate_providers()` tests stack during `archon-search install`, not on server start | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Dimension mismatch guard** | ✅ | ❌ | ⚠️ Pydantic default 384 vs actual default 768 is a known footgun | ❌ | ❌ | ⚠️ Must manually match `base_dimension` to model output |

**Scores: Archon 8 · AnythingLLM 6 · PrivateGPT 6 · Kotaemon 8 · mem0 5 · R2R 7**

---

## Dimension 5 — Storage Backend

### Vector store comparison

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Default vector store** | LanceDB (embedded) | LanceDB (embedded) | Qdrant (local file or remote) | ChromaDB (local persistent) | Qdrant (local `/tmp/qdrant`) | PostgreSQL + pgvector (only option) |
| **Vector store options** | LanceDB only | LanceDB · Chroma · ChromaCloud · Pinecone · Qdrant · Weaviate · Milvus · Zilliz · Astra · pgvector (10 total) | Qdrant · Chroma · Postgres/pgvector · Milvus · ClickHouse (5 total) | ChromaDB · LanceDB · Milvus · Qdrant · in-memory (5 total) | Qdrant · Chroma · Pinecone · pgvector · Milvus · Weaviate · FAISS · Redis · Elasticsearch · OpenSearch · Azure AI · Vertex AI · Upstash · MongoDB · Baidu · Databricks · S3 Vectors · in-memory (19 total) | PostgreSQL + pgvector (1 — no alternatives) |
| **ANN algorithm** | LanceDB native IVF-PQ (sub-linear) | LanceDB auto-index (sub-linear); others delegate to service | Qdrant HNSW (default); PrivateGPT sets no index params | ChromaDB HNSW; LanceDB IVF-PQ; Milvus IVF/HNSW | HNSW via Qdrant (default); delegated to backend | pgvector HNSW or IVFFlat; concurrent index build; `CREATE INDEX CONCURRENTLY` |
| **FTS on same store** | ✅ LanceDB native FTS (tantivy) | ❌ No FTS in any provider | ❌ | ✅ LanceDB FTS (tantivy) or Elasticsearch | ✅ BM25 keyword search in 15 vector store adapters | ✅ PostgreSQL `tsvector` `GENERATED ALWAYS AS` stored column; GIN index |
| **Multi-process safe** | ❌ LanceDB embedded (no concurrent writers) | ❌ LanceDB embedded | ✅ remote Qdrant/Postgres support | ❌ Chroma `PersistentClient` not multi-process safe | ✅ remote Qdrant or Postgres options | ✅ PostgreSQL connection pool via asyncpg `SemaphoreConnectionPool` |
| **Schema / metadata** | Fixed Arrow schema per collection; `_archon_collection_meta` table with centroid, doc count, embedding model | All metadata fields flat-packed into LanceDB table at insert | Node metadata: `file_name`, `doc_id`, `page_label`; separate doc store for metadata | Per-system metadata varies; Kotaemon stores `file_id`, `user_id` | Memory metadata: `user_id`, `agent_id`, `app_id`, `run_id`, categories, expiry | Fixed Postgres schema: `chunks(id, document_id, owner_id, collection_ids, vec, vec_binary, text, metadata JSONB, fts)` |
| **Async client** | ✅ `lancedb.db.AsyncConnection` | ❌ Node.js event loop (sync LanceDB FFI) | ⚠️ asyncpg for Postgres only; other backends sync | ❌ Threading | ✅ `AsyncMemory` / `AsyncMemoryClient` | ✅ asyncpg pool throughout |
| **Binary quantization** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `INT1` bit vector column; 20x over-retrieval then float re-rank; 32x storage reduction |
| **Graph store** | ❌ | ❌ | ❌ | ⚠️ External (MS GraphRAG / LightRAG / nano-graphrag) | ✅ Neo4j · Memgraph · Kuzu · AWS Neptune | ✅ Postgres graph tables: entities, relationships, communities; Leiden clustering |
| **Human-inspectable storage** | ❌ LanceDB binary Apache Arrow | ❌ LanceDB binary | ✅ Qdrant local JSON-based; Chroma SQLite readable | ✅ SQLite for metadata; Chroma SQLite; LanceDB binary | ✅ SQLite history DB readable | ✅ PostgreSQL (standard tooling) |
| **Backup** | ❌ No export API | ❌ No built-in backup | ✅ Single-file copy for local Qdrant/Chroma | ⚠️ Copy `ktem_app_data/` directory | ✅ copy SQLite (OSS); platform handles (cloud) | ✅ Standard `pg_dump` |
| **Multi-tenancy at storage level** | ⚠️ Single LanceDB on disk; logical isolation via namespaces + ACL filtering (`acl.py`, `[namespaces]` TOML) | ❌ Workspaces share one SQLite | ❌ Single collection per deployment | ⚠️ `user_id`-scoped document tables | ✅ `user_id`/`agent_id`/`app_id`/`run_id` scoping on every query | ✅ PostgreSQL schema-per-project; `x-project-name` header routing |

**Scores: Archon 7 · AnythingLLM 6 · PrivateGPT 7 · Kotaemon 6 · mem0 9 · R2R 8**

---

## Dimension 6 — API / Integration Surface

### API capability matrix

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **REST API** | ✅ FastAPI control plane with routes for health, state, status, search, route, collections, jobs, telemetry; `GET /openapi.json` authoritative | ✅ ~60 OpenAPI-documented endpoints at `/api/v1/`; Swagger at `/api/docs` | ✅ OpenAI-compatible `/v1/` (chat, completions, embeddings, ingest, chunks, summarize, health) | ❌ Gradio built-in HTTP; no documented consumer REST API | ✅ `https://api.mem0.ai` platform; `POST /v3/memories/add/`, `search/`, history, entities | ✅ FastAPI `/v3/` with 10+ routers: chunks, documents, collections, graphs, retrieval, auth, system |
| **OpenAI-compatible API** | ❌ | ✅ Drop-in: `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, `/v1/vector_stores` | ✅ `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | ❌ | ❌ | ❌ |
| **MCP server (exposes tools)** | ✅ FastMCP endpoint sharing FastAPI auth: `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document` (9 tools) | ❌ Does not expose itself as MCP server | ❌ | ❌ | ✅ Hosted MCP at `https://mcp.mem0.ai/mcp`; self-hosted `mem0-mcp` package | ❌ |
| **MCP client (consumes tools)** | ❌ archon-search exposes MCP; does not consume external MCP servers | ✅ `MCPCompatibilityLayer`; any MCP server auto-discovered and exposed as agent plugin | ❌ | ✅ `MCPManager`; MCP servers configured via Settings UI and used in ReAct/ReWoo agents | ❌ | ❌ |
| **Python SDK** | ❌ (Claude Agent SDK for session integration) | ❌ | ❌ | ❌ | ✅ `Memory`, `AsyncMemory`, `MemoryClient` from `mem0ai` | ✅ `r2r` package; `R2RClient` with namespaced sub-clients |
| **TypeScript/JavaScript SDK** | ❌ | ✅ (it is JS) | ❌ | ❌ | ✅ `mem0ai` npm; `Memory` (OSS) + `MemoryClient` (platform) | ✅ `r2r-js` npm |
| **Web UI** | ❌ | ✅ React SPA (Vite) — full-featured chat, workspace, settings | ✅ Gradio 4.x — RAG / Search / Basic / Summarize modes | ✅ Gradio 4.x — chat, index management, settings, citation panel | ❌ | ✅ Next.js dashboard (`r2r-dashboard` Docker image) |
| **CLI** | ✅ `archon-search` entry point; 9 subcommands: `start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config` | ❌ Scripts only; no installed CLI | ❌ Scripts only | ⚠️ `kotaemon` CLI via trogon TUI; limited | ✅ Node + Python CLI: `add`, `search`, `get`, `delete` | ✅ `r2r` CLI: `serve`, `db upgrade`, `generate-private-key` |
| **Streaming responses** | ❌ | ✅ SSE streaming chat | ✅ SSE streaming chat | ❌ | ❌ | ✅ SSE for `rag` and `agent` endpoints; `CitationTracker` for span-level source attribution |
| **Authentication** | ✅ Bearer token middleware on every route except `GET /health`; key auto-generated at `~/.archon-search/.search.env` (mode 600); `ARCHON_SEARCH_API_KEY` / `ARCHON_SEARCH_KEY_FILE` overrides | ✅ JWT (single-user or multi-user RBAC); API keys; per-user daily message limits | ✅ HTTP Basic Auth; single static secret; no JWT/OAuth | ✅ Username/password SQLite; SSO (Google OAuth / Keycloak OIDC) | ✅ `Authorization: Token <key>`; webhook events | ✅ JWT (self-hosted) / Supabase / Clerk; email verification; API key management; rate limiting per user per route |
| **Webhooks** | ❌ | ❌ | ❌ | ❌ | ✅ `memory_add/update/delete/categorize` events | ❌ |
| **Embedded widget** | ❌ | ✅ UUID-based embed endpoint; no user account required; configurable per-embed | ❌ | ❌ | ❌ | ❌ |
| **Telegram** | ❌ Not a Telegram bot; no Telegram integration | ✅ optional `node-telegram-bot-api` | ❌ | ❌ | ❌ | ❌ |
| **Health endpoint** | ✅ `GET /health` on FastAPI app (also exposed via MCP `health_check`) | ✅ `GET /api/ping → { online: true }` (no deep checks) | ✅ `GET /health → { status: "ok" }` (no component checks) | ❌ None | ❌ None | ✅ `GET /v3/health`; Docker healthcheck interval 6s; `/v3/system/status` for authorized users |

**Scores: Archon 7 · AnythingLLM 8 · PrivateGPT 6 · Kotaemon 5 · mem0 7 · R2R 8**

---

## Dimension 7 — Operational Concerns

### Operations comparison

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Install method** | `uv sync`; `archon-search install` command with GPU detection (auto-swap to `fastembed-gpu`) and provider validation | Docker single container; manual: `yarn setup` + 3 processes; Desktop Electron app available | Poetry extras: `poetry install --extras "ui vector-stores-qdrant"`; Docker Compose | Docker (`lite`/`full`/`ollama` variants); conda or `uv` | `pip install mem0ai`; local Qdrant via Docker; fully local with Ollama + Kuzu | `pip install r2r` (light); `docker compose` (full); Kubernetes manifests in `deployment/k8s/` |
| **Config format** | `~/.archon-search/archon-search.toml` (20+ validated fields); `.search.env` for the API key; annotated `archon-search.toml.example` | `.env` file only; runtime settings in SQLite `system_settings` table (admin panel) | YAML profiles with deep merge; env var substitution; `PGPT_PROFILES` composable | `flowsettings.py` (Python) + `.env`; runtime settings in SQLite via Gradio Settings tab | Python dict or `MemoryConfig` dataclass; env vars for API keys only | TOML `r2r.toml`; env var overrides; named presets: `full`, `full_ollama`, `gemini`, `lm_studio`, etc. |
| **Crash recovery** | ✅ Per-collection `IN_PROGRESS → PENDING` state machine; tested | ⚠️ Bree job restarts; no ingestion state machine | ❌ None beyond Docker restart policy | ❌ None | ❌ None | ✅ Hatchet task queue persistence; Docker `restart: on-failure`; Sentry error tracking |
| **Health / diagnostics** | ✅ `GET /health` HTTP; `archon-search status` CLI; internal `_diagnostics.py` module (not exposed as a `doctor` subcommand) #Unverified | ⚠️ `GET /api/ping` (no deep checks); admin panel stats | ⚠️ `GET /health → ok` (no component liveness checks) | ❌ No health endpoint or doctor command | ❌ No health check API | ✅ `GET /v3/health`; `/v3/system/status`; Sentry; Fluent-Bit log shipping config |
| **Monitoring** | ⚠️ Opt-in telemetry (JSONL under `~/.archon-search/search-logs/`); no notification system | ❌ No metrics; PostHog telemetry opt-out | ❌ LlamaIndex event callbacks to stdout; no metrics endpoint | ❌ No structured monitoring | ❌ Python `logging` only; no metrics | ✅ Sentry SDK; Fluent-Bit; `request_log` table for rate-limit audit; no Prometheus endpoint |
| **Service management** | ✅ macOS launchd / Linux systemd via `PlatformService` ABC | ❌ No launchd/systemd; Docker `restart: unless-stopped` or external PM2/systemd | ❌ No service manager; Docker `restart: unless-stopped` | ❌ Foreground process only | N/A (library) | ✅ Docker Compose restart policies; Kubernetes StatefulSet; Gunicorn multi-worker |
| **Scheduled maintenance** | ⚠️ `archon_search/jobs/` is an async job store (model + store) for long-running ingest/reindex, not a user-task scheduler | ✅ Bree: cleanup every 12h/8h; doc sync every 1h | ❌ | ❌ | ❌ | ✅ APScheduler `VACUUM` / `VACUUM ANALYZE` daily at 3am; `vacuum_schedule` configurable |
| **Auto-update** | ❌ No `update` subcommand; releases via `release.sh` + PyPI | ❌ | ❌ | ❌ | ❌ | ✅ `r2r db upgrade` (Alembic migrations) |
| **Windows support** | ⚠️ Platform stubs exist; service management not yet implemented | ✅ Docker / Desktop Electron app | ⚠️ Docker only | ✅ Docker | ✅ pip install | ✅ Docker |
| **Multi-tenancy** | ⚠️ Namespaces + ACLs (`acl.py`, `[namespaces]` TOML); single LanceDB on disk | ✅ Full RBAC: default/manager/admin roles; invite codes; user suspension; daily message limits | ❌ Single static auth secret | ✅ `user_id`-scoped documents; SSO; multi-user UI management | ✅ `user_id`/`agent_id`/`app_id`/`run_id` — 4-level scoping | ✅ PostgreSQL schema-per-project; per-user storage limits; rate limiting per user/route |

**Scores: Archon 8 · AnythingLLM 6 · PrivateGPT 5 · Kotaemon 4 · mem0 4 · R2R 8**

---

## Dimension 8 — Test Coverage & Code Quality

### Testing and quality comparison

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Search-specific test lines** | ~34,000 lines across ~84 test files #Unverified | ~0 (Jest declared, no test files found; no CI test workflow) | Limited; route-level integration tests with mock backends | Minimal (sparse for a 25k-star project) | `pytest` + `ruff`; CLI integration tests; no coverage threshold published | Integration-heavy (JS SDK tests against live server); limited Python unit tests |
| **Test approach** | TDD mandatory; happy paths first; unit + integration; `Protocol`-typed backends enable clean mock injection | None discoverable | Mock-injected DI; `MockLLM` + `MockEmbedding(384)` via DI override | `pytest` + `pytest-mock` declared; near-zero visible coverage | `pytest`; ruff lint/format; no mypy on main library | `pytest-asyncio`; Alembic migration tests; JS integration suite; no unit test matrix |
| **Notable test modules (Archon)** | `test_sync.py` ~4,612 lines · `test_install.py` ~1,858 · `test_pipeline.py` ~2,129 · `test_store.py` ~2,415 · server tests under `tests/server/` #Unverified | — | — | — | — | — |
| **Coverage tooling** | `pytest-cov`; per-module coverage visible; project mandate ≥ 85% | None | `pytest-cov` configured; `branch = true`; no threshold enforcement | `coverage` declared; no threshold | No published threshold | No unit coverage threshold; integration suite only |
| **Static analysis** | `ruff` configured; type hints throughout; `Protocol`-typed extensibility (mypy full coverage #Unverified) | ESLint 9 + Prettier; Flow type annotations (optional); no TypeScript | `mypy ^1.11` strict; `ruff`; `black`; `pre-commit`; Google docstring convention; `ban-relative-imports = "all"` | `black` + `flake8`; no mypy visible | `ruff check` + `ruff format`; no mypy on main library | `mypy ≥ 1.5.1`; `pre-commit`; Pydantic v2 throughout |
| **Key code quality issues** | Single-node LanceDB embedded; no horizontal scaling; FTS rebuild lifecycle on `ingest_directory` #Unverified | No TypeScript; `console.log` mixed with Winston; charSize vs. token inconsistency; multiple TODO comments for missing features | `similarity_top_k=2` default is dangerously low; collection name hardcoded as `"make_this_parameterizable_per_api_call"` TODO | `print()` in retrieval path; FTS fusion score=-1.0 hardcoded; `chromadb<=0.5.16` pinned; `asyncio.run()` from sync in LightRAG | Default embedding model acknowledged as suboptimal in README; `user_id + agent_id` AND filter documented footgun | Dead code in `AgentFactory`; `FIXME` in agent base; `VACUUM FULL` not yet implemented; FTS English-only hardcoded |

**Scores: Archon 9 · AnythingLLM 1 · PrivateGPT 6 · Kotaemon 3 · mem0 5 · R2R 5**

---

## Dimension 9 — Performance & Scalability

### Performance comparison

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Vector search complexity** | LanceDB scan/ANN — explicit `create_index` not confirmed in `store.py` #Unverified | Sub-linear — LanceDB auto-index (default); varies by provider | Delegated to backend; Qdrant HNSW O(log n); Chroma brute-force for small collections | ChromaDB brute-force for small; HNSW at scale; LanceDB IVF-PQ; Milvus IVF/HNSW | O(log n) HNSW via Qdrant (default); delegated to backend | pgvector HNSW O(log n) or IVFFlat O(√n); binary quantization 32x storage reduction |
| **Reranker throughput** | fastembed `TextCrossEncoder` (ONNX) with optional CUDA/CoreML providers; batch inference | ~5.2s/20 docs on i7 (ONNX CPU) — prohibitively slow for interactive production use | fast (MiniLM-L-2); practical | Cohere API (fast); LLM reranker via ThreadPoolExecutor (variable, API-bound) | Fast if Cohere; slower if SentenceTransformer or LLM reranker | External TEI service; network hop per rerank request |
| **Async implementation** | ✅ asyncio throughout; `asyncio.to_thread()` for CPU-bound embedding | ❌ Single-threaded Node.js; no worker_threads for CPU-intensive tasks | ❌ Sync route handlers block uvicorn event loop | ❌ Threading-based parallelism; `asyncio.run()` from sync in LightRAG | ✅ `AsyncMemory`; `asyncio.gather()` for parallel `add()` | ✅ asyncio + asyncpg throughout; `asyncio.create_task()` for parallel sub-queries |
| **Ingestion parallelism** | `asyncio.to_thread()` for CPU-bound ops; per-file sequential then batch embed | Sequential by default; no worker pool | 4 modes: simple/batch/parallel/pipeline; `multiprocessing.Pool(count_workers=2)` | Synchronous; no worker pool | `asyncio.gather()` for concurrent `add()`; no native batch `add()` in OSS | Hatchet `ingestion_concurrency_limit=16`; batch size 128 for embedding |
| **Horizontal scaling** | ❌ Single-node; LanceDB embedded | ❌ Single Node.js process; SQLite bottleneck; LanceDB embedded | ❌ Single-process; `@singleton` DI scope | ❌ Single Gradio process; Chroma not multi-process safe | ✅ Remote Qdrant or Postgres backend enables scaling | ✅ Stateless R2R process; all state in Postgres; multiple replicas possible; Hatchet is distributed |
| **max_parallel_collections** | ✅ `max_parallel_collections=3` configurable | N/A | N/A | N/A | N/A | N/A |
| **Practical ceiling** | Millions of chunks (LanceDB ANN); limited by single-node embedded DB for concurrent writes | ~10k–100k chunks per workspace before LanceDB auto-index kicks in; single-writer SQLite | ~100k–1M chunks per backend type; depends on vector store choice | ~50k chunks with Chroma; higher with LanceDB/Milvus/Qdrant backends | ~1M+ memories with remote Qdrant/Postgres; local Qdrant `/tmp/` not production-grade | 100M+ with pgvector HNSW + Postgres cluster; binary quantization extends further |
| **FTS rebuild cost** | ⚠️ FTS rebuild lifecycle differs between `ingest_directory()` and `ingest_file()` #Unverified | N/A (no FTS) | N/A (no FTS) | ⚠️ LanceDB tantivy FTS rebuilt on schema changes; ES incremental | ⚠️ Retrieves top-10 existing memories before each `add()` (extra vector search per write) | ✅ Postgres FTS is a stored generated column; automatically maintained; no rebuild cost |
| **Embedding concurrency** | ✅ `asyncio.to_thread()` non-blocking | `maxConcurrentChunks` 5–25; sequential batching | `multiprocessing.Pool` for CPU; async not in HTTP layer | ❌ Synchronous | `asyncio.Semaphore(concurrent_request_limit)` | `asyncio.Semaphore(concurrent_request_limit=256)` |

**Scores: Archon 8 · AnythingLLM 4 · PrivateGPT 5 · Kotaemon 4 · mem0 7 · R2R 9**

---

## Dimension 10 — Unique Features & Innovations

### Differentiating capabilities

| Feature / Innovation | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|----------------------|--------|-------------|------------|----------|------|-----|
| **Multi-collection routing with centroid pre-ranking** | ✅ Cosine similarity against collection centroids → confidence gating → LLM decomposer routing; 3-tier strategy based on collection count; `routing_shortlist_size=8`, `routing_confidence_threshold=0.30` | ❌ | ❌ | ❌ | ❌ (scoped by user/agent ID) | ❌ (filter by collection_ids) |
| **LLM-based memory extraction** | ❌ | ❌ | ❌ | ❌ | ✅ Core differentiator — LLM extracts atomic structured facts from conversations; v3 ADD-only algorithm; conflict resolution; 93% temporal reasoning accuracy claimed | ❌ |
| **GraphRAG** | ❌ | ❌ | ❌ | ✅ 3 variants: MS GraphRAG, nano-graphrag, LightRAG; entity/relationship queries | ✅ Dual storage (vector + graph) in one `add()` call; entity extraction inline; supports Neo4j, Memgraph, Kuzu | ✅ Full pipeline: entity extraction → dedup → Leiden community detection → LLM community summaries; entity + relationship + community search |
| **HyDE (Hypothetical Document Embeddings)** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Configurable `hyde` strategy; N hypothetical LLM answers embedded and searched in parallel |
| **Multi-modal PDF (VLM)** | ❌ | ❌ | ❌ | ✅ `AdobeReader` / `AzureAIDocumentIntelligenceLoader`; page thumbnails stored as base64 PNG; retrieval feeds images to GPT-4o VLM; separate evidence modes for TEXT/TABLE/FIGURE | ❌ | ✅ `zerox` VLM-based PDF ingestion via vision LLM |
| **In-browser PDF citation panel** | ❌ | ❌ | ❌ | ✅ PDF.js embedded; retrieved chunks highlighted in source PDF with relevance score | ❌ | ❌ |
| **Auto-description generation** | ✅ Haiku samples 20 random chunks → collection description; re-triggers on 20%+ doc change | ❌ | ❌ | ❌ | ❌ | ✅ LLM document summary at ingest from first N chunks |
| **Filesystem crash recovery state machine** | ✅ Per-collection `PENDING → IN_PROGRESS → DONE/FAILED`; stale `IN_PROGRESS` reset on restart | ❌ | ❌ | ❌ | ❌ | ✅ Hatchet workflow steps with retry policies |
| **Archon doctor diagnostics** | ❌ No `doctor` subcommand; `archon-search status` + `GET /health` + internal `_diagnostics.py` | ❌ | ❌ | ❌ | ❌ | ✅ `/v3/system/status` + Docker healthcheck |
| **Indexing completion notification** | ❌ Not implemented | ❌ | ❌ | ❌ | ✅ Webhooks: `memory_add/update/delete/categorize` | ❌ |
| **Pinned collections** | ✅ Always searched regardless of router decision | ✅ Pinned documents injected verbatim as full-text context (bypass RAG) | ❌ | ❌ | ✅ `immutable=True` flag on memories | ❌ |
| **Binary quantization** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `INT1` bit vectors; 32x storage reduction; two-stage re-rank |
| **Memory versioning / audit** | ❌ | ❌ | ❌ | ❌ | ✅ Full audit trail in SQLite; `memory.history(id)` returns every mutation with prev/new value | ❌ |
| **Memory expiration / TTL** | ❌ | ❌ | ❌ | ❌ | ✅ `expiration_date` ISO 8601; `immutable=True` | ❌ |
| **Agentic research pipeline** | ❌ (Claude handles this) | ✅ AIbitat multi-agent; 10+ built-in tools; AgentFlows; MCP server consumption | ❌ | ✅ ReAct + ReWoo agents; web search (Tavily/Jina); sub-question decomposition | ❌ | ✅ RAG Agent + Research Agent (o3-mini + claude-3-7-sonnet); Tavily + Firecrawl web tools |
| **OpenAI API drop-in compatibility** | ❌ | ✅ Full drop-in: `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, `/v1/vector_stores` | ✅ `/v1/` endpoints follow OpenAI schema | ❌ | ❌ | ❌ |
| **Citation tracking in streaming** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `CitationTracker` + `CitationSpans` + `extract_citations()` — span-level source attribution in SSE stream |

**Scores: Archon 8 · AnythingLLM 7 · PrivateGPT 5 · Kotaemon 9 · mem0 9 · R2R 9**

---

## Dimension 11 — Memory / Agent Integration

This dimension is not present in most RAG systems but is core to how these systems interact with AI agents and LLM sessions.

| Attribute | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|--------|-------------|------------|----------|------|-----|
| **Native LLM session integration** | ✅ MCP tools exposed to MCP-capable clients (e.g., Claude Code) via FastMCP endpoint | ✅ AIbitat agent framework; `@agent` invocation; automatic mode with native tool calling | ❌ External clients use the REST API | ✅ ReAct/ReWoo reasoning pipelines; MCP tool consumption | ✅ `@mem0/vercel-ai-provider` wraps any AI model transparently; `user_id` as model option | ✅ RAG Agent multi-turn with memory; Research Agent with o3-mini planning |
| **Conversation history as retrieval signal** | ❌ No conversation-history feature in archon-search | ⚠️ `fillSourceWindow` backfills sources from past 20 messages heuristically | ❌ | ✅ `AddQueryContextPipeline` uses last N turns to reformulate query | ✅ Full history store (SQLite); v3 retrieves top-10 related memories before each `add()` | ✅ Conversation sessions stored; multi-turn RAG agent |
| **Reminder / drift prevention** | ❌ Not implemented | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Custom extraction / categorization** | ❌ | ❌ | ❌ | ❌ | ✅ `custom_instructions` at project level shapes extraction LLM; custom categories configurable | ❌ |
| **Memory scope** | Collection-level (all sessions share collections) | Workspace-level (per-workspace namespace) | Document-level (filter by doc IDs) | Index-level (per-user documents) | `user_id`/`agent_id`/`app_id`/`run_id` — 4-level scoping with AND/OR filters | Collection-level with user/org ownership |

**Scores: Archon 8 · AnythingLLM 6 · PrivateGPT 3 · Kotaemon 6 · mem0 10 · R2R 7**

---

## Overall Scorecard

| Dimension | Archon | AnythingLLM | PrivateGPT | Kotaemon | mem0 | R2R |
|-----------|:------:|:-----------:|:----------:|:--------:|:----:|:---:|
| Architecture & Design | **8** | 5 | 7 | 6 | 7 | **8** |
| Indexing / Ingestion Pipeline | **9** | 7 | 5 | 7 | 2 | 8 |
| Search Quality | **9** | 4 | 5 | 8 | 6 | **9** |
| Embedding Model Choices | 8 | 6 | 6 | **8** | 5 | 7 |
| Storage Backend | 7 | 6 | 7 | 6 | **9** | 8 |
| API / Integration Surface | 7 | **8** | 6 | 5 | 7 | **8** |
| Operational Concerns | **8** | 6 | 5 | 4 | 4 | **8** |
| Test Coverage & Code Quality | **9** | 1 | 6 | 3 | 5 | 5 |
| Performance & Scalability | 8 | 4 | 5 | 4 | 7 | **9** |
| Unique Features & Innovations | 8 | 7 | 5 | **9** | **9** | **9** |
| Memory / Agent Integration | **8** | 6 | 3 | 6 | **10** | 7 |
| **Total (/ 110)** | **89** | **60** | **60** | **66** | **71** | **86** |

---

## Verdict

> **Note**: Per-dimension scores in this document have not been rescored after the factual corrections (default reranker, `top_k_retrieve`, REST API existence, Bearer auth, removal of non-existent subsystems). Treat totals and ranking as #Unverified.

**Archon (89) and R2R (86) are the two production-quality systems.** The gap to the others is real and reflects structural choices, not feature counts:

- **AnythingLLM (60)** is the most user-friendly entry point — 40+ LLM providers, a polished UI, and an OpenAI-compatible API — but its search subsystem has no FTS/hybrid retrieval, effectively no tests, and single-threaded Node.js limits serious throughput. It is a great tool for individual productivity, not a serious RAG engine.

- **PrivateGPT (60)** scores identically to AnythingLLM from opposite directions: it has good architecture and code quality, but its retrieval quality is self-undermined by a `similarity_top_k=2` default and no hybrid search. The Python `<3.12` constraint is a growing liability.

- **Kotaemon (66)** has the richest feature palette for knowledge work — multi-modal PDF, GraphRAG variants, citation panel, layered rerankers — but its hybrid fusion is naive (FTS score hard-coded to -1.0), its test suite is sparse, and it cannot scale beyond a single Gradio process.

- **mem0 (71)** is not a document RAG system and should not be compared to the others on ingestion. It wins decisively on memory correctness (LLM extraction, conflict resolution, versioning, graph dual-storage) and multi-tenancy scoping. Its score reflects dominance in its domain.

- **R2R (86)** is Archon's most credible competitor: full async stack, proper hybrid search with configurable RRF, HyDE, RAG Fusion, GraphRAG with Leiden communities, multi-tenancy via Postgres schemas, Hatchet distributed task queue, and Kubernetes manifests. Its gaps vs. Archon are: no MCP server, English-only FTS, no embedded GPU inference, and Postgres-only storage locks out LanceDB's performance characteristics.

---

## Opportunities for Archon

The gap analysis identifies specific capabilities present in competitors that Archon currently lacks.

### High-value gaps (implement soon)

| # | Gap | Best example | What to build |
|---|-----|-------------|---------------|
| 1 | **HyDE / query expansion** | R2R `hyde` strategy | Add optional `query_expansion=True` flag to `Pipeline.search()`; LLM generates N hypothetical answers → embed each → merge with RRF. Improves recall on under-specified short queries. |
| 2 | **Metadata filters at search time** | R2R `filters` on `metadata` JSONB; mem0 structured `filters` | Add `filter_by={"source_path": "*.py", "indexed_after": "2026-01-01"}` to `hybrid_search()`. LanceDB supports predicate pushdown natively — this is a small addition. |
| 3 | **Per-collection embedding model override** | (None does this fully, but `CollectionMeta.embedding_model` field already exists in Archon) | Wire `embedding_model` from `CollectionMeta` into ingest and search validation paths. Enables code collections to use a code-specialized model independently of prose collections. |
| 4 | **Incremental FTS rebuild** | R2R stored `GENERATED ALWAYS AS` tsvector | Switch from full FTS rebuild on `ingest_directory()` to incremental updates. LanceDB `create_index(replace=False)` is additive. FTS full rebuild is O(n) over the whole collection. |
| 5 | **RAG Fusion (sub-query decomposition)** | R2R `rag_fusion` strategy; Kotaemon `FullDecomposeQAPipeline` | Add multi-sub-query strategy: LLM rephrases query into N variants → parallel search → RRF. Improves recall for complex multi-faceted queries. |

### Medium-value gaps (consider)

| # | Gap | Best example | What to build |
|---|-----|-------------|---------------|
| 6 | ~~REST API alongside MCP~~ | — | **Already shipped**: `archon_search/server/app.py` is a FastAPI control plane with routes for health, state, status, search, route, collections, jobs, telemetry; `GET /openapi.json` is authoritative. Item removed. |
| 7 | **Streaming search results** | R2R SSE for `rag` and `agent` | Return first `top_k_return` results as they score rather than waiting for full cross-encoder pass. Reduces perceived latency on large reranker runs. |
| 8 | **Chunk-level access logging** | mem0 salience / Marveen access boost | Add `(chunk_id, accessed_at, query)` access log to LanceDB. Use access frequency to re-weight RRF scores. Turns search into a learning system that surfaces frequently-relevant chunks. |
| 9 | **Binary quantization** | R2R `INT1` bit vector column | Add `vec_binary bit(N)` column to LanceDB schema for two-stage retrieval: Hamming distance coarse pass (32x faster) → exact float re-rank. Meaningful for collections > 500k chunks. |
| 10 | **GraphRAG / knowledge graph** | Kotaemon (3 variants), R2R (Leiden), mem0 (Neo4j/Kuzu) | Add optional entity/relationship extraction pass at ingest (Haiku-based). Enables global queries about document corpora ("What are the main architectural patterns across all design docs?"). |

### Observational gaps (low priority)

| # | Gap | Notes |
|---|-----|-------|
| 11 | **Multi-modal PDF ingestion** | Kotaemon (Adobe/Azure DI + VLM), R2R (zerox) — both require external API keys or VLM endpoint. High cost per page; suitable only for high-value document collections. |
| 12 | **Horizontal scaling** | R2R achieves this via stateless FastAPI + Postgres. Archon's LanceDB embedded storage would need to be replaced or fronted by a proxy to enable this. Not needed for current single-user daemon model. |
| 13 | **Multi-tenancy** | R2R (schema-per-project), mem0 (4-level scoping). Archon supports namespaces + ACL filtering but shares a single LanceDB on disk; broader multi-tenancy would need storage-level isolation. |
| 14 | **Memory versioning / audit trail** | mem0's `history(memory_id)` with full mutation log is a strong operational feature. Archon could add a lightweight mutation log to `CollectionMeta` for collection-level changes (description updates, chunk size changes, ingest timestamps). |
