# Review: Backlog/02_competitive_analysis_marveen.md

## Summary

Reviewed `Documentation/Backlog/02_competitive_analysis_marveen.md` against
`/Users/manczg/Documents/development/archon-search/archon_search/` source. The
document is generally directionally accurate about archon-search's pipeline
shape (parser → chunker → embedder → store → reranker → router, LanceDB,
fastembed, FastMCP, hybrid + RRF, cross-encoder rerank, watcher debounce,
crash recovery), but it contains a number of concrete factual errors —
mostly inflated/invented features, wrong defaults, and stale module names
inherited from the pre-standalone (`archon.*`) era. All Marveen claims are
unverified (per rules, flagged unverifiable).

The most consequential errors are: (a) the default reranker model is
misstated; (b) the document claims a REST surface does not exist when in
fact `archon_search/server/routes_*.py` defines a full FastAPI REST surface
running alongside MCP under shared Bearer auth; (c) "ArchonToolkit", "archon
doctor", "IndexingNotificationMonitor", "Telegram notifications", and
"58 binary exclusions" are not present in the codebase; (d) several test
line counts and the test module roster are wrong.

## Inaccuracies (numbered)

1. **L26 — "archon_toolkit_search.py is 884 lines"**: file does not exist in
   `archon_search/`. The package has no `archon_toolkit_search.py`. Largest
   modules are `sync.py` (889 lines) and `store.py` (841 lines).

2. **L26 — "FastMCP HTTP server decouples the search service from the main
   daemon"**: archon-search is a standalone server; there is no "main
   daemon" to decouple from. Stale framing from the monorepo era.

3. **L33 — "FastMCP decoupling means the search server survives main daemon
   restarts"**: same — no parent daemon exists in this repo.

4. **L60 — "15+ formats, 58 binary exclusions"**: the `_BINARY_EXTENSIONS`
   frozenset in `archon_search/pipeline.py` contains ~40 unique extensions
   (counted via tokenisation), not 58. Parsed format count is plausible
   (HTML/PDF/Office/images via trafilatura/docling/markitdown) but the
   "15+" figure is not enforced anywhere.

5. **L60 — "Chonkie RecursiveChunker (512-token, GPT-2 tokenizer)"**: the
   tokenizer string is correct (`tokenizer="gpt2"` in `chunker.py:15`), but
   the 512 figure is the *default* `chunk_size` in `SearchConfig`
   (`config.py:36`), not a fixed property of the chunker.

6. **L66 — "Auto description generation via Haiku (20% doc change
   threshold)"**: the 20% threshold is correct
   (`description_generator.py:58`, `>= 0.20`). "Haiku" is correct
   (`DEFAULT_FAST_MODEL = "claude-haiku-4-5-20251001"` in `constants.py:6`).
   No inaccuracy here — flagged for completeness.

7. **L67 — "Filesystem watcher for live synchronization"**: correct
   (`watcher.py`, watchdog, 5.0s debounce). Score-row claim only.

8. **L72 — "DocumentParser is not thread-safe (explicitly documented)"**:
   I could not find any explicit thread-safety statement in `parser.py`.
   The claim that it is "explicitly documented" is unsupported by source.

9. **L99 — "cross-encoder reranking (BAAI/bge-reranker-v2-m3)"**: **Wrong
   default.** `SearchConfig.reranker_model` defaults to
   `"cross-encoder/ms-marco-MiniLM-L-6-v2"` (`config.py:35`), not
   `BAAI/bge-reranker-v2-m3`.

10. **L105 — "Adaptive fetch size: max(top_k * 3, 20)"**: correct
    (`store.py:471`). No inaccuracy.

11. **L139 — "Default: BAAI/bge-small-en-v1.5 (384-dim, 33 MB ONNX, no GPU
    required)"**: model name is correct (`config.py:34`). The 384-dim / 33
    MB / "no GPU required" specifics are not asserted in source —
    unverifiable from this repo (true of the model in general, but the
    document presents them as archon-search facts).

12. **L140 — "Reranker: BAAI/bge-reranker-v2-m3 (cross-encoder)"**: same
    error as #9 — wrong default.

13. **L141 — "fastembed (CPU / CUDA / CoreML) — auto-detected by
    SearchInstaller.detect_gpu()"**: method exists but is named
    `detect_gpu` returning a `GpuType`; runtime detects CUDA on Linux,
    METAL on ARM macOS, NONE otherwise (`platform/runtime.py:38`). The
    "CoreML" execution-provider claim is overstated — `install.py:286`
    references CoreML acceleration validation on macOS, so partially
    accurate, but the framing as a clean three-way auto-detect is
    simplified.

14. **L144 — "install.py validate_providers() tests the embedding stack
    before committing config"**: `_SEARCH_PACKAGES` and provider
    validation logic exist in `install.py`, but the specific method name
    `validate_providers()` was not located. Probably stale name — verify
    against `install.py` before re-using in docs.

