# E1 — Integration & E2E Test Coverage

**Purpose**: Close the systematic gap where ~30 completed features have strong unit coverage at the pipeline/store layer but lack tests that exercise the full chain: HTTP request → middleware → route handler → pipeline → LanceDB → serialized response. Also covers missing CLI e2e, MCP error-path, job-dispatch, and centroid/routing integration tests discovered by a per-plan audit of A1–D2.
**Audience**: Engineering team; CI gate enforces regressions don't slip through the HTTP/MCP/CLI surface.
**Status**: To Do

---

## Background

A 37-agent audit of all completed and in-progress plan files (A1–D2) found that the dominant test gap is not missing unit tests but missing integration and e2e tests that verify the wiring between layers. Six cross-cutting themes emerged:

1. **HTTP layer untested for most features** — nearly every plan has unit tests at pipeline or store level, but the path HTTP→middleware→route→pipeline→store→JSON serialization is not exercised. A regression in `SearchResultSchema.from_result()`, a mismatched field rename, or a broken middleware bypass would be invisible to the current suite.
2. **Dispatch/scheduler never exercised end-to-end** — D1-D2 and D2-backup have unit tests for individual workers and route handlers, but no test runs the real `JobScheduler` dispatch through a full lifespan. The dispatch fix was the primary D2 motivation.
3. **MCP tool error paths systematically untested** — REST error-path coverage exists but the equivalent MCP tool error paths (validation errors, typed exceptions, dependency-absent errors) are missing for B3, C4, C5, C7.
4. **CLI commands lack real e2e coverage** — CLI tests almost universally mock `httpx` or bypass Click wiring. A break in Click parameter→handler→HTTP chain is invisible.
5. **Real disk I/O never verified** — `atomic_write_json` and `configure_logging` are always tested with mocks; no test confirms data lands on disk and survives a read-back.
6. **Env var + app startup integration absent** — combining `ARCHON_SEARCH_DATA_DIR` → `load_config` → `create_app` lifespan → verify via endpoint has no test.

---

## Goal

Every significant feature from A1 through D2 has at least one integration test that exercises real components end-to-end without mocking the system under test, and at least one e2e test that traces a complete user-visible flow. All new tests pass in the default `uv run pytest` run (parallel, no extra flags).

---

## Scope

### In Scope
- Integration tests: multiple real components collaborating (real `SearchStore`, real `SearchPipeline`, real LanceDB in `tmp_path`, `TestClient` against real FastAPI app)
- E2E tests: full user-visible flows (CLI via `CliRunner`, HTTP via `TestClient` or `httpx` against real server, MCP tool invocations)
- One test file per theme/feature area — no omnibus files
- All tests run under `uv run pytest` with the default xdist config

### Out of Scope
- Replacing or modifying existing unit tests
- Testing third-party library internals (fastembed model quality, LanceDB SQL semantics)
- Live network or real fastembed model downloads (tests use stubs)
- D2-backup scheduled timer (interval tick tested separately in unit tests)
- Watcher/sync integration tests (`watcher.py` + `sync.py`) — these require a real filesystem watchdog event loop which is difficult to control in-process; deferred to a separate watcher integration plan

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See Task 7.1 — Final verification & documentation update.

---

## What does NOT change
- `tests/conftest.py` — existing fixtures stay; new fixtures added only as needed
- Existing test files — no modifications to pass/fail state of current tests
- Production code — this plan adds tests only; any production bug found is filed separately
- `pyproject.toml` addopts, markers, norecursedirs — no changes unless a new marker is needed

---

## Known limitations / accepted trade-offs
- Tests that need the full server process (not TestClient) are left for a future live-server E2E plan; here we use `TestClient` which covers the same routing/middleware without managing a subprocess.
- Some centroid correctness tests (B5) use a tolerance of 1e-5 because floating-point accumulation order differs across runs.
- The dispatch/scheduler tests (Phase 2) exercise the sync path through `create_app` lifespan; they do not test the async background tick.

---

## Architecture

No new production modules. New test modules follow the structure:

```
tests/
  integration/           # already exists for some plans
    test_http_metadata_fields.py        (Phase 1)
    test_http_filters_round_trip.py     (Phase 1)
    test_http_multi_collection.py       (Phase 1)
    test_http_per_collection_model.py   (Phase 1)
    test_http_enrichment_metadata.py    (Phase 1)
    test_http_hyde_rag_fusion.py        (Phase 1)
    test_dispatch_scheduler_e2e.py      (Phase 2)
    test_backup_dispatch_e2e.py         (Phase 2)
    test_mcp_error_paths.py             (Phase 3)
    test_mcp_schema_contract.py         (Phase 3)
    test_cli_e2e.py                     (Phase 4)
    test_wizard_e2e.py                  (Phase 4)
    test_centroid_integration.py        (Phase 5)
    test_routing_integration.py         (Phase 5)
    test_fts_delete_no_phantom.py       (Phase 6)
    test_container_env_integration.py   (Phase 6)
    test_observability_integration.py   (Phase 6)
    test_acl_namespace_integration.py   (Phase 6)
    test_collection_lifecycle_integration.py  (Phase 6)
```

