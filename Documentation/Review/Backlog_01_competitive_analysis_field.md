# Review: Backlog/01_competitive_analysis_field.md

## Summary

The document is structurally severely outdated. Many of its claims about `archon-search` describe a different product — apparently a previous-generation Telegram-bot variant of "Archon" — rather than the current standalone `archon-search` package. **Multiple core, prominently-displayed claims are wrong**, including the default reranker model, default `top_k_retrieve`, the existence of a REST API, the existence of authentication, the package's identity as a "Telegram bot", and a long list of named subsystems (HistoryManager, ContextProvider, ContextReminder, JobScheduler, IndexingNotificationMonitor, ArchonToolkit, `archon_toolkit_search.py`, `archon doctor`, `archon update`, `SearchInstaller` wizard) that do not exist in the codebase.

Competitor claims are not independently verified here per the rules; they are flagged "unverifiable" unless trivially confirmable. Internal-consistency / self-contradiction issues in the document are also noted.

Source-of-truth used:
- `archon_search/config.py` (defaults)
- `archon_search/constants.py`
- `archon_search/chunker.py`, `parser.py`, `embedder.py`, `reranker.py`, `store.py`, `router.py`, `pipeline.py`, `description_generator.py`, `watcher.py`, `sync.py`
- `archon_search/server/app.py`, `mcp.py`, `middleware_auth.py`, `key_manager.py`
- `archon_search/platform/windows.py`
- `archon_search/cli/main.py`
- `archon_search/collection_meta.py`
- `pyproject.toml`
- `tests/` (listing + line counts)

## Inaccuracies (numbered)

1. **Framing table, "Archon" row — "Claude Code companion daemon; full-document RAG over local file collections"** — Half-true at best. archon-search is a standalone hybrid retrieval server (REST + MCP). "Claude Code companion daemon" frames it as a session/companion tool; the codebase is a generic FastAPI + FastMCP search server (`archon_search/server/app.py`). It is not a daemon dedicated to Claude Code.

2. **Framing table — "LanceDB + fastembed (custom pipeline)"** — Accurate components, but missing `chonkie` (chunker) and `docling`/`markitdown`/`trafilatura` (parsers), which the doc itself contradictorily references later.

3. **Dimension 1, "Architecture style" — "Store → Pipeline → Router → Server → CLI/Toolkit"** — "Toolkit" is not part of `archon_search/`. There is no toolkit module. The CLAUDE.md doc-map and source tree describe `parser → chunker → embedder → store → reranker → pipeline` with `router`, `server`, `cli` on the side. The "Toolkit" reference is a leftover from a different product.

4. **Dimension 1, "Notable design weakness" — "`archon_toolkit_search.py` is 884 lines"** — **File does not exist** in `archon_search/`. Verified via directory listing. This is the most clearly load-bearing wrong fact in the architecture section.

5. **Dimension 2 ingestion table, Archon "Auto-description generation" — "Haiku samples 20 random chunks; re-runs on > 20% doc change"** — Sample-count is correct (`_MAX_SAMPLE_CHUNKS = 20` in `description_generator.py`). The ">20% doc change" threshold is correct (`description_generator.py:52`: "abs change >= 20% → regenerate"). This row is **accurate** — listed here only because the same claim is mis-attributed elsewhere; flagging as verified.

6. **Dimension 2, "Filesystem watch" — "5s debounce; `watch = true` in config"** — Verified: `watcher.py` `debounce_seconds: float = 5.0`; `config.py` exposes `watch: bool = False`. **Accurate.**

7. **Dimension 2, "Auto-reindex on config change — detects `chunk_size` change and triggers re-ingest"** — Verified: `config.py` has `auto_reindex_on_chunk_size_change: bool = True`. **Accurate.**

8. **Dimension 3 retrieval table, Archon "Cross-encoder reranking — `BAAI/bge-reranker-v2-m3`"** — **WRONG.** `config.py` line 35: `reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"`. The bge-reranker-v2-m3 model is not the default; not referenced anywhere in source. This is a major load-bearing inaccuracy: it inflates Archon's quality positioning vs. competitors.