15. **L188 — "metadata table with centroid, doc count, embedding model"**:
    consistent with `_ROUTING_FIELDS = {"name", "description", "centroid",
    "embedding_model", "doc_count", "chunk_count"}` (`router.py:21`).
    Accurate.

16. **L231 — "FastMCP server: 9 tools"**: correct (`server/mcp.py:35`
    docstring "Create a FastMCP app with 9 RAG tools registered"; matches
    the list in CLAUDE.md). Tool names listed in the doc match.

17. **L232 — "ArchonToolkit: 10 MCP tools registered into Claude
    sessions"**: **no `ArchonToolkit` exists** in `archon_search/`. The
    only MCP surface is `server/mcp.py`. Invented or stale claim from a
    different repo.

18. **L233 — "CLI: archon search <subcommand> — 8 subcommands"**: CLI
    binary is `archon-search` (not `archon search`); `cli/main.py`
    registers 9 commands: `start`, `stop`, `status`, `install`,
    `uninstall`, `ingest`, `sync`, `collection`, `config`. So the
    invocation prefix and count are both wrong.

19. **L234 — "Health endpoint: GET /health on the FastMCP HTTP server"**:
    `/health` lives on the FastAPI app (`server/routes_health.py`), not
    on FastMCP. FastAPI + FastMCP run side-by-side; the doc collapses
    them.

20. **L242 — "No REST API (only MCP protocol)"**: **False.**
    `archon_search/server/` contains a full REST surface:
    `routes_health.py`, `routes_state.py`, `routes_status.py`,
    `routes_search.py`, `routes_route.py`, `routes_collections.py`,
    `routes_jobs.py`, `routes_telemetry.py`. CLAUDE.md and
    `Documentation/Architecture/600_api_reference_or_public_interface.md`
    both treat REST as authoritative (`GET /openapi.json`). Listing this
    as a weakness is the largest factual error in the doc.

21. **L244 — "No authentication on the FastMCP server (assumed
    localhost-only)"**: **False.** `server/middleware_auth.py`
    implements Bearer-token auth (`APIKeyMiddleware`), keys auto-generated
    via `key_manager.py`. All endpoints except `GET /health` require a
    Bearer token (per CLAUDE.md).

22. **L275 — "SearchInstaller wizard with GPU detection, provider
    validation, config write-back"**: `SearchInstaller` class exists in
    `install.py:29`; GPU detection and provider-list setup confirmed.
    Reasonable.

23. **L277 — "Config: 20+ fields in [search] section"**: archon-search
    config is not nested under a single `[search]` table — it uses
    `[server]`, `[database]`, `[routing]`, `[collections]`, `[logging]`,
    `[telemetry]`, `[namespaces]` (see `config.py:131-233`). The doc
    confuses the standalone config layout with the prior monorepo layout.

24. **L278 — "archon doctor checks service status, collection health,
    indexing state"**: **no `doctor` subcommand exists** in
    `archon_search/cli/`. `cli/main.py` does not register one. Invented
    feature.

25. **L280 — "IndexingNotificationMonitor sends Telegram summary when all
    collections reach terminal state"**: **does not exist.** No
    `notification_monitor` module, no `telegram` references, and no
    `IndexingNotificationMonitor` class anywhere in `archon_search/`.
    Invented feature.

26. **L282 — "Service management: macOS launchd / Linux systemd via
    PlatformService ABC"**: `archon_search/platform/` includes `macos.py`
    (LaunchdSearchService), `linux.py`, `windows.py`, `service.py`. The
    ABC exists; Windows is also supported (not just launchd/systemd). Doc
    omits Windows.

27. **L286 — "Telegram notification on indexing completion"**: see #25.
    Does not exist.

28. **L324 — "Test lines: 13,843 across 18 search test modules"**: the
    test suite has 58 files totaling ~35,763 lines. The doc's table only
    sums to ~13,843 because it cherry-picks ~15 files. The "18 search
    test modules" figure is invented; there is no `tests/search/`
    subpackage in the standalone repo.

29. **L334 — table row "test_sync.py 4,613"**: actual 4,612. Negligible
    but off-by-one.

30. **L335 — "test_install.py 1,892"**: actual 1,858.

31. **L336 — "test_server.py 1,611"**: **no `test_server.py` exists.**
    Server tests live in `tests/server/` and `tests/test_app.py` (no
    file named `test_server.py`).

32. **L337 — "test_pipeline.py 1,172"**: actual 2,129. Significantly off.

33. **L338 — "test_store.py 1,024"**: actual 2,415. Significantly off.

34. **L339 — "test_progress.py 874"**: actual 1,001.

35. **L340 — "test_watcher.py 711"**: actual 771.

36. **L342 — "test_router.py 325"**: actual 325. Accurate.

37. **L343 — "test_notification_monitor.py 399"**: **file does not
    exist.** Consistent with #25.