Shared helpers used across test modules:
- `make_real_app(tmp_path, monkeypatch, *, backup_enabled=False, namespaces: dict[str, str] | None = None)` — returns `(TestClient, config, api_key)` backed by real `SearchStore` + `SearchPipeline` in `tmp_path`. The helper MUST accept `monkeypatch` as a parameter and use `monkeypatch.setenv` for BOTH `ARCHON_SEARCH_DATA_DIR` and `ARCHON_SEARCH_API_KEY` — this auto-reverts env vars after each test. Signature: `make_real_app(tmp_path, monkeypatch, *, backup_enabled=False, namespaces: dict[str, str] | None = None) -> tuple[TestClient, config, api_key]`. Pass `namespaces={'key_hex_a': 'ns-a', 'key_hex_b': 'ns-b'}` for multi-namespace tests. When provided, the namespaces dict is set on `config` before `create_app` is called, so `APIKeyMiddleware` initializes with the correct namespace mapping. The `api_key` in the return tuple is the single-namespace default key; for namespace-specific keys, use the keys from the `namespaces` dict directly. Do NOT use raw `os.environ` assignment — it leaks across tests in the same xdist worker. All requests through the returned client must include `Authorization: Bearer <api_key>`. To prevent BackupLoop from running during tests, set `config.backup.interval_hours = 0` — this makes the backup trigger loop self-exit immediately (the loop checks `interval_hours > 0` before sleeping). Pass `backup_enabled=True` only in Task 2.2 backup tests; when `True`, set `config.backup.interval_hours = 1` and `config.backup.output_dir = str(tmp_path / 'backups')`. The watcher is not started by the server process and needs no special handling. The helper must create and pass a `JobScheduler` to `create_app`. Specifically: create a `JobStore(jobs_file=tmp_path / 'jobs.json')`, then `scheduler = JobScheduler(store=job_store, max_concurrent=config.jobs.max_concurrent_bulk, dispatch_fn=lambda job: None)`. The `dispatch_fn` no-op lambda is replaced by the real dispatch function during `create_app` lifespan startup — passing a no-op here satisfies the constructor requirement. Pass both to `create_app(config, job_store=job_store, scheduler=scheduler)`. Without this, export/import jobs stay QUEUED forever — the scheduler background tick is never started. The returned `TestClient` exposes `client.app.state.scheduler` for tests that need to manipulate the scheduler directly (Task 2.1). Return `(TestClient, config, api_key)` — tests needing scheduler access use `client.app.state.scheduler`.
- `ingest_doc(client, col, text, path, *, timeout_s=10)` — POST ingest + poll GET `/jobs/{id}` with 100ms sleep until `status == 'done'`. Raises `pytest.fail(f'ingest did not complete in {timeout_s}s')` after timeout.
- `search(client, col, query, **filters)` — POST /search, assert 200, return items
- `async def make_real_pipeline(tmp_path, monkeypatch)` — creates `SearchStore(db_path=str(tmp_path / 'db'), ...)`, calls `await store.connect()`, creates `SearchPipeline(store=store, embedder=stub_embedder, ...)`. Uses `monkeypatch.setenv('ARCHON_SEARCH_DATA_DIR', str(tmp_path))`. Returns `(store, pipeline)`. Uses stub embedder (same stubs as test suite's `install_stubs()`). Must call `await store.ensure_collection(col_name)` before ingest operations. Callers must use `await make_real_pipeline(...)` from inside `async def` test functions. Tasks 5.1 and 5.2 use this helper instead of `make_real_app` because they call async pipeline/store methods directly (`pipeline.ingest_file`, `pipeline.delete_document`, `store.get_collection_meta`, `recompute_collection_meta`, `MultiCollectionRouter.rank()`) without going through TestClient — and all tests in Tasks 5.1 and 5.2 are already required to be `async def` (see Task 5.1 note). Follow the pattern from `tests/test_pipeline_acl.py:_make_pipeline` but make it `async def` to call `await store.connect()` directly. Note: missing `await store.connect()` is the most likely failure — `SearchStore` is a lazy-connect class and raises `RuntimeError("not connected")` without it.

All task descriptions that say "poll until DONE" mean: max 10s timeout, 100ms poll interval, `pytest.fail` on timeout.

Shared helpers go in `tests/integration/conftest.py` (new file only — do NOT modify the existing `tests/conftest.py`).

---

## Task breakdown

### Phase 1 — HTTP Layer Integration Tests
> **Releasable**: after each task — each test file is independently runnable and immediately closes a regression gap

#### Task 1.1 — Metadata fields flow through ingest → search HTTP round-trip
- [x] **File**: `tests/integration/test_http_metadata_fields.py`
- **Depends on**: nothing
- **Description**:
  - `test_x_ingested_by_header_flows_through_to_lancedb_row`: POST `/ingest` with `X-Ingested-By: watcher` via `TestClient` against real FastAPI app (real `SearchPipeline`, real `SearchStore`, real LanceDB in `tmp_path`). Poll until job DONE. Assert stored row has `ingested_by == 'watcher'` by reading back via `store.hybrid_search`.
  - `test_rest_search_response_carries_metadata_fields_from_real_ingest`: Real `pipeline.ingest_file` → POST `/search` via TestClient. Assert response JSON items contain non-empty `file_type`, non-empty `updated_at`, `ingested_by == 'cli'`, `acl` present. Verifies `SearchResultSchema.from_result()` wiring end-to-end.
  - `test_future_mtime_accepted_as_is`: Ingest a file with `os.utime` set 1 hour ahead. POST `/search`. Assert `updated_at` in response carries the future timestamp verbatim (no clamping). Catches any future mtime-normalization regression.
  - `test_reindex_metadata_cli_to_search_response_round_trip`: Seed LanceDB rows with legacy-shaped data (empty `file_type`, `ingested_by` set to `'cli'` or empty string). Run `archon-search collection reindex-metadata <col>` via `CliRunner`. POST `/search` against same real store. Assert response items carry `file_type`, `updated_at`, `ingested_by == 'reindex'` — this verifies the reindex path actually overwrites the field rather than leaving legacy values.
- **Releasable**: closing A1 HTTP regression gap
- **Tests (TDD)** — `tests/integration/test_http_metadata_fields.py`:
  - Integration: `test_x_ingested_by_header_flows_through_to_lancedb_row`
  - Integration: `test_rest_search_response_carries_metadata_fields_from_real_ingest`
  - Integration: `test_future_mtime_accepted_as_is`
  - E2E: `test_reindex_metadata_cli_to_search_response_round_trip`
  - Checkpoint: `uv run pytest tests/integration/test_http_metadata_fields.py -v`

#### Task 1.2 — Metadata filter HTTP round-trips
- [x] **File**: `tests/integration/test_http_filters_round_trip.py`
- **Depends on**: nothing
- **Description**:
  - `test_source_path_prefix_special_chars_round_trip_via_http`: POST `/search` with `source_path_prefix` containing `%`, `_`, `\`, `'`. Assert only matching docs returned. Verifies `build_where()` SQL-escaping through the full HTTP→LanceDB path.
  - `test_system_metadata_fields_always_present_regardless_of_include_metadata`: POST `/search` twice — once with `include_metadata=false`, once with `include_metadata=true`. Assert `file_type`, `updated_at`, `ingested_by`, `language` are non-empty in both responses. Assert custom `metadata` dict is only present when `include_metadata=true`.
  - `test_indexed_after_filter_excludes_older_docs`: Ingest doc A, sleep 10ms, record timestamp T, ingest doc B. POST `/search indexed_after=T`. Assert only doc B returned.
  - `test_language_filter_http_response_returns_matching_lang_only`: Ingest French and English docs with explicit `language` tag. POST `/search filters.language=fr`. Assert all results have `language == 'fr'`.
- **Releasable**: closing A2/C2 HTTP filter regression gap
- **Tests (TDD)** — `tests/integration/test_http_filters_round_trip.py`:
  - Integration: `test_source_path_prefix_special_chars_round_trip_via_http`
  - Integration: `test_system_metadata_fields_always_present_regardless_of_include_metadata`
  - Integration: `test_indexed_after_filter_excludes_older_docs`
  - Integration: `test_language_filter_http_response_returns_matching_lang_only`
  - Checkpoint: `uv run pytest tests/integration/test_http_filters_round_trip.py -v`

#### Task 1.3 — Multi-collection search and routing HTTP integration
- [x] **File**: `tests/integration/test_http_multi_collection.py`
- **Depends on**: nothing
- **Description**:
  - `test_e2e_multi_collection_search_full_stack`: Ingest into two real collections. POST `/search {collections: ['col_a', 'col_b'], query: '...'}`. Assert 200, every result has non-empty `collection` field, results from both collections present.
  - `test_e2e_multi_collection_missing_collection_returns_404`: POST `/search {collections: ['existing', 'ghost'], query: '...'}`. Assert 404.
  - `test_explain_request_rerank_false_multi_collections_is_422`: POST `/explain` with `{collections: ['a','b'], rerank: false}`. Assert 422 (Pydantic model_validator enforces `rerank=True` for multi-collection).
  - `test_post_route_hybrid_strategy_returns_200_and_uses_blended_ranking`: Config `routing_strategy='hybrid'`. Ingest two distinct corpora. POST `/route`. Assert 200 and returned collection matches corpus aligned with query description.
- **Releasable**: closing B3/B4 HTTP multi-collection gap
- **Tests (TDD)** — `tests/integration/test_http_multi_collection.py`:
  - E2E: `test_e2e_multi_collection_search_full_stack`
  - E2E: `test_e2e_multi_collection_missing_collection_returns_404`
  - Integration: `test_explain_request_rerank_false_multi_collections_is_422`
  - Integration: `test_post_route_hybrid_strategy_returns_200_and_uses_blended_ranking`
  - Checkpoint: `uv run pytest tests/integration/test_http_multi_collection.py -v`

#### Task 1.4 — Per-collection embedding model HTTP lifecycle
- [x] **File**: `tests/integration/test_http_per_collection_model.py`
- **Depends on**: nothing
- **Description**:
  - `test_full_lifecycle_patch_reindex_get`: PATCH `/collections/{name}` to set `active_embedding_model`. Assert 200 with `needs_reindex=True`. POST reindex job. Poll until DONE. GET `/collections/{name}`. Assert `active_embedding_model` promoted from pending.
  - `test_reindex_task_vectors_intact_after_failure`: Ingest with model-A. Start reindex to model-B (stub `_reindex_task` to raise midway via monkeypatch). Assert after failure: `active_embedding_model` still model-A; documents still searchable.
  - `test_search_remains_available_during_reindex_and_model_updates_after_done`: PATCH `/collections/{name}` to set a new `active_embedding_model`. Assert PATCH response shows `needs_reindex=True`. Use monkeypatch to inject a slow `_reindex_task` that uses `await asyncio.to_thread(release_event.wait)` — this blocks in a thread-pool worker, NOT the event loop, so concurrent requests can proceed. Use two threading.Events: `started_event` (set by the task when it begins) and `release_event` (test sets after search assertion). Test thread: (1) PATCH to set new model. (2) Wait for `started_event.wait(timeout=5)`. (3) POST `/search` (search proceeds because event loop is not blocked). (4) Assert 200 with results. (5) Set `release_event.set()` to let reindex finish. (6) Poll until DONE. (7) GET `/collections/{name}` and assert `active_embedding_model` updated. Note: do NOT use `threading.Event().wait()` directly inside the coroutine — that blocks the entire event loop thread and deadlocks all concurrent requests including the search POST. Note: "model-A vs model-B" result differentiation through search results does not exist — the only observable behavioral difference is `active_embedding_model` on the collection after DONE.
- **Releasable**: closing C1 HTTP lifecycle regression gap
- **Tests (TDD)** — `tests/integration/test_http_per_collection_model.py`:
  - Integration: `test_full_lifecycle_patch_reindex_get`
  - Integration: `test_reindex_task_vectors_intact_after_failure`
  - E2E: `test_search_remains_available_during_reindex_and_model_updates_after_done`
  - Checkpoint: `uv run pytest tests/integration/test_http_per_collection_model.py -v`

#### Task 1.5 — Content enrichment metadata in HTTP responses
- [x] **File**: `tests/integration/test_http_enrichment_metadata.py`
- **Depends on**: nothing
- **Description**:
  - `test_markdown_heading_flows_through_to_search_response`: Ingest a Markdown file with headings. POST `/search`. Assert response items carry `metadata._heading` and `metadata._section_path` fields.
  - `test_pdf_page_number_in_search_response`: Ingest a multi-page PDF via real docling parser (uses stub embedder). POST `/search`. Assert results carry `metadata._page_start` with an integer value.
  - `test_code_symbol_metadata_in_search_response`: Ingest a Python source file. POST `/search query='class '`. Assert results carry `metadata._symbol_kind` and `metadata._symbol_name`.
  - `test_image_file_assigns_page_start_one`: Ingest an image file (PNG). POST `/search`. Assert `metadata._page_start == 1`.
- **Releasable**: closing C3a/C3b/C3c enrichment HTTP gap
- **Tests (TDD)** — `tests/integration/test_http_enrichment_metadata.py`:
  - Integration: `test_markdown_heading_flows_through_to_search_response`
  - Integration: `test_pdf_page_number_in_search_response`
  - Integration: `test_code_symbol_metadata_in_search_response`
  - Integration: `test_image_file_assigns_page_start_one`
  - Checkpoint: `uv run pytest tests/integration/test_http_enrichment_metadata.py -v`

#### Task 1.6 — HyDE and RAG-Fusion HTTP integration
- [x] **File**: `tests/integration/test_http_hyde_rag_fusion.py`
- **Depends on**: nothing
- **Description**:
  - `test_hyde_kill_switch_returns_hyde_applied_false`: Config `hyde.enabled=false` (kill-switch). POST `/search {hyde: true}`. Assert 200 and `response.hyde_applied == false`.
  - `test_rag_fusion_multi_collection_search_returns_merged_results`: Ingest docs into two collections. POST `/search {rag_fusion: true, collections: ['a','b']}`. Assert 200, results from both collections, no duplicate `chunk_id`.
  - `test_hyde_dependency_absent_returns_clear_error`: Mock `anthropic` import to fail. POST `/search {hyde: true}`. Assert 4xx with error message referencing the missing dependency.
  - `test_rag_fusion_dependency_absent_returns_error`: Same for RAG fusion: mock `anthropic` absent, POST `/search {rag_fusion: true}`. Assert 4xx with clear message.
- **Releasable**: closing C4/C5 HTTP gap
- **Tests (TDD)** — `tests/integration/test_http_hyde_rag_fusion.py`:
  - Integration: `test_hyde_kill_switch_returns_hyde_applied_false`
  - Integration: `test_rag_fusion_multi_collection_search_returns_merged_results`
  - Integration: `test_hyde_dependency_absent_returns_clear_error`
  - Integration: `test_rag_fusion_dependency_absent_returns_error`
  - Checkpoint: `uv run pytest tests/integration/test_http_hyde_rag_fusion.py -v`

---

### Phase 2 — Job Dispatch & Scheduler End-to-End Tests
> **Releasable**: after Task 2.1 (export/import round-trip); after Task 2.2 (backup trigger verified on disk)

#### Task 2.1 — Export/import job dispatch integration
- [x] **File**: `tests/integration/test_dispatch_scheduler_e2e.py`
- **Depends on**: nothing
- **Description**:
  - `test_export_job_reaches_done_with_archive_on_disk`: POST `/collections/{name}/export` against real app with real `JobScheduler` (real `dispatch_fn`, not mocked). Poll `GET /jobs/{id}` until `status == 'done'`. Assert `.tar.gz` exists at `archive_path` and contains a valid manifest JSON.
  - `test_import_job_reaches_done_and_restores_searchable_collection`: Export a real collection. POST `/collections/{name}/import` with the archive. Poll until `status == 'done'`. POST `/search` against the imported collection. Assert results match original.
  - `test_user_job_precedes_backup_source_job`: Get `scheduler` from `client.app.state.scheduler`. Also apply `monkeypatch.setattr(archon_search.jobs.scheduler, '_SCHEDULER_TICK_SECONDS', 0.1)` BEFORE `make_real_app` — the `_tick()` method reads this module-level constant each iteration, so the monkeypatch takes effect immediately, bringing the dispatch wait from ~5s to ~0.1s. Use `monkeypatch.setattr(scheduler, '_max_concurrent', 0)` to prevent dispatch (the `_tick()` method checks `self._max_concurrent` on every cycle). Enqueue both jobs via `app.state.job_store.create_export(...)` directly (bypasses HTTP, controls timing). Then use `monkeypatch.setattr(scheduler, '_max_concurrent', 1)` to resume. Poll `GET /jobs` every 100ms (max 15s). Assert the `source='user'` job reaches RUNNING before the `source='backup'` job. Note: `max_concurrent_bulk` cannot be used — the scheduler stores it as `self._max_concurrent` at init and does not read `config` dynamically. `scheduler.pause()` does not exist. Completion timestamps are NOT a reliable signal — use RUNNING state transition instead, because completion depends on execution speed, but RUNNING order reflects dispatch priority.
  - `test_post_import_unsafe_tar_member_returns_422`: POST `/collections/{name}/import` with a zip-slip archive member (`../../evil.txt`). Assert 422, no file written outside the tmp directory.
- **Releasable**: closing D1-D2 dispatch gap; import/export e2e verifiable
- **Tests (TDD)** — `tests/integration/test_dispatch_scheduler_e2e.py`:
  - Integration: `test_export_job_reaches_done_with_archive_on_disk`
  - E2E: `test_import_job_reaches_done_and_restores_searchable_collection`
  - Integration: `test_user_job_precedes_backup_source_job`
  - Integration: `test_post_import_unsafe_tar_member_returns_422`
  - Checkpoint: `uv run pytest tests/integration/test_dispatch_scheduler_e2e.py -v`

#### Task 2.2 — Backup trigger and on-disk verification
- [x] **File**: `tests/integration/test_backup_dispatch_e2e.py`
- **Depends on**: Task 2.1
- **Description**:
  - `test_backup_trigger_queued_jobs_eventually_complete`: POST `/backup/trigger` against real app with real `SearchStore` and real `dispatch_fn`. **Note on timing**: Apply `monkeypatch.setattr(archon_search.jobs.backup_loop, '_BACKUP_COMPLETION_POLL_SECONDS', 0.1)` BEFORE calling `make_real_app(backup_enabled=True)` — if applied after, the completion loop may already be sleeping 60s before the monkeypatch takes effect. The completion loop sleeps `_BACKUP_COMPLETION_POLL_SECONDS` (default 60s) between drain cycles; reducing to 100ms allows the test to complete within its timeout. Poll `GET /jobs` (max 15s, 100ms interval) until all backup jobs reach `status == 'done'`. Assert `.tar.gz` files appear under the configured `output_dir`.
  - `test_backup_trigger_post_status_reflects_completion`: POST `/backup/trigger` via TestClient. **Note on timing**: Apply `monkeypatch.setattr(archon_search.jobs.backup_loop, '_BACKUP_COMPLETION_POLL_SECONDS', 0.1)` BEFORE calling `make_real_app(backup_enabled=True)` — if applied after, the completion loop may already be sleeping 60s before the monkeypatch takes effect. The completion loop sleeps `_BACKUP_COMPLETION_POLL_SECONDS` (default 60s) between drain cycles; reducing to 100ms allows the test to complete within its timeout. Poll GET `/status` (max 15s, 100ms interval) until `backup.last_backup_at` is non-null (accessed as `backup.collection_status[0].last_backup_at` — `last_backup_at` is per-collection, not top-level). Note: testing `archon-search backup now` via CliRunner is architecturally impossible here — the CLI uses httpx to call a real TCP server, but TestClient is ASGI transport only, not a TCP listener. HTTP endpoint testing is the correct approach.
- **Releasable**: closing D2-backup dispatch gap
- **Tests (TDD)** — `tests/integration/test_backup_dispatch_e2e.py`:
  - Integration: `test_backup_trigger_queued_jobs_eventually_complete`
  - Integration: `test_backup_trigger_post_status_reflects_completion`
  - Checkpoint: `uv run pytest tests/integration/test_backup_dispatch_e2e.py -v`

---

### Phase 3 — MCP Tool Error-Path Integration Tests
> **Releasable**: after Task 3.1; each test file independently runnable

#### Task 3.1 — MCP search/explain validation and typed-exception mapping
- [x] **File**: `tests/integration/test_mcp_error_paths.py`
- **Depends on**: nothing
- **Description**:
  - `test_mcp_search_both_collection_fields_returns_validation_error`: Call MCP `search` tool with both `collection` and `collections` set. Assert tool returns `is_error=True` with message referencing mutual exclusivity.
  - `test_mcp_search_missing_collection_returns_not_found_code`: Call `search` with a nonexistent collection. Assert `is_error=True` and error text contains `not found`.
  - `test_mcp_search_fanout_timeout_returns_timeout_code`: Monkeypatch `pipeline.search_many` to raise `FanoutTimeoutError`. Call MCP `search`. Assert `is_error=True` and `timeout` in the error dict.
  - `test_mcp_explain_rerank_false_multi_collections_returns_error`: Call MCP `explain` with `rerank=false` and two collections. Assert `is_error=True`.
  - `test_mcp_search_with_context_hyde_dependency_absent_returns_error`: Monkeypatch `anthropic` absent. Call MCP `search_with_context {hyde: true}`. Assert `is_error=True` with dependency message.
  - `test_mcp_search_rag_fusion_dependency_absent_returns_error`: Same for `rag_fusion=true`.
- **Releasable**: closing B3/C4/C5 MCP error-path gap
- **Tests (TDD)** — `tests/integration/test_mcp_error_paths.py`:
  - Integration: all 6 tests above
  - Checkpoint: `uv run pytest tests/integration/test_mcp_error_paths.py -v`

#### Task 3.2 — MCP schema contract after real ingest
- [x] **File**: `tests/integration/test_mcp_schema_contract.py`
- **Depends on**: nothing
- **Description**:
  - `test_mcp_search_real_pipeline_result_passes_pydantic_gate`: Real ingest via `pipeline.ingest_file`. Call `pipeline.search()`. Pass result through `McpSearchResultSchema.from_result()`. Assert no `ValidationError` and output dict has exactly the public contract fields.
  - `test_mcp_list_collections_real_pipeline_result_field_rename`: Real collection. Call `get_all_collections_meta()`. Pass through `CollectionListItemSchema`. Assert `embedding_model` present (not internal field names), no `centroid_sum_json` or other internal fields leak.
  - `test_mcp_search_with_context_excludes_transient_chunk_fields`: Real `search_with_context`. Pass result through `ContextChunkSchema.from_result()`. Assert no `vector`, `start_offset`, or `end_offset` in serialized output.
  - `test_e2e_mcp_search_tool_response_shape_after_real_ingest`: Real app via TestClient. Ingest. Call MCP `search` tool. Assert response dict exactly matches `McpSearchResponse` shape — no extra keys, no missing required keys.
- **Releasable**: closing C7 schema-contract gap
- **Tests (TDD)** — `tests/integration/test_mcp_schema_contract.py`:
  - Integration: `test_mcp_search_real_pipeline_result_passes_pydantic_gate`
  - Integration: `test_mcp_list_collections_real_pipeline_result_field_rename`
  - Integration: `test_mcp_search_with_context_excludes_transient_chunk_fields`
  - E2E: `test_e2e_mcp_search_tool_response_shape_after_real_ingest`
  - Checkpoint: `uv run pytest tests/integration/test_mcp_schema_contract.py -v`

---

### Phase 4 — CLI End-to-End Tests (Real Click Wiring)
> **Releasable**: after Task 4.1; each task independently runnable

#### Task 4.1 — Ingest path safety and container env e2e
- [x] **File**: `tests/integration/test_cli_e2e.py`
- **Depends on**: nothing
- **Description**:
  - `test_e2e_ingest_path_safety_full_flow`: POST `/ingest {path: '/foo/../bar'}` with valid auth via TestClient. Assert 400, detail starts with `'path is unsafe:'`, no job created (GET `/jobs` returns empty list).
  - `test_serve_command_with_real_app_responds_to_ready`: `load_config(serve=True)` with `DATA_DIR` set. Build `create_app(config)`. `TestClient(app)`. GET `/ready`. Assert 200. Verifies the `serve` entry point's host-default flip doesn't break routing.
  - `test_data_dir_env_routes_key_file_under_data_dir`: `create_app` lifespan with `ARCHON_SEARCH_DATA_DIR=<tmp_path>`. Assert key file created under `tmp_path`, not `~/.archon-search/`.
  - `test_container_stderr_handler_attached_when_env_set`: `ARCHON_SEARCH_CONTAINER=1`. Call `configure_logging(config)`. Assert `archon_search` logger has a `StreamHandler` targeting `sys.stderr`.
  - `test_ingest_unauth_takes_precedence_over_path_validation`: Dotted path POST `/ingest` without `Authorization` header. Assert 401 (auth middleware fires before path validation).
  - `test_wizard_dry_run_fresh_install_via_cli_runner`: `archon-search wizard --dry-run` via `CliRunner` on a clean `DATA_DIR`. Assert exit 0 and output contains `[dry-run]`.
  - `test_wizard_dry_run_idempotent_on_existing_install`: Run wizard twice with `--dry-run`. Assert exit 0 both times; no file mutations.
- **Releasable**: closing A5/C9/C14 CLI e2e gap
- **Tests (TDD)** — `tests/integration/test_cli_e2e.py`:
  - E2E: all 7 tests above
  - Checkpoint: `uv run pytest tests/integration/test_cli_e2e.py -v`

#### Task 4.2 — Wizard configurability e2e
- [x] **File**: `tests/integration/test_wizard_e2e.py`
- **Depends on**: nothing
- **Description**:
  - `test_wizard_db_path_not_writable_exits_nonzero`: Pass `--db-path /nonexistent/path/db`. Assert exit code non-zero and error message in output.
  - `test_wizard_non_interactive_hyde_accepted_writes_toml`: `archon-search wizard --non-interactive --enable-hyde` via `CliRunner`. Assert TOML file contains `[hyde] enabled = true`.
  - `test_wizard_non_interactive_hyde_declined_omits_toml_key`: `--non-interactive` without `--enable-hyde`. Assert `[hyde]` section absent from generated TOML.
  - `test_wizard_non_interactive_rag_fusion_accepted_writes_toml`: `--non-interactive --enable-rag-fusion`. Assert TOML contains `[rag_fusion] enabled = true`.
  - `test_wizard_summary_contains_next_steps_block`: Any successful wizard run. Assert output contains `Next steps` or equivalent onboarding section.
- **Releasable**: closing C15 wizard CLI e2e gap
- **Tests (TDD)** — `tests/integration/test_wizard_e2e.py`:
  - E2E: all 5 tests above
  - Checkpoint: `uv run pytest tests/integration/test_wizard_e2e.py -v`

---

### Phase 5 — Centroid and Routing Integration Tests
> **Releasable**: after Task 5.1 (correctness); after Task 5.2 (routing equivalence)

#### Task 5.1 — Incremental centroid correctness
- [x] **File**: `tests/integration/test_centroid_integration.py`
- **Depends on**: nothing
- **Description**:
  **All tests in this file must be defined as `async def`** — they call async pipeline and store methods (`pipeline.ingest_file`, `pipeline.delete_document`, `store.get_collection_meta`, `recompute_collection_meta`). Since `pyproject.toml` sets `asyncio_mode = 'auto'`, no `@pytest.mark.asyncio` decorator is needed — just define as `async def test_...`.
  - `test_multi_batch_ingest_centroid_correctness`: Two real ingest batches on the same collection. Assert `centroid_sum / chunk_count` equals the arithmetic mean of all chunk vectors (tolerance 1e-5). Verifies `_do_update_meta_on_add` accumulation.
  - `test_reingest_changed_document_net_zero`: Ingest doc_id A (batch 1). Re-ingest same doc_id A with different text (batch 2). Assert final centroid equals the centroid of batch-2 vectors only (batch-1 contribution subtracted).
  - `test_delete_then_verify_centroid`: Ingest doc X and doc Y. Delete doc X via `pipeline.delete_document`. Assert centroid equals doc Y's vectors only.
  - `test_drift_guard`: Ingest 10 docs in 5 incremental batches. Assert `store.get_collection_meta().centroid_sum` matches `recompute_collection_meta(force=True)` output within 1e-5. Catches accumulated floating-point drift.
  - `test_concurrent_ingest_and_delete_serializes_correctly`: Deterministic serialization test — do NOT use `asyncio.gather` with non-deterministic lock acquisition order. Instead: (1) Ingest batch_1 with 3 docs. Record centroid C1. (2) Use a `threading.Event` (NOT `asyncio.Event` — asyncio.Event is bound to a single event loop and cannot be signaled from the test thread) monkeypatch to make ingest batch_2 hold the `_lock_for` lock until explicitly released. Monkeypatch the ingest coroutine to call `await asyncio.to_thread(hold_event.wait)` after acquiring the lock — this blocks in a thread pool without blocking the event loop. Start batch_2 as an `asyncio.create_task`. (3) Wait briefly (e.g., `await asyncio.sleep(0.05)`) for batch_2 to acquire the lock and start blocking. (4) Start `delete_document` for a batch_1 doc as a second `asyncio.create_task` (this will queue waiting for the lock). (5) Release the lock by calling `hold_event.set()` from the test. (6) `await asyncio.gather(batch_2_task, delete_task)` to let both complete. (7) Assert final centroid equals force-recomputed centroid (within 1e-5). Verifies `_lock_for` serialization without deadlock. Note: this test requires `pytest-asyncio` or running inside an async test function to use `asyncio.create_task` and `asyncio.gather`.
  - `test_pre_b5_collection_seeds_on_first_ingest`: Create a meta row with empty `centroid_sum_json`. Ingest one batch. Assert `centroid_sum_json` populated and equals batch centroid. Verifies migration path for pre-B5 collections.
- **Releasable**: closing B5 centroid correctness gap
- **Tests (TDD)** — `tests/integration/test_centroid_integration.py`:
  - Integration: all 6 tests above
  - Checkpoint: `uv run pytest tests/integration/test_centroid_integration.py -v`

#### Task 5.2 — Routing integration and e2e
- [x] **File**: `tests/integration/test_routing_integration.py`
- **Depends on**: Task 5.1
- **Description**:
  - `test_incremental_vs_recomputed_routing_equivalence`: Three real collections with distinct corpora. Run 10 queries. Assert `MultiCollectionRouter.rank()` top-K results are identical whether centroids were maintained incrementally or force-recomputed. Verifies routing correctness after B5.
  - `test_hybrid_routing_end_to_end_ingest_persist_fetch_rank`: Ingest two corpora with distinct description_embeddings. `recompute_collection_meta`. Instantiate `MultiCollectionRouter(strategy='hybrid')`. Assert query aligned with corpus B's description ranks B first.
  - `test_e2e_delete_updates_routing_centroid`: Ingest 2 docs → DELETE via HTTP → GET `/route`. Assert routing centroid no longer includes deleted doc's vectors (verified by checking `centroid_sum` via `/status` or direct store read).
  - `test_e2e_incremental_centroid_survives_reconnect`: Ingest batch 1. Disconnect and reconnect `SearchStore`. Ingest batch 2. Assert centroid equals mean of all batches (not just batch 2).
- **Releasable**: closing B4/B5 routing integration gap
- **Tests (TDD)** — `tests/integration/test_routing_integration.py`:
  - Integration: `test_incremental_vs_recomputed_routing_equivalence`
  - Integration: `test_hybrid_routing_end_to_end_ingest_persist_fetch_rank`
  - E2E: `test_e2e_delete_updates_routing_centroid`
  - E2E: `test_e2e_incremental_centroid_survives_reconnect`
  - Checkpoint: `uv run pytest tests/integration/test_routing_integration.py -v`

---

### Phase 6 — Feature-Specific Integration Tests
> **Releasable**: after each task independently

#### Task 6.1 — FTS delete with no phantom hits
- [x] **File**: `tests/integration/test_fts_delete_no_phantom.py`
- **Depends on**: nothing
- **Description**:
  - `test_ingest_directory_optimize_fts_called_not_rebuild_when_index_exists`: Real FTS-indexed collection. Call `pipeline.ingest_directory`. Assert `store.optimize_fts` called (monkeypatch spy), `store.rebuild_fts_index` NOT called. Verifies C6 O(delta) path.
  - `test_mcp_delete_document_no_phantom_hits_in_subsequent_search`: MCP `delete_document` on a real FTS-indexed store. Call `store.hybrid_search` directly for the deleted text. Assert zero results. Verifies `optimize_fts` after delete actually removes entries.
  - `test_e2e_delete_document_via_mcp_no_phantom_hits`: Full flow — POST `/ingest` → verify in `/search` → MCP `delete_document` → `/search` returns zero results for deleted text.
  - `test_e2e_reingest_via_ingest_file_old_content_absent`: POST `/ingest` file with text A. Modify file to text B. POST `/ingest` again. POST `/search text=A` → zero results. POST `/search text=B` → found. Verifies incremental FTS correctly removes stale entries.
- **Releasable**: closing C6 FTS phantom gap
- **Tests (TDD)** — `tests/integration/test_fts_delete_no_phantom.py`:
  - Integration: `test_ingest_directory_optimize_fts_called_not_rebuild_when_index_exists`
  - Integration: `test_mcp_delete_document_no_phantom_hits_in_subsequent_search`
  - E2E: `test_e2e_delete_document_via_mcp_no_phantom_hits`
  - E2E: `test_e2e_reingest_via_ingest_file_old_content_absent`
  - Checkpoint: `uv run pytest tests/integration/test_fts_delete_no_phantom.py -v`

#### Task 6.2 — Container env and disk I/O integration
- [x] **File**: `tests/integration/test_container_env_integration.py`
- **Depends on**: nothing
- **Description**:
  - `test_data_dir_env_routes_log_file_to_derived_path`: `load_config()` + `configure_logging()` in sequence with `DATA_DIR=<tmp_path>`. Assert file handler path is under `tmp_path`, not `~/.archon-search/`.
  - `test_container_env_and_data_dir_together_in_real_app`: `ARCHON_SEARCH_CONTAINER=1` + `DATA_DIR=<tmp_path>`. Create real app. Assert: db path under `tmp_path`, log file under `tmp_path`, stderr handler attached to `archon_search` logger.
  - `test_atomic_write_json_roundtrip_real_disk`: Write dict to a real `tmp_path` file via `atomic_write_json`. Read back via `json.loads(path.read_text())`. Assert round-trips correctly. No mock; verifies actual disk I/O.
  - `test_job_store_survives_json_roundtrip`: Enqueue a job via `JobStore` with a real `tmp_path`. Instantiate a new `JobStore` pointing at same path. Assert job is retrievable and fields match. Verifies A7 fsync contract end-to-end.
  - `test_key_file_created_with_mode_600_on_first_start`: Create real app with fresh `tmp_path` (no existing key file). After lifespan startup (use TestClient context manager), assert key file exists under `tmp_path` with `os.stat().st_mode & 0o777 == 0o600`. Verifies key-manager bootstrap security invariant.
- **Releasable**: closing A7/C9 disk I/O gap; key-file security bootstrap
- **Tests (TDD)** — `tests/integration/test_container_env_integration.py`:
  - Integration: all 5 tests above
  - Checkpoint: `uv run pytest tests/integration/test_container_env_integration.py -v`

#### Task 6.3 — Health/status and observability integration
- [x] **File**: `tests/integration/test_observability_integration.py`
- **Depends on**: nothing
- **Description**:
  - `test_get_status_storage_connected_with_real_store`: Create real app (real `SearchStore`, real LanceDB). GET `/status`. Assert `readiness.storage_connected == true` (not mocked ping). Verifies B2 health contract.
  - `test_correlation_id_appears_in_log_jsonl`: Middleware assigns `X-Request-Id`. Set `config.log_format = 'json'` and `config.log_file = str(tmp_path / 'archon.log')` before building the app — the default `log_format='text'` does not render `correlation_id` in the log output. After making a real request, read `tmp_path / 'archon.log'`, parse each line as JSON, and assert at least one record has `correlation_id` matching the `X-Request-Id` response header. Verifies B7 structured log wiring.
  - `test_explain_stage_timings_ms_in_response_body`: Real ingest + POST `/explain`. Set `config.observability.stage_timings_enabled = True` explicitly before building the app to guarantee timings are recorded — do not rely on the default (the explain route's `getattr` fallback is `False` if the observability attribute is missing). Assert `stage_timings_ms` is not `None` before checking keys. Assert response body contains `stage_timings_ms` dict with `embed`, `vector`, `fts` keys. Note: stage timings are in the `/explain` response body as `stage_timings_ms`, NOT as an `X-Stage-Timings` HTTP header on `/search` (which does not exist). Verifies B1 stage timing wiring end-to-end.
  - `test_explain_explicit_single_collection_returns_200_with_context`: Ingest into a named collection. POST `/explain {collection: '<name>', query: '...'}` (note: `collection` singular, not `collections`). Assert 200 and response body has `context_chunks` non-empty and `stage_timings_ms` non-null. Verifies the explicit single-collection path at `routes_explain.py:460-470`, distinct from the routing-based path.
  - `test_lifespan_telemetry_drains_on_app_shutdown_with_real_writer`: `create_app` with real `TelemetryWriter` writing to `tmp_path`. Trigger a search. Shut down app (exit lifespan). Assert JSONL file contains the search record. Verifies A3 lifespan drain.
  - `test_telemetry_entry_does_not_contain_raw_query_string`: Enable telemetry in real app config by setting `config.telemetry.enabled = True` and `config.telemetry.log_dir = str(tmp_path / 'search-logs')` before building the app (do NOT use the default global path — that would write to the real user's `~/.archon-search/`). POST `/search` with `query='secret_test_query_string'`. Assert the JSONL telemetry file under `tmp_path / 'search-logs'` exists AND is non-empty (otherwise the test passes vacuously). Then assert no line in the file contains the literal string `secret_test_query_string`. Verifies the structural no-raw-query invariant at the HTTP wiring level.
- **Releasable**: closing A3/B1/B2/B7 observability gap; telemetry privacy invariant verified at wiring level
- **Tests (TDD)** — `tests/integration/test_observability_integration.py`:
  - Integration: `test_get_status_storage_connected_with_real_store`
  - Integration: `test_correlation_id_appears_in_log_jsonl`
  - Integration: `test_explain_stage_timings_ms_in_response_body`
  - Integration: `test_explain_explicit_single_collection_returns_200_with_context`
  - Integration: `test_lifespan_telemetry_drains_on_app_shutdown_with_real_writer`
  - Integration: `test_telemetry_entry_does_not_contain_raw_query_string`
  - Checkpoint: `uv run pytest tests/integration/test_observability_integration.py -v`

#### Task 6.4 — ACL and namespace isolation integration tests
- [x] **File**: `tests/integration/test_acl_namespace_integration.py`
- **Depends on**: nothing
- **Description**:
  **Multi-namespace setup**: These tests require the app to be configured with two distinct API keys mapped to two namespace names. Use `make_real_app` with a custom config override: `config.namespaces = {"key_hex_a": "ns-a", "key_hex_b": "ns-b"}` (use `secrets.token_hex(32)` for each key in tests). Requests for namespace-A use `Authorization: Bearer key_hex_a`; namespace-B use `Authorization: Bearer key_hex_b`. Note: ACL (`acl.py`) is chunk-level access control within a namespace (different from namespace-level isolation). The first two tests verify namespace isolation (collection visibility scoped by bearer token); the third tests ACL field persistence through export/import (data integrity, not enforcement). A test for ACL enforcement post-import (namespace-B cannot see ACL-restricted chunks after import) should be added in a follow-up ACL plan.
  - `test_acl_restricted_collection_not_visible_in_cross_namespace_http_search`: Ingest docs into a namespace-A collection. Make HTTP search request scoped to namespace-B. Assert zero results from namespace-A's data. Verifies ACL filtering is enforced through the HTTP layer and a broken ACL filter cannot cause cross-namespace data exposure.
  - `test_acl_field_survives_export_import_round_trip`: Ingest docs where the ACL field is explicitly set via chunk-level metadata (e.g., by directly inserting rows with `acl='ns-a'` into the store via `store.ingest_chunks`, or by using the ingest API with a document that has ACL-compatible front-matter, depending on how `acl.py` assigns ACL during ingest). Export the collection. Import into a new collection. POST `/search`. Assert `acl` field on results equals the original value. Note: if the ingest API does not support setting ACL during ingest, use `store.ingest_chunks` directly with explicit `acl` values. Verifies ACL metadata survives the export/import pipeline.
  - `test_namespace_a_cannot_list_namespace_b_collections`: GET `/collections` scoped to namespace-A auth. Assert namespace-B collections are absent from response. Verifies namespace isolation on the collection list endpoint.
- **Releasable**: closing security gap for ACL/namespace isolation — a broken ACL filter causes data exposure
- **Tests (TDD)** — `tests/integration/test_acl_namespace_integration.py`:
  - Integration: `test_acl_restricted_collection_not_visible_in_cross_namespace_http_search`
  - Integration: `test_acl_field_survives_export_import_round_trip`
  - Integration: `test_namespace_a_cannot_list_namespace_b_collections`
  - Checkpoint: `uv run pytest tests/integration/test_acl_namespace_integration.py -v`

#### Task 6.5 — Collection lifecycle integration tests
- [x] **File**: `tests/integration/test_collection_lifecycle_integration.py`
- **Depends on**: nothing
- **Description**:
  - `test_delete_collection_drops_table_and_removes_meta`: Ingest into a real collection. DELETE `/collections/{name}` via TestClient. Assert 200. GET `/collections` — name absent. Assert LanceDB table no longer exists: check BOTH `store.list_collections()` (verifies store internal tracking) AND that the LanceDB directory under `tmp_path` for the collection is absent (verifies actual disk cleanup). Checking only one is insufficient — `drop_collection` could fail silently while meta is deleted.
  - **Multi-namespace setup for `test_delete_collection_wrong_namespace_returns_403_or_404`**: Requires `config.namespaces = {'key_hex_a': 'ns-a', 'key_hex_b': 'ns-b'}` (see Task 6.4 setup). Use `key_hex_a` Bearer token to create the collection in ns-a, then use `key_hex_b` token for the DELETE attempt. Expected status is 403 or 404 (namespace ownership check at `routes_collections.py:273`).
  - `test_delete_collection_wrong_namespace_returns_403_or_404`: Create a collection in namespace-A. Attempt DELETE from a namespace-B token. Assert 403 or 404 (namespace ownership check).
  - `test_delete_pinned_only_collection_returns_error`: Ingest at least one document into the collection first (so the LanceDB table and meta row exist). Then configure: path in `config.pinned_collections` but NOT in `config.collections`. Attempt DELETE `/collections/{name}`. Assert 409 (the route checks collection existence first via `routes_collections.py:272-275`, then checks pinned-only status at lines 286-294).
- **Releasable**: closing collection deletion integration gap
- **Tests (TDD)** — `tests/integration/test_collection_lifecycle_integration.py`:
  - Integration: `test_delete_collection_drops_table_and_removes_meta`
  - Integration: `test_delete_collection_wrong_namespace_returns_403_or_404`
  - Integration: `test_delete_pinned_only_collection_returns_error`
  - Checkpoint: `uv run pytest tests/integration/test_collection_lifecycle_integration.py -v`

---

### Final Phase — Verification & Documentation

#### Task 7.1 — Final verification & documentation update
- [x] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Run full test suite and assert zero failures: `uv run pytest --no-cov` (verifies zero failures without slow coverage overhead). Then run: `uv run pytest` (without `--no-cov`) to verify the coverage gate (`--cov-fail-under=85`) still passes with the new tests included.
  - Spawn an agent to update `Documentation/Architecture/200_testing_strategy.md` to document the integration test directory structure and the cross-cutting gap themes this plan addressed.
  - Spawn an agent to update `CLAUDE.md` repository conventions section to note that `tests/integration/` contains multi-component integration and e2e tests, distinct from unit tests.
  - Spawn an agent to update `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` if any new fixtures or helpers were introduced.
- **Releasable**: after this task the full integration/e2e gap is closed and documentation reflects it.
- **Acceptance criteria** (must all pass):
  - `uv run pytest --no-cov` passes with 0 failures
  - `uv run pytest` (with coverage) passes the `--cov-fail-under=85` gate
  - Every test added in phases 1–6 is listed in `tests/integration/` and runnable with its checkpoint command
  - The 6 cross-cutting themes identified in Background are each covered by at least 2 new tests
  - No existing test is modified or deleted
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: Run both commands above. All criteria checked.