9. **Dimension 3, Archon default `top_k` retrieved — "5 (configurable `top_k_retrieve`)"** — **WRONG.** `config.py` line 39: `top_k_retrieve: int = 15`. The "5" appears to confuse it with `top_k_return: int = 5` (line 40). This understates Archon's retrieval breadth.

10. **Dimension 3, Archon "Multi-collection routing — Centroid pre-ranking → confidence gating (cosine ≥ 0.30) → LLM decomposer routing; 3-tier strategy"** — Verified: `router.py` implements 3 tiers; `config.py` `routing_confidence_threshold: float = 0.30`; `routing_shortlist_size: int = 8`. **Accurate.**

11. **Dimension 3, Archon "Metadata filtering — ❌"** — Partially accurate. There is no user-facing metadata filter API. `store.py` does support `where` clauses internally for `chunk_id IN (...)` and namespace filtering for ACLs (`pipeline.py` `apply_acl_filter`). "No metadata filter" as shown is defensible at the API surface.

12. **Dimension 3, Archon "Context-window enrichment — `search_with_context()` fetches adjacent chunk_ids"** — Verified: `pipeline.py:306` `search_with_context` + `fetch_adjacent_chunks`. **Accurate.**

13. **Dimension 4 embedding table, Archon "Default reranker model — `BAAI/bge-reranker-v2-m3`"** — **WRONG, same as #8.** Default is `cross-encoder/ms-marco-MiniLM-L-6-v2`. Repeated inaccuracy.

14. **Dimension 4, Archon "Embedding model validation on startup — ✅ `install.py validate_providers()` tests stack before committing config"** — `install.py` exists and references provider validation, but exposed via the CLI `install`/`uninstall` commands, not "on startup" of the server. The claim is misleading.

15. **Dimension 4, Archon "Per-collection model override — ❌ `embedding_model` stored in `CollectionMeta` but not wired into ingest"** — Verified: `collection_meta.py` has `embedding_model: str = ""`. Router uses it to skip mismatched collections (`router.py:142`). Ingest does not consult per-collection override. **Accurate.**

16. **Dimension 5 storage table, Archon "Vector store options — LanceDB only"** — Verified: only `lancedb` is referenced. **Accurate.**

17. **Dimension 5, Archon "Async client — `lancedb.db.AsyncConnection` ✅"** — Plausible; `store.py` uses async LanceDB. Verified by inspection of async usage patterns. **Accurate.**

18. **Dimension 5, Archon "Multi-tenancy at storage level — ❌ All collections share one LanceDB"** — Partially misleading. archon-search supports **namespaces** with ACL filtering (`acl.py`, `apply_acl_filter`, `_validate_namespace` in `constants.py`, `[namespaces]` config table). Co-tenancy is supported at logical-ACL level even if the on-disk LanceDB is shared. The "❌" is over-strong.

19. **Dimension 6 API table, Archon "REST API — ❌ MCP-only (FastMCP HTTP); no raw REST for non-Claude consumers"** — **WRONG.** `archon_search/server/app.py:1` is literally `"""FastAPI app factory for archon-search REST control plane."""`. There are routes_health, routes_state, routes_status, routes_search, routes_route, routes_collections, routes_jobs, routes_telemetry. An `/openapi.json` is generated. This is a major factual error.

20. **Dimension 6, Archon "MCP server (exposes tools) — `... ArchonToolkit: 10 session-level search tools`"** — There are 9 MCP tools (verified in `mcp.py`, matches CLAUDE.md). "ArchonToolkit: 10 session-level search tools" describes a non-existent module.

21. **Dimension 6, Archon "MCP client (consumes tools) — ✅ Full MCP client via ArchonMCPServer"** — No such component in `archon_search/`. archon-search exposes MCP, it does not consume external MCP servers.