38. **L344 — "test_reranker.py 197"**: actual 347.

39. **L345 — "test_embedder.py 174"**: actual 254.

40. **L346 — "test_description_generator.py 172"**: file exists but
    line-count not verified line-for-line; not material.

41. **L354 — "No coverage threshold enforcement visible in CI config"**:
    **False.** `pyproject.toml` `addopts` enforces `--cov-fail-under=85`
    (per CLAUDE.md "Default pytest run … Coverage gate
    (--cov-fail-under=85)"). The threshold is enforced.

42. **L390 — "FTS rebuild: Full rebuild per ingest_directory() call"**:
    consistent with `store.py:451` (`create_index("text", config=FTS(),
    replace=True)`) and `pipeline.py:255`. Verified.

43. **L444 — "Filesystem watcher | watchdog-based live sync with
    per-collection debounce (5s)"**: matches `watcher.py:47`
    (`debounce_seconds: float = 5.0`). Verified.

44. **L445 — "Indexing notification | Telegram summary when all
    collections reach terminal state"**: see #25 — does not exist.

45. **L447 — "Auto-reindex on chunk size change"**: confirmed
    (`config.py:37`, `auto_reindex_on_chunk_size_change: bool = True`;
    `sync.py:412` references the warning). Verified.

46. **L515 — Recommendation §7 "Switch to incremental FTS updates:
    LanceDB's table.create_index(..., replace=False) is additive"**:
    technical claim about LanceDB API semantics — unverifiable from this
    repo. Should be checked against LanceDB docs before acting on the
    recommendation.

47. **L509 — Recommendation §5 "The embedding_model field already exists
    in CollectionMeta — wire it into the ingest and validation paths"**:
    `embedding_model` is indeed in `_ROUTING_FIELDS` and used by router
    for mismatch detection (`router.py:142`). Verified.

## Verified claims

- Hybrid retrieval uses RRF with k=60 (`store.py:29` `_RRF_K = 60`).
- LanceDB is the store; native FTS via `FTS()` index on `text` column
  (`store.py:451`).
- SHA256 content addressing for doc_id (`store.py:552`,
  `pipeline.py:140`).
- Chonkie `RecursiveChunker` with `tokenizer="gpt2"` (`chunker.py:15`).
- Parsers: trafilatura (HTML), docling (PDF/images OCR), markitdown
  (Office) (`parser.py:6-9`).
- Description generator uses Haiku (`DEFAULT_FAST_MODEL =
  "claude-haiku-4-5-20251001"`), 20-chunk sample, 30s timeout, 20%
  regeneration threshold.
- Crash recovery resets stale `IN_PROGRESS → PENDING` on restart
  (`sync.py:129`, `sync.py:321-344`).
- `max(top_k * 3, 20)` adaptive fetch (`store.py:471`).
- Watcher debounce 5.0s (`watcher.py:47`).
- Router has 3 routing tiers based on collection count
  (`router.py:198-203`).
- FastMCP server exposes 9 tools (`server/mcp.py:35`).
- Bearer-token auth via `APIKeyMiddleware` (`server/middleware_auth.py`).
- LaunchdSearchService for macOS exists (`platform/macos.py`).
- `SearchInstaller` with `detect_gpu()` exists (`install.py:29`,
  `install.py:57`).
- `embedding_model` default `BAAI/bge-small-en-v1.5` (`config.py:34`).
- `max_parallel_collections` defaults to 3 (`config.py:44`).

## Unverifiable / ambiguous

- Every factual claim about **Marveen** (db.ts line counts, FTS5 triggers,
  BM25, nomic-embed-text dim, salience decay rates, AES-256-GCM vault,
  trust graph, prompt-injection sentinels, install scripts, port locking,
  PID file legitimacy check, tmux pane state detection, ~10 Vitest test
  files, `prompt-safety.test.ts` etc.) — flagged unverifiable per review
  rules. Did not enter the Marveen repo.
- Model specs treated as archon-search facts but really upstream model
  properties (e.g. "bge-small-en-v1.5 is 384-dim / 33 MB ONNX",
  "nomic-embed-text is 768-dim", "fastembed uses ONNX Runtime") —
  externally true in general but not asserted by archon-search source.
- Performance / scalability table cells like "sub-millisecond vector
  search at millions of records", "Memory per chunk ~1.5 KB", "LanceDB
  ANN scales to millions of documents without latency degradation",
  "millions of rows" — no benchmarks in this repo support them. The
  `archon_search/eval/` harness explicitly documents that latency p50/p95
  is a regression guard, not a production SLA (CLAUDE.md), which directly
  conflicts with the implication of absolute numbers.
- Scoring (all the "/10" numbers and the 83 vs 46 verdict) is editorial,
  not a factual claim — not graded.
- Recommendation §3 "LanceDB supports predicate pushdown natively" — true
  per LanceDB docs in general but should be re-verified before
  implementation work.