22. **Dimension 6, Archon "CLI — `archon` entry point; 8 search subcommands; `collection add/remove/reindex`"** — Entry point is `archon-search` (per pyproject + CLAUDE.md), not `archon`. CLI registers 9 subcommands (`start, stop, status, install, uninstall, ingest, sync, collection, config`), not 8.

23. **Dimension 6, Archon "Authentication — ❌ No auth on FastMCP server (assumed localhost)"** — **WRONG.** `server/middleware_auth.py` implements Bearer token auth on the FastAPI app; `key_manager.py` auto-generates `~/.archon-search/.search.env` with mode 600 on first start. CLAUDE.md confirms: "All endpoints except `GET /health` require a `Bearer` token." Verified at `middleware_auth.py:31`.

24. **Dimension 6, Archon "Telegram — ✅ (core feature — Archon is a Telegram bot)"** — **WRONG.** Zero `Telegram` references in `archon_search/`. archon-search is a hybrid retrieval server, not a Telegram bot. The claim "Archon is a Telegram bot" is fundamentally wrong about the product being analyzed.

25. **Dimension 6, Archon "Health endpoint — `GET /health` on FastMCP HTTP server"** — `/health` exists, but on the FastAPI app and additionally on the MCP HTTP wrapper. Attributing it to "FastMCP HTTP server" alone is wrong. (`server/app.py` plus `mcp.py` health_check.)

26. **Dimension 7 ops table, Archon "Install method — `SearchInstaller` wizard with GPU detection and provider validation"** — `install.py` exists (provider validation, GPU `fastembed-gpu` swap), but there is no class named `SearchInstaller`, and the user-facing entry is `archon-search install`. The "wizard" framing is decorative.

27. **Dimension 7, Archon "Config format — `config.toml` (20+ validated fields); `.env` for bot token"** — File is `archon-search.toml` (per CLAUDE.md and `archon-search.toml.example`), located at `~/.archon-search/archon-search.toml`. There is no "bot token" — confirms the Telegram framing is wrong.

28. **Dimension 7, Archon "Health / diagnostics — `archon doctor` CLI"** — **WRONG.** No `doctor` command in `cli/main.py`. Subcommands are `start, stop, status, install, uninstall, ingest, sync, collection, config`. `_diagnostics.py` exists but is not wired as a `doctor` subcommand.

29. **Dimension 7, Archon "Monitoring — `IndexingNotificationMonitor` — Telegram summary when all collections reach terminal state"** — **WRONG.** No such class or module exists.

30. **Dimension 7, Archon "Scheduled maintenance — `JobScheduler` for user-defined tasks"** — **WRONG.** `archon_search/jobs/` contains an async job *store* (model + store) for long-running ingest/reindex operations, not a user-task scheduler.

31. **Dimension 7, Archon "Auto-update — `archon update [--tag <version>]`"** — **WRONG.** No `update` subcommand in the CLI.

32. **Dimension 7, Archon "Windows support — Platform stubs exist; service management not yet implemented"** — Verified: `platform/windows.py` is explicitly a NotImplementedError stub. **Accurate.**

33. **Dimension 7, Archon "Multi-tenancy — ❌ Single-user (whitelist of Telegram user IDs)"** — Wrong framing. archon-search supports namespaces + ACLs (`acl.py`, `[namespaces]` TOML section). There is no "whitelist of Telegram user IDs" anywhere.

34. **Dimension 8 testing table, Archon "Search-specific test lines — 13,843 across 18 test modules"** — Total `test_*.py` line count is ~34,432 across ~84 files (verified `find ... | wc -l`). The specific module counts cited ("`test_sync.py` 4,613 · `test_install.py` 1,892 · `test_server.py` 1,611 · `test_pipeline.py` 1,172 · `test_store.py` 1,024") are close but off: actual line counts are 4,612 / 1,858 / (no `test_server.py` at root — server tests are under `tests/server/`) / 2,129 / 2,415. "`test_server.py` 1,611" is not verifiable as a single file.

35. **Dimension 8, Archon "Static analysis — `mypy` (full)"** — pyproject does not configure mypy as far as I checked; coverage and ruff are configured. Cannot fully verify "mypy full" — flag as **unverified**.

36. **Dimension 9 perf table, Archon "Vector search complexity — Sub-linear — LanceDB IVF-PQ ANN"** — LanceDB does use IVF-PQ, but archon-search does not appear to explicitly create an ANN index in `store.py`. The complexity depends on whether `create_index` is called. **Unverified.**

37. **Dimension 9, Archon "Reranker throughput — ONNX CPU; batch inference; practical for interactive use"** — Reranker is `fastembed.TextCrossEncoder` with optional CUDA/CoreML providers (`reranker.py`). Not strictly "ONNX CPU". Misleading.

38. **Dimension 9, Archon "FTS rebuild cost — Full FTS rebuild on every `ingest_directory()`; individual `ingest_file()` skips rebuild"** — Not verified from a single source line; would require inspecting `store.py` FTS index lifecycle. **Unverified.**

39. **Dimension 10 features table, Archon "Pinned collections — Always searched regardless of router decision"** — Verified: `config.py` has `pinned_collections: list[str] = field(default_factory=list)`. **Accurate** at the config level; semantics ("always searched") is plausible per `router.py` but not directly traced here.

40. **Dimension 10, Archon "Auto-description generation — Haiku samples 20 random chunks → collection description; re-triggers on 20%+ doc change"** — Verified above (item 5). **Accurate.**

41. **Dimension 10, Archon "Indexing completion notification — ✅ Telegram summary when all collections reach terminal state"** — **WRONG.** No Telegram, no such notifier.

42. **Dimension 11 memory table, Archon "Native LLM session integration — MCP tools injected directly into Claude Code sessions; search context provided at session startup via `ContextProvider`"** — Only the MCP-tool-exposure half is true. **`ContextProvider` does not exist** in `archon_search/`.

43. **Dimension 11, Archon "Conversation history as retrieval signal — `HistoryManager` session history; `ContextProvider` injects history context at startup"** — **WRONG.** No `HistoryManager`. No `ContextProvider`. archon-search has no conversation-history feature.

44. **Dimension 11, Archon "Reminder / drift prevention — `ContextReminder` injects `REMINDER.md` every N messages/tokens"** — **WRONG.** No `ContextReminder`, no `REMINDER.md` mechanism.

45. **Verdict section — "Archon (89) and R2R (86) are the two production-quality systems"** — Total score for Archon is built on multiple inflated cells (esp. reranker model, REST API, auth, doctor, monitoring, Telegram). Verdict and ranking are not trustworthy without rescoring against verified facts.

46. **Opportunities table item #6 — "REST API alongside MCP — Add a thin FastAPI REST endpoint layer on the FastMCP server for non-Claude consumers"** — **The REST API already exists.** This "opportunity" is moot. (Direct contradiction with `server/app.py`.)

47. **Opportunities item #3 — "Per-collection embedding model override — `CollectionMeta.embedding_model` field already exists in Archon — Wire ... into ingest and search validation paths."** — Field exists (`collection_meta.py`); router already uses it for skip logic. The "not wired into ingest" half is correct. **Partially accurate**, valid roadmap item.

48. **Self-contradiction — Dimension 2 says PDF parsing uses "trafilatura/docling".** `parser.py` shows trafilatura handles HTML, docling handles PDF and OCR/images, markitdown handles docx/pptx/xlsx. The combined "trafilatura/docling" for PDF is misleading.

49. **Self-contradiction — Dimension 6 "REST API ❌" vs. Dimension 6 "Health endpoint ✅ `GET /health` on FastMCP HTTP server"** — `/health` is on the FastAPI REST app. The document contradicts itself within one table.

50. **Audience / status framing — "Last reviewed: 2026-04-30 / Next review: 2026-10-30"** — Reviewed dates do not reflect the underlying inaccuracies; the document needs a full rewrite, not a date bump.

## Verified claims

- Chunker is Chonkie `RecursiveChunker`, GPT-2 tokenizer, 512 tokens default. (`chunker.py`)
- Default embedding model `BAAI/bge-small-en-v1.5` (384-dim). (`config.py:34`)
- LanceDB native FTS + vector hybrid with RRF, k=60 constant. (`store.py:29`, `_rrf_score`)
- Watcher uses watchdog, 5s debounce. (`watcher.py`)
- Crash recovery: stale `IN_PROGRESS → PENDING` reset on restart. (`sync.py:321`)
- Per-collection state machine for indexing. (`sync.py`)
- Routing: 3-tier strategy with `routing_shortlist_size=8`, `routing_confidence_threshold=0.30`, `max_parallel_collections=3`. (`config.py`, `router.py`)
- Description generator: Haiku, samples up to 20 chunks, regenerates at ≥20% doc-count delta. (`description_generator.py`)
- Auto-reindex on `chunk_size` change toggle. (`config.py:37`)
- 9 MCP tools registered in `server/mcp.py`.
- Bearer token auth on every route except `/health`. (`server/middleware_auth.py`, `key_manager.py`)
- Pinned collections config field exists. (`config.py:46`)
- Windows service stubs raise `NotImplementedError`. (`platform/windows.py`)
- LanceDB-only vector store, no alternative backends.
- `CollectionMeta.embedding_model` field exists; ingest does not honor per-collection overrides.

## Unverifiable / ambiguous

- All competitor claims (AnythingLLM v1.12.1, PrivateGPT v0.6.2, Kotaemon, mem0 v3, R2R v3.6.6) — version strings, code-quality cited issues, default settings, line counts, throughput numbers. Per review rules, flag as **unverifiable** unless trivially confirmable. Specific high-impact competitor claims that should be re-verified before publication:
  - "AnythingLLM reranker ~5.2s/20 docs on i7" — anecdotal, no source.
  - "Kotaemon FTS hard-coded to -1.0" — specific code claim, needs re-verification.
  - "mem0 v3 ADD-only algorithm; 93% temporal reasoning accuracy claimed" — vendor claim, attribution needed.
  - "R2R `ingestion_concurrency_limit=16`, batch size 128" — specific constants, needs re-verification.
  - "R2R binary quantization: `INT1` bit vector column; 32x storage reduction" — needs re-verification.
  - "PrivateGPT `similarity_top_k=2` default" and `"make_this_parameterizable_per_api_call"` TODO — needs re-verification.
  - "AnythingLLM 10 vector store options" / "mem0 19 vector store adapters" / "R2R Postgres-only" — counts need re-verification.
  - All "Total parsed formats" counts in the format-support matrix.
- Archon-specific items still ambiguous:
  - Whether LanceDB ANN index (IVF-PQ) is explicitly created or relies on default scan (#36).
  - FTS rebuild lifecycle on `ingest_directory` vs. `ingest_file` (#38).
  - "mypy (full)" — not confirmed by pyproject inspection (#35).
  - Test-module-specific line counts cited in the document (#34) — close but not exact.

## Recommendation

This document needs a structural rewrite. At minimum:

1. Strip every claim about Telegram, `archon doctor`, `archon update`, `HistoryManager`, `ContextProvider`, `ContextReminder`, `JobScheduler`, `IndexingNotificationMonitor`, `ArchonToolkit`, `SearchInstaller`, `ArchonMCPServer`, `archon_toolkit_search.py`, "bot token", "Telegram user IDs whitelist".
2. Correct: default reranker (MiniLM-L-6-v2, not bge-reranker-v2-m3), default `top_k_retrieve` (15, not 5), REST API exists, Bearer auth exists, CLI entry-point name (`archon-search`), CLI subcommands list, config filename (`archon-search.toml`).
3. Drop "Opportunity #6 — REST API alongside MCP" since it already ships.
4. Rescore every dimension from the corrected baseline; the verdict ranking is currently not defensible.
5. Re-verify every competitor cell against current upstream sources, or mark them clearly as "based on docs as of YYYY-MM, not independently re-audited".
