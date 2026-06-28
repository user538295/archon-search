# Learnings

## What Has Failed

**[2026-06-28] — Roadmap expansion: agents hitting session limit make zero edits — respawn with identical prompt**
- Observation: Agent spawned to write Phase G to the roadmap hit the user's session token limit before making any edits. Zero changes landed. Respawning with the identical detailed prompt on the next session completed the task fully in one shot.
- Action: When an agent returns with "session limit" and zero tool uses, respawn immediately with the same prompt — no simplification needed. The work was well-specified; the only failure was wall-clock timing.
- Confidence: high

**[2026-06-28] — Competitive analysis update: agents need full Phase A-E context, not just recent weeks**
- Observation: When told "three recent weeks," the agent prompt omitted Phase A/B features (hybrid routing B4, server-side multi-collection B3, explain endpoint A4, metadata filters A2) that were shipped months ago but still absent from the comparison doc.
- Action: When briefing agents on competitive analysis updates, scope the task as "all phases since the document's last-reviewed date" and verify which roadmap phases are fully checked off. A git log range like `git log --since=<last-reviewed>` is more reliable than a wall-clock window.
- Confidence: high



**implement-all T-1: subagent stops after review without committing or checking off plan**
- Action: Subagent prompt must require `grep "\- \[x\]" <plan>` and `git log --oneline -1` as proof before declaring done. Stated intent is not execution.

## What Has Worked

**[2026-06-28] — E0e T-1: excluded_collections is list[dict], not list[str]**
- Observation: `assert col_b not in excluded_collections` where `excluded_collections` is `list[dict]` always returns True. String membership in list[dict] never matches.
- Action: Always assert `excluded == []` for zero-result leg assertions. Never use `name not in list_of_dicts`.
- Confidence: high

**[2026-06-28] — E0e T-1: tester-role e2e tests for language filter must use language filter (not file_type)**
- Observation: S10 regression scenario specified `language: "en"` but the test was written with `file_type: ".md"`. The language filter is the one whose restriction was lifted by E0e; using a different filter doesn't guard the correct regression.
- Action: Match the exact filter type stated in the scenario. Language stub returns `language=""` so results are empty, but 200 still proves the path wasn't broken.
- Confidence: high

**[2026-06-28] — E0e T-1: coverage illusion from missing result assertion**
- Observation: An S2 test that asserts only `status=200` and `applied_filters echo` but never checks `data["results"]` passes whether the filter works or is silently ignored. Reviewers flagged this as a coverage illusion.
- Action: Always add `assert data["results"] == []` when the expected result is empty, with a comment explaining why. This eliminates ambiguity between "filter returns empty" and "filter silently dropped".
- Confidence: high

### Testing patterns

**asyncio.run() not get_event_loop().run_until_complete() in xdist-parallel tests**
- Action: Always use `asyncio.run(coroutine)` in integration tests. `get_event_loop().run_until_complete()` raises RuntimeError in xdist workers after another test clears the loop.

**Use FastAPI Query(ge=, le=) not manual if-check for pagination limits**
- Action: Declare `limit: int = Query(default=50, ge=1, le=200)`. Manual if-check produces wrong 422 shape and missing OpenAPI constraints. See `routes_jobs.py:421` as canonical sibling.

**math.ceil for pagination page count; non-multiple doc count**
- Action: Use `n_docs` that is NOT a multiple of `page_size` so partial-last-page is exercised. Use `math.ceil(n_docs / page_size)` for expected page count.

**Cursor pagination tests need both deleted-cursor and cursor-past-all-docs cases**
- Action: For S4 spec, include (a) deleted cursor where docs exist after it, and (b) cursor past all docs (`"z" * 64`) returning empty items + null next_cursor.

**"not 422" assertions are weaker than specific downstream codes**
- Action: For at-limit boundary tests with non-existent resources, assert `== 404` (proves request passed validation AND reached collection lookup), not `!= 422`.

**Exit code assertions need a unique string to pin the code path**
- Action: When two code paths share an exit code, add `assert "specific string" in result.stderr`. Exit code alone is insufficient.

**Assert directly on result.stderr, not combined stdout+stderr**
- Action: Never concatenate `result.output + result.stderr`. Assert on `result.stderr` directly. In Click 8.3.3, default `CliRunner()` already separates streams — never pass `mix_stderr=False`.

**Defensive `or 0` on optional fields needs two tests: absent key AND null value**
- Action: `get(key, default) or fallback` has two branches. Add one test for absent key and one for null value — they are distinct code paths.

**Place assertions inside `with patch(...)` when using synchronous TestClient**
- Action: Always put HTTP call AND assertions inside the `with patch(...)` block. Outside is fragile to async refactors.

**`type(j) is IngestJob` predicates need a negative-case test with a subclass**
- Action: Seed an `ExportJob` with the target status and assert it is NOT counted. Without this, replacing exact-type check with `isinstance` silently passes.

**Namespace isolation tests must be two-sided**
- Action: Assert BOTH namespaces return their own distinct values. One-sided assertions cannot detect constant-return bugs.

**`bool | None` fields: use `is True` and test all three states**
- Action: `entry.field is True` correctly excludes both `None` and `False`. Always test True/False/None explicitly — reviewers flag two-state suites as gaps.

**caplog must target the specific logger for lifespan startup logging**
- Action: Use `caplog.at_level(logging.WARNING, logger="archon_search.telemetry.hasher")` wrapping the `make_real_app` context. Assert on `r.getMessage()` content, not just `r.levelno`.

**Index slicing, not `[-1]`, for isolating entries from a second session**
- Action: Use `entries[before_count]` to isolate entries written by a second app session sharing the same log dir. `[-1]` picks the last overall entry, not the first one from session 2.

**`os.getuid()` is POSIX-only — use `getattr` form in skipif**
- Action: `@pytest.mark.skipif(getattr(os, "getuid", lambda: -1)() == 0, ...)`. Bare `os.getuid()` crashes test collection on Windows.

**`if result_doc_ids:` guard in assertions is a vacuous-pass trap**
- Action: Replace with `assert result_doc_ids`. A zero-result scenario silently satisfies the `if` guard while proving nothing about correctness.

**FAILED_EXPIRED must be checked alongside FAILED in every polling/terminal-status path**
- Action: `_TERMINAL_STATUSES` has 5 definitions (store.py, routes_jobs.py, backup_cmd.py, export_cmd.py, collection.py). When adding a new terminal status, grep and update all five. In polling loops, use `status in {"FAILED", "FAILED_EXPIRED"}`.

**FAILED+timeout race in multi-job polling loops**
- Action: Check accumulated failure list before deciding to `exit 0` in the timeout branch. Confirmed failure must win over timeout.

**Dead module-level constants must be deleted, not commented as legacy**
- Action: When replacing a constant with a parameter-derived value, delete it and update all test patches to use the actual controlling input (e.g., `--timeout N` CLI arg).

**Pre-seeding JobStore before `make_real_app` via the same file path**
- Action: Create `JobStore(path=tmp_path / "jobs.json")` before entering `make_real_app`, seed it — `make_real_app` reads the same file on init via `_load()`. No need to expose the store.

**`pytest.mark.integration` as bare expression is dead code**
- Action: Use `@pytest.mark.integration` as a decorator. A bare expression statement inside a function body does nothing. Verify with `uv run pytest -m integration <file> -n0 -v --no-cov`.

**Hint-line count assertions need full surrounding phrase, not just a digit**
- Action: Never assert `str(N) in result.output`. Assert the full specific substring (e.g., `"2 revoked key(s) hidden" in result.output`).

**`JobStore.transition()` not `update()` for state transitions in batch loops**
- Action: `transition()` returns `None` on eviction/already-changed instead of raising KeyError. Using `update()` in a batch loop aborts all remaining jobs on race conditions.

**Exception-swallowing change to re-raise breaks existing tests — grep first**
- Action: Before changing exception-swallowing to re-raise, grep for tests calling the function without `pytest.raises`. Run that file's tests before the full suite.

**`asyncio.TimeoutError` IS a subclass of `Exception` in Python 3.12**
- Action: `except asyncio.TimeoutError` MUST come before `except Exception`. Verify MRO with `python3 -c "print(issubclass(asyncio.TimeoutError, Exception))"` when unsure.

**`assert` in production code is stripped by `python -O`**
- Action: For data-integrity postconditions, use `if ... != ...: raise RuntimeError("BUG: ...")`. Never use `assert` for invariants that should hold in production.

**Exception message leakage via `f"...: {exc}"` in MCP/API boundaries**
- Action: Never interpolate `{exc}` into `internal_error` responses. Log internally; use a fixed string externally.

**`or fallback` is wrong for falsy-valid attribute values**
- Action: `getattr(..., "api_key", None) or self._api_key` silently falls back when the value is `""`. Always use explicit `is not None` guard.

**`None == None` is True — guard nullable-id lookups with explicit type check**
- Action: Synthetic TOML records have `id=None`. Add `if not isinstance(key_id, str): raise KeyError(...)` at entry to any method matching against a nullable entity field.

**[2026-06-27] — Test stub migration for store method rename**
- Observation: When a store method is renamed (e.g., `hybrid_search` → `hybrid_search_with_trace`) and return type changes, all stubs must update both method name AND returned object type. ACL tests that built `SearchResult` inline from `ChunkRecord` need a `_chunk_to_candidate` helper for the new `ScoredSearchCandidate` type.
- Observation: When the reranker method changes from `rerank` to `rerank_candidates`, `MagicMock` stubs set via `reranker.rerank = fn` silently succeed but the pipeline never calls that attribute — tests pass vacuously. Always verify the method name with `grep -n "self._reranker\." archon_search/pipeline.py` first.
- Observation: Never use `git stash` as a baseline-test mechanism mid-session. A failed `git stash pop` (conflict) silently reverts files, requiring all edits to be redone.
- Action: Before writing any stub, grep the production code to confirm exact method names called.
- Confidence: high

**[2026-06-27] — `#manual_test` tasks must not be automated by /implement-next**
- Observation: `/implement-next` on a `#manual_test` task triggered a PDF size calibration loop (~10 script attempts), hit a pre-existing stub mismatch during benchmark verification, and spawned two unrelated fix agents (~70K output tokens). Total session cost: ~206K output tokens, 96M cache reads for a task estimated at 1.5h.
- Action: When the plan task contains `#manual_test`, the implementing agent must NOT run the test or generate synthetic test data. It should write a checklist document and mark the task done — or the plan should be downscoped to a `#integration_test` before the agent touches it.
- Confidence: high

**[2026-06-27] — Session-scoped fixtures run before function-scoped autouse; close the gap with a session-scoped env clear**
- Observation: The function-scoped autouse `_archon_isolated_data_dir` clears `ANTHROPIC_API_KEY` before each test body, but session-scoped fixtures run before any function-scoped fixture. A session fixture that calls `ingest_directory` would see the key live. Added `_block_anthropic_key_at_session` (session-scoped autouse, `os.environ.pop`) to clear it before any session fixture can fire.
- Observation: `anthropic` is an optional extra (`hyde`/`rag_fusion` deps). Session-level `patch("anthropic.Anthropic", ...)` by string must be replaced with `patch.object(imported_mod, "Anthropic", ...)` and guarded by `try: import anthropic except ImportError: yield; return`. Tests that prove the mock must use `pytest.importorskip("anthropic")` so they skip gracefully when the extra is absent.
- Action: When adding session-level env-var protection, always pair it with a session-scoped fixture to prevent the session-fixture timing gap. For optional-dep mocks, use `patch.object` with an ImportError guard.
- Confidence: high

### Config and schema

**Adding a `SearchConfig` field requires four coupled updates**
- Action: (1) config.py dataclass + field + `_apply_toml` block; (2) `test_config_defaults.py` snapshot; (3) `tests/path_home_allowlist.txt` line number (check with `grep -n "Path.home" archon_search/config.py`); (4) config.py `_coerce_bool` block. All in one commit.

**Adding/removing a Pydantic response field breaks OpenAPI snapshot — regen in the same commit**
- Action: Run `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot` in the same commit. CI is 3.12; local 3.13 differs on 422 descriptions. `tests/contract/openapi_snapshot.json` has NO test — only `tests/server/openapi_snapshot.json` is the CI guard.

**Moving Pydantic Field bounds to handler body changes 422 shape — document in BREAKING.md**
- Action: Removing `le=100` from `SearchRequest.top_k` and moving the check to handler body changes the 422 envelope from Pydantic array to plain string. This is a wire-level breaking change; add to BREAKING.md.

**Pydantic `required-and-nullable` field: `str | None = Field(...)` without `default=None`**
- Action: `Field(default=None)` makes the field optional in OpenAPI. For required-and-nullable as in a C3 contract, omit `default`. For camelCase JSON keys, use `serialization_alias` + `validation_alias` + `serialize_by_alias=True` on the model config.

**Default value changes have a blast radius across 5+ files**
- Action: Grep for the old value across: (1) `tests/test_config.py`, (2) `test_config_defaults.py`, (3) `*.toml.example`, (4) `Documentation/UserManual/`, (5) architecture docs. Per-field test assertions in test_config.py are not covered by the snapshot test alone.

**camelCase JSON field is the first in schemas.py — use alias machinery**
- Action: `model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)` + `Field(serialization_alias="bindAddress", validation_alias=AliasChoices("bindAddress", "bind_address"))`.

### FastMCP / MCP wiring

**FastMCP lifespan delegation requires explicit `router.lifespan_context`**
- Action: `app.mount('/mcp', mcp_starlette)` without `async with mcp_starlette.router.lifespan_context(app): yield` causes `RuntimeError: Task group is not initialized` on every MCP request. Call `app.mount()` INSIDE the `async with` block — Starlette has no `app.unmount()`.

**MCP SSE requires `Accept` header and `data:` line parsing**
- Action: Include `"Accept": "application/json, text/event-stream"`. Parse response by splitting on `"data: "` prefix, not `.json()`. Also requires `notifications/initialized` after `initialize` before any tool call.

**`notifications/initialized` status — assert `in (200, 202, 204)`**
- Action: FastMCP accepts a range for fire-and-forget notifications. Never assert `== 202`.

**Wire new lifespan closure param through full MCP chain**
- Action: `app.state.<param>` → `create_mcp_http_app(<param>=...)` → `create_app(<param>=...)` → tool closure capture. Missing any link silently falls back to `None`. Mirrors the `writer` threading pattern.

**Namespace gate in MCP search tools breaks tests with `get_collection_meta=None`**
- Action: Any helper that sets `get_collection_meta = AsyncMock(return_value=None)` for non-access purposes must be changed to `return_value=MagicMock()` after adding a namespace gate.

**`ContextVar.get` is read-only in C — patch the Python-level wrapper**
- Action: Never `patch.object(module._current_http_request, "get", ...)`. Patch `"archon_search.server.mcp._get_request_namespace"` instead.

**Use `fastmcp.server.dependencies.get_http_request()` (public API)**
- Action: Never import `_current_http_request` from `fastmcp.server.http`. Use `from fastmcp.server.dependencies import get_http_request` with `try/except RuntimeError`.

**fastmcp stub contamination — lazy import in mcp.py**
- Action: Move `fastmcp.server.dependencies` imports inside function body with `try/except ImportError`. Module-level imports fail in workers that stub `fastmcp` as a bare `ModuleType`.

**2026-06-27 — Bug fix: `_search_standard` called `hybrid_search` (→ SearchResult) instead of `hybrid_search_with_trace` (→ ScoredSearchCandidate)**
- Observation: `_search_standard` passed `SearchResult` objects to `_candidate_to_search_result` which expects `ScoredSearchCandidate`. The bug was latent because stubs returned empty lists. The reranker path also called `reranker.rerank()` (SearchResult) not `reranker.rerank_candidates()` (ScoredSearchCandidate).
- Action: (1) Change `_search_standard` to call `hybrid_search_with_trace` with `candidate_depth=`. (2) Change reranker call to `rerank_candidates`. (3) Apply `source_path_glob` post-filter in `_search_standard` (not done by `_hybrid_search_with_trace`). (4) Update all test stubs to use `hybrid_search_with_trace` returning `ScoredSearchCandidate`. (5) Add `rerank_candidates` to mock rerankers.
- Confidence: high

**`app.user_middleware` to inspect FastMCP middleware (not `.middleware`)**
- Action: `StarletteWithLifespan` does NOT expose `.middleware`. Use `app.user_middleware` — a list of `Middleware` namedtuples with `.cls` and `.kwargs`.

**`create_mcp_http_app()` needs `ARCHON_SEARCH_DATA_DIR` redirected even with `config=None`**
- Action: `create_mcp_http_app(config=None)` still calls `load_or_generate_key()`. Always `monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))`.

**`delete_document` caller-controlled namespace is a cross-tenant bypass**
- Action: Validate caller-supplied `namespace` against `_get_request_namespace()`. Mismatch → `code="forbidden"`. The authenticated namespace is always authoritative.

**MCP search telemetry test must ingest a real collection first**
- Action: Empty store causes a 404 error-path telemetry entry. Ingest via `ingest_file_via_path` before MCP call; assert `status == "ok"` to pin the success path.

**`make_real_app` must set `cfg.telemetry.log_dir` to `tmp_path/search-logs`**
- Action: Default is `~/.archon-search/search-logs`. Tests writing to a manually constructed `tmp_path / "search-logs"` but checking the default path are vacuous. Always check `Path(_cfg.telemetry.log_dir)`.

**Namespace gate isolation is metadata-gate-level, not chunk-level**
- Action: Document in test docstrings. Do not add chunk-level filtering unless the security model explicitly requires defense-in-depth.

**namespace validation regex rejects underscores at start/end**
- Action: The regex `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` rejects `"__sentinel__"`. Use `"wrong-sentinel-xyz"` — always use valid namespace-format strings even as sentinels.

### Architecture and planning

**Verify function names against codebase before writing plans**
- Action: Grep before naming any function in a plan. For factory/constructor wiring, identify ALL construction sites — CLI and server use different paths in this codebase.

**Return type changes require full call-chain enumeration**
- Action: Grep ALL callers of a function; trace through intermediate functions. Check every branch (e.g., early-return paths in `resolve_acl` that bypass `read_acl_sidecar`).

**Route-level vs pipeline-level seam determines where failure signals can live**
- Action: Before putting a failure signal on a domain result type, verify WHERE the failing operation runs. HyDE runs at route level, before `pipeline.search()` — it cannot populate a pipeline-level field.

**Dual-guard predicates require a shared helper — two independent checks will drift**
- Action: Extract shared `_file_exceeds_limit(path, max_file_mb) -> bool` when the same predicate is evaluated in two places. Don't defer "we'll keep them in sync manually."

**`list[str] | None` sentinel distinguishes test-seam bypass path from dispatch path**
- Action: Initialize `ingest_warnings: list[str] | None = None`. Only the dispatch path sets a real list. Guard `store.update()` on `is not None` to avoid corrupting test-seam results.

**Status sub-objects must reflect ACTUAL state, not config intent**
- Action: Derive `bindAddress` from `app.state.mcp_bound` (set True only after successful `app.mount()`), not from `config.mcp.enabled`. Initialize `app.state.mcp_bound = False` unconditionally BEFORE the conditional block.

**`hmac.digest(key, msg, "sha256").hex()` over `hmac.new(...).hexdigest()`**
- Action: One-shot C-level implementation (Python 3.7+, guaranteed on 3.12). Also removes `hashlib` import.

**`functools.partial` over closure factory for single-argument adaptation**
- Action: `functools.partial(hash_doc_id, salt)` replaces a closure factory — fully typed, 4 fewer lines, no `noqa` suppression needed.

**TypeSpec `@doc` on response body does not set OpenAPI response description — need a named model**
- Action: `@doc` placed on a `@body body: ErrorDetail` field inside an anonymous inline union branch sets the schema description, not the HTTP response description. To get a meaningful description on a specific status code in OpenAPI, extract it into a named model with `@doc` on the model itself: `@doc("...") model FileTooLargeResponse { @statusCode statusCode: 413; @body body: ErrorDetail; }`. Then reference `| FileTooLargeResponse` in the union.

**TypeSpec contracts must mirror actual route schemas — verify against real Pydantic models**
- Action: Before finalising a contract TypeSpec, read the actual Pydantic model in `schemas.py` or the route file. The C3 contract omitted `documents?: Record<unknown>[]` from `IngestRequest`, which is load-bearing for the "guard skipped for documents payload" semantic. The fix is always to add the field — not to document the omission as acceptable.

**Kickoff task "completes" field should say "agrees" — reserve "completes" for code deliverables**
- Action: In the plan's Task Breakdown, a `completes` field traditionally means "makes this scenario/contract true in code." A kickoff/alignment task that only ratifies contracts on paper should use `agrees` or `ratifies` to distinguish paper agreement from code realization. Without this distinction, the task history is misleading about what actually landed in the codebase at each step.

### Documentation and close-out

**Moving completed files to `Documentation/Completed/`**
- Action: Make `mv Documentation/Backlog/<feature>-*.md Documentation/Completed/` an explicit close-out checklist item. Feature is not closed until files are moved.

**Close-out doc scope requires grep beyond the plan checklist**
- Action: Run `grep -r "planned\|roadmap\|future release" Documentation README.md` for the feature's key terms. UserManual/ and README.md are frequently missed in dev-authored checklists.

**CLAUDE.md module-path bullets must not mix cross-module feature concerns**
- Action: Every claim in a module-path bullet must be implemented in that module. Cross-cutting features go in their respective module bullets. Verify with grep.

**110 component catalog must annotate ALL modules changed in a feature**
- Action: Annotate acl.py, pipeline.py, _types.py, etc. — not just the entity layer. The 110 convention covers use-case and adapter layer changes too.

**C1 plan fixes must propagate to ALL cross-referencing sections**
- Action: After changing a test strategy claim, grep the entire document for the same phrase and update every instance. Plan docs reference the same fact in multiple places.

**No-op extension changes need explicit "cosmetic" labelling**
- Action: If an extension already routes via a catch-all `else`-branch, adding it to the set is a no-op. Label as "explicitness only — no behavior change" to prevent tautological tests.

**DA hallucinations — verify function signatures before spawning fix agents**
- Action: Before acting on a DA finding about a function signature, grep with `grep -n "def <function>"`. A "Major" severity label does not mean the finding is correct.

**ADR append-only rule — restore original body verbatim on incorrect edits**
- Action: The accepted body is a frozen record. The Amendment section provides D-series context. No partial strikethroughs or "see Amendment" annotations inside the accepted body.

**Docker Compose: `down --volumes [SERVICES]` scopes to specified services (empirically verified)**
- Action: When reviewers claim it destroys ALL named volumes, run an empirical test. Docker Compose v5.1.3 removes only volumes attached to specified services.

**Starlette lowercases HTTP header names on the wire**
- Action: In `urllib` tests checking response headers, normalize to lowercase via `{k.lower(): v for k, v in ...}`. Assert `"www-authenticate"`, not `"WWW-Authenticate"`. `httpx`/`requests` handle case-insensitively; `urllib` does not.

**Plan documents go stale fast — re-verify before implementing**
- Action: Before /iterative-review on a plan, check `git log` for feature merges since the plan was updated. Cross-reference "Resolved open questions" against CLAUDE.md (updated at each close-out).

**docker-compose service name ≠ container name in tests**
- Action: Use `docker compose exec <service>`, not `docker exec <service>`. Inject known keys via `-e ARCHON_SEARCH_API_KEY=<key>` at `docker run` time.

**Empirical markitdown testing is mandatory before documenting format support**
- Action: Run `uv run python -c "from markitdown import MarkItDown; r=MarkItDown().convert(path); print(repr(r.text_content[:100]))"` for every format in `_OFFICE_EXTENSIONS`. Never infer quality from extension-set membership. `.rtf` returns garbled control codes; `.eml` returns readable RFC 822.

**Declare markitdown extras explicitly; verify transitive dep tree**
- Action: Run `uv pip show markitdown | grep Requires` to verify optional backends are declared. Formats working "by accident" via docling's transitive deps will break on fresh installs. Name the actual extras spec (`markitdown[docx,pptx,xls,xlsx,outlook]`), not transitive package names.

**Version specifier floor = tested version; add `<X.0` upper bound for pre-1.0 libs**
- Action: `>=0.1.6,<0.2` not `>=0.1.0`. The floor is the version verified in the current environment. Per project convention, add `<X.0` for all pre-1.0 libraries.

**FastMCP API changed between versions — spike undeclared deps before writing ADRs**
- Action: Before any ADR referencing a third-party API, (1) verify the package is in pyproject.toml, (2) verify method/class names by importing them. `streamable_http_app()` no longer exists in FastMCP 3.4+; use `http_app()`.

**[2026-06-27] — E0d BE-1 (Entities layer additions)**
- Observation: A 500 MB file write in a unit test for `_file_exceeds_limit(path, 0)` is wasteful — the implementation short-circuits before `os.path.getsize` when `max_file_mb <= 0`. Caught by iterative review (C1-T-4). The test only needs to prove the guard fires; a 1-byte file is sufficient.
- Action: For unit tests of short-circuit logic, always use the smallest fixture that exercises the branch — never write large files to prove a path that skips reading the file.
- Confidence: high

**[2026-06-27] — E0d BE-1 (Entities layer additions)**
- Observation: `pytest` unused import in a test file escalates to "Major" in review because it signals a `pytest.raises` test was intended but dropped — a coverage gap signal, not just style. Adding `test_ingest_error_is_exception` (with `pytest.raises`) resolved both the import warning and the missing Exception-base coverage.
- Action: Treat an unused `pytest` import as a missing test hint, not a style nit. Add the corresponding `pytest.raises` test immediately.
- Confidence: high

**[2026-06-27] — E0d BE-2 (Frameworks & Drivers config additions)**
- Observation: When a new sub-config dataclass adds lines to `config.py`, the `path_home_allowlist.txt` ratchet test fails because line numbers shift. This is a forced side-effect of any config.py insertion — always update the allowlist after adding dataclasses.
- Action: After adding any new dataclass or block to `config.py`, run `uv run pytest tests/test_no_hardcoded_path_home.py -n0 --no-cov` early to catch the line-number shift before the full suite.
- Confidence: high

**[2026-06-27] — E0d BE-2 (Frameworks & Drivers config additions)**
- Observation: `bool` is a subclass of `int` in Python, so `isinstance(True, int)` is `True`. Any config field using `_coerce_int` silently accepts `max_file_mb = true` as `1`. The explicit `isinstance(raw, bool)` guard is the correct defense; always test this branch when adding strict-integer validation that bypasses `_coerce_int`.
- Action: When a config field must reject TOML booleans, add `isinstance(raw, bool)` check AND a `test_*_bool_raises_config_error` test. Without the test, the guard is invisible to regressions.
- Confidence: high

**[2026-06-27] — E0d BE-3 (Use Cases size guard in pipeline)**
- Observation: `os.path` is a singleton module in Python — `import os; os.path.getsize` and `from archon_search._types import os; os.path.getsize` both resolve to the SAME function object. Patching `archon_search._types.os.path.getsize` and `archon_search.pipeline.os.path.getsize` in sequence creates two nested patches of the same slot; the inner patch overrides the outer one, making the outer mock's `assert_called_with` always fail. Patching `os.path.getsize` globally once is the correct approach when multiple modules share the same `os.path` reference.
- Action: When multiple modules use `import os; os.path.getsize`, use a single `patch("os.path.getsize")`. Document explicitly that this is intentional because `os.path` is a singleton. Do not try to scope patches per-module — they share the same object.
- Confidence: high

**[2026-06-27] — E0d BE-3 (Use Cases size guard in pipeline)**
- Observation: A plan-specified shared helper (`_file_exceeds_limit`) was implemented in BE-1 but the BE-3 implementor inlined the same logic instead of calling it. Iterative-review caught this as a Major finding. The `assert_not_called()` pattern on `os.path.getsize` (in the `max_file_mb=0` test) is the correct way to prove a guard path is truly skipped — a dead mock that never fails gives false confidence.
- Action: After any implementation, grep for shared helpers named in the plan and verify they are actually called. Use `assert_not_called()` when proving a guard does not fire, not a mock that would silently pass even if the guard ran.
- Confidence: high

**[2026-06-27] — E0d BE-4 (Interface Adapters 413 route pre-check)**
- Observation: A fix agent that addressed the "double getsize + floor rounding" Major finding resolved it by removing the shared helper call entirely and inlining `raw_size > max_file_mb * 1024 * 1024`. This reproduced the BE-3 anti-pattern one task later despite an explicit learnings entry. Cycle 2 review caught it again.
- Action: When fixing a rounding/dual-stat issue that involves a shared helper, keep the helper call for the boolean check and add a second try/except for the display-only stat. Never remove the shared helper to fix an unrelated bug — the two concerns are independent.
- Confidence: high

**[2026-06-27] — E0d BE-4 (Interface Adapters 413 route pre-check)**
- Observation: A `try/except OSError` correctly wrapped the `_file_exceeds_limit` call, but a second bare `os.path.getsize` for the human-readable error message was left unguarded — a second TOCTOU window. Pattern: every `os.path.getsize` call in a route handler that runs after the file was already stat'd needs its own guard. The fallback for the display-size case (`file_size_mb = max_file_mb + 1`) is a correct graceful degradation — the 413 is still returned with a slightly imprecise message.
- Action: After adding any OSError guard in a handler, scan the same code block for additional filesystem calls that could raise and add defensive guards.
- Confidence: high

**[2026-06-27] — E0d T-1 (Tester role e2e tests)**
- Observation: `with patch("os.path.getsize", ...)` exits before an `asyncio.create_task` background task runs in TestClient. The fake returns the real size, so the size guard never fires, and tests 3–4 passed vacuously. `monkeypatch.setattr("os.path.getsize", fn)` persists for the test function's full duration (same process, same thread), including background tasks — the correct fix.
- Action: For any test that patches a built-in function AND spawns background tasks, use `monkeypatch.setattr` rather than `with patch(...)`. Verify the fix by asserting on the side-effect (e.g., `file_results` non-empty), not just the HTTP status code.
- Confidence: high

**[2026-06-27] — E0d T-1 (Tester role e2e tests)**
- Observation: `MagicMock().code is not None` evaluates to `True` — the MagicMock attribute itself is not `None`. A mock returning `MagicMock()` for `pipeline.ingest_file` caused `_dispatch_ingest` to append the mock object to `file_results`, then `json.dump()` raised `TypeError: Object of type MagicMock is not JSON serializable`. The fix is to return a real `IngestResult` whenever the return value's attributes are inspected.
- Action: When a function inspects `.code` or any attribute for business logic (not just calls a method), return a real dataclass instance from the mock, not `MagicMock()`. Type-check the test helper's return value before running the full suite.
- Confidence: high

**[2026-06-27] — E0d T-1 (Tester role e2e tests)**
- Observation: A tautological `or len(results) > 0` fallback on a path assertion silently masks false positives — if ANY results exist, the assertion passes even if they are from the wrong file. DA review (C2-1) caught this in cycle 2.
- Action: Never use `or len(collection) > 0` as a fallback on a membership assertion. Either the member check passes or the test fails. Drop the fallback unconditionally.
- Confidence: high

**[2026-06-27] — E0d BE-5 (Interface Adapters MCP schema)**
- Observation: `IngestResultSchema.code: str | None` is weaker than the domain type `Literal["file_too_large"] | None`. The plan explicitly stated "Using Literal rather than bare str enables exhaustive type-checking" — the first implementation widened to `str` and was caught by iterative review (C1-A-1). The fix is trivial but the Literal matters: it produces `{"const": "file_too_large"}` in JSON Schema (not `{"type": "string"}`), and Pydantic rejects unknown code values at the schema boundary instead of silently passing them through.
- Action: At any MCP/REST schema boundary that maps a `Literal` domain field, use `Literal[...]` in the Pydantic schema too. `str` at the boundary defeats the contract signal and drops JSON Schema constraints for MCP clients.
- Confidence: high

**[2026-06-27] — E0d BE-5 (Interface Adapters MCP schema)**
- Observation: The `ingest_directory` list-return path (`mcp.py:1003`) uses the same `IngestResultSchema.from_result(r).model_dump()` pattern as `ingest_file`. A new field added to `IngestResultSchema` (like `code`) propagates automatically to both paths. But without an explicit `ingest_directory` test for the new field, this is invisible to regressions — a future refactor could filter error items out of the list.
- Action: When adding a field to `IngestResultSchema`, add a unit test for the `ingest_directory` mixed-batch list return (one ok + one error result) to verify the field propagates in both list items, not just the single-result `ingest_file` path.
- Confidence: high

**[2026-06-27] — E0d T-4 (Project close-out)**
- Observation: `archon-search.toml.example` was updated in the same commit as the implementation (BE-2), so the example was already correct at close-out. When the implementation task and the example update are in the same task, the close-out doc sweep must verify rather than re-apply the change.
- Action: At close-out, grep `archon-search.toml.example` for the new TOML section key before editing — if it's already present, skip the edit and note it as already done.
- Confidence: high

**[2026-06-27] — E0d T-4 (Project close-out)**
- Observation: The OpenAPI snapshot regeneration step (`uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot -n0 -x`) correctly requires Python 3.12 (matching CI). Running it with a single-test invocation produces a coverage failure (expected; only run the snapshot test file, not the full suite) — the 1 passed / coverage fail output is the expected correct outcome.
- Action: Never interpret "FAIL Required test coverage not reached" from a single-test invocation as a test failure. Only "N passed" vs "N failed" matters for the snapshot update step.
- Confidence: high

**[2026-06-27] — E0d T-4 (Acceptance fact-check)**
- Observation: At close-out, all 10 documentation files in the plan's "Documentation update" section were already correctly updated by the implementing tasks (140, 110, 600, UserManual, CLAUDE.md, toml.example, BREAKING.md, OpenAPI snapshot, learnings.md, plan file). The fact-check was a grep-and-verify pass, not an edit pass. 5679 tests passed with 93.55% coverage (above the 85% gate). All 10 acceptance criteria were confirmed by reading actual code — no assumptions.
- Action: At close-out, do a grep-first pass across all listed doc files before editing. If every file is already updated, the close-out is a verify-only pass. Never re-apply changes that are already correct — it introduces noise.
- Confidence: high

**[2026-06-27] — E0d T-4 (Iterative review — pipeline bugs)**
- Observation: The working tree bundled an unrelated `hybrid_search → hybrid_search_with_trace` pipeline refactor alongside the E0d docs close-out. Iterative review (Brooks-Lint C1-B-1) caught that both `_search_standard` and the `search()` RAG Fusion path were calling `hybrid_search_with_trace(candidate_depth=self._top_k_retrieve)` — a 3×–5× candidate fetch regression vs. all sibling call sites that use `max(self._top_k_retrieve * 3, 20)`. A second DA finding (C1-I-1) identified that `source_path_glob` post-filtering was applied only in `_search_standard` and silently omitted in the RAG Fusion fuse path.
- Action: When committing a refactor that migrates a store-layer call to a new method, audit every call site's `candidate_depth` argument and compare to sibling call sites — silent under-fetch is the most common regression pattern in these migrations. Also verify that every post-filter applied in the old method is re-applied in the new caller.
- Confidence: high

**[2026-06-28] — E0e K1 (Contract/kickoff task: TypeSpec contract review)**
- Observation: A contract TypeSpec stub that added `ExcludedCollection` used field name `collection` instead of the real schema's `name` field (from `schemas.py ExcludedCollectionSchema`). The error was caught by iterative review (Cycle 2 DA + Brooks-Lint). Contract fidelity defects — especially wrong field names — are invisible until a client implements against them.
- Action: When adding a new model to a TypeSpec contract that represents an existing Python schema, always grep the actual Pydantic/dataclass field names first (`grep -n "class ExcludedCollection\|name\|reason" archon_search/`). Never guess field names from the model's concept name.
- Confidence: high

**[2026-06-28] — E0e K1 (Contract/kickoff task: seam file design)**
- Observation: TypeSpec seam files that are partial views (showing only E0e-delta fields, not the full type) must be explicitly labeled as partial or readers assume they are complete. Without a "E0e delta view — missing fields: results, excluded_collections, fanout_timings" comment, reviewers and implementers treated `SearchPipelineResult` as complete and flagged it as wrong.
- Action: Any TypeSpec seam file that intentionally omits fields must carry a top-level docstring listing the omitted fields and pointing to the source of truth (e.g., `pipeline.py:43-51`). The pattern "Stub — see X for full shape" is acceptable for the HTTP API stubs but more detail is needed for internal seam files.
- Confidence: high

**[2026-06-28] — E0e K1 (Contract/kickoff task: RAG Fusion coverage gap)**
- Observation: The E0e plan's S1-S11 scenario table had zero coverage for the RAG Fusion + multi-collection + filters combination, despite the plan's BE-2 task explicitly identifying 4 separate RAG Fusion call sites that must all receive `filters=`. The tester role allocations table (cheapest-level) for S12 was also absent, leaving testers without guidance. Both gaps were caught only by iterative review.
- Action: When writing scenarios for any multi-collection feature, always include at least one RAG Fusion scenario (even if unit-level). RAG Fusion has structurally independent code paths that can silently miss parameters even when the standard path is correct. Add the scenario to both the table AND the cheapest-level allocation table before declaring the plan ready.
- Confidence: high

**[2026-06-28] — E0e BE-1 (Entities schema — `applied_filters` + language doc)**
- Observation: Adding `applied_filters: SearchFilters | None = None` to `SearchResponse` broke one existing test (`test_search_response_schema_fields` in `test_routes_search_acl.py`) that used an exact-match dict assertion. The full suite caught it; the task-scoped pre-commit run did not (only the new tests were run pre-commit).
- Action: When adding a new optional field to a Pydantic response model, grep for exact-match `model_dump()` assertions across the entire test suite (`grep -rn "model_dump\|== {" tests/`). Update them in the same change. Do not rely on the task-scoped test run to catch these — they sit in sibling files.
- Confidence: high

**[2026-06-28] — E0e BE-1 (doc-ahead-of-code for entity-level descriptions)**
- Observation: Removing a restriction caveat from the entity model's `Field.description` (e.g., "single-collection queries only" from `SearchFilters.language`) while the Presentation-layer restriction still exists is correct for the entity layer — the entity IS capable after E0e. It is distinct from MCP tool `_LanguageParam*` description strings, which should only be updated when the runtime restriction is removed (BE-4). Brooks-Lint (C1-B-1) flagged the entity-level change as "doc ahead of implementation" — but the entity capability is real; only the route handler hasn't threaded it through yet.
- Action: Distinguish entity-level field descriptions (document the entity's true capability) from presentation-layer tool descriptions (document the tool's current runtime behavior). Don't update presentation-layer descriptions until the runtime supports them.
- Confidence: high

**[2026-06-28] — E0e BE-2 (Use Cases: `search_many` filters threading)**
- Observation: `search_many()` had 4 distinct `hybrid_search_with_trace()` call sites (RAG Fusion per-collection vector, RAG Fusion FTS-only fallback, embedding-failure fallback via `_fanout_merge_acl()`, standard path via `_fanout_merge_acl()`). The initial implementation missed the FTS-only fallback and the embedding-failure fallback. Iterative review exposed that the embedding-failure test was vacuously passing (the mock always succeeded on the fallback embed). Fix: use `call_count == 2` to fail only the variant embed (call 2), allowing the fallback single-query re-embed (call 3) to succeed.
- Action: When threading a new parameter through a fan-out method, enumerate ALL call sites by grepping for the callee name, including fallback branches inside try/except blocks. The fan-out path often has 2× as many call sites as the happy path alone.
- Confidence: high

**[2026-06-28] — E0e BE-2 (glob post-filter placement in RAG Fusion path)**
- Observation: The first implementation placed the glob post-filter after cross-collection merge ("Step D.5"), while `_fanout_merge_acl()` applied it per-leg before trim. In the RAG Fusion path, non-matching candidates consumed `fanout_leg_trim` slots before being filtered, silently degrading recall when trim was tight. Iterative review caught this asymmetry.
- Action: For any post-filter that is per-leg in one code path (`_fanout_merge_acl`), it must also be per-leg in the sibling path (RAG Fusion per-collection loop). Never apply a per-result filter after a cross-collection merge — it allows non-matching candidates to consume trim budget.
- Confidence: high

**[2026-06-28] — E0e BE-3 (pre-existing implementation and duplicate test files)**
- Observation: When the BE-3 task was picked up, the Presentation-layer implementation (`routes_search.py`) was already in place — the restriction had been removed and `applied_filters` wired in both handler paths. A previous implement-next run had also created `tests/server/test_e0e_be3_search_filters.py` (untracked). Writing a new `tests/test_e0e_be3_search_route_filters.py` without checking existing untracked files duplicated 3 tests and created a maintenance trap. Iterative review flagged the redundancy.
- Action: Before writing new test files, always run `git status --short` and read any existing untracked test files that look related. An untracked file is often work already done by a prior session. Delete the redundant file immediately rather than waiting for the review cycle.
- Confidence: high

**[2026-06-28] — E0e BE-3 (applied_filters echo not the same as filter forwarding)**
- Observation: The single-collection test `test_post_search_single_collection_with_filter_applied_filters_echoed` initially only checked `response["applied_filters"]["language"] == "en"`. This only proves the echo works; it does NOT prove the filter was forwarded to `pipeline.search()`. Since `applied_filters=body.filters` is set directly from the request (Option B), the handler could theoretically echo filters without passing them down. The iterative review caught this gap and added a `pipeline.search.call_args.kwargs["filters"]` assertion.
- Action: For echo-field tests, always add a second assertion verifying the value was also forwarded to the downstream call — echo correctness and forwarding correctness are distinct. Check `mock.call_args.kwargs["param"]` in addition to the response body.
- Confidence: high

**[2026-06-28] — E0e BE-2 (mock signature breakage in sibling test files)**
- Observation: Adding `filters: SearchFilters | None = None` to `search_many()` and `_fanout_merge_acl()` broke 12 mock helpers in 3 sibling test files (`test_pipeline_multi.py`, `test_pipeline_explain.py`, `tests/eval/test_multi_collection_merge.py`). Each file had a local `_hybrid()` stub that didn't accept `filters`. The failures appeared across non-obvious filenames (eval harness, explain tests) that are not in the same directory as the changed code.
- Action: After changing a method signature, grep all test files for the method name AND for local stub functions (`def _hybrid`, `def _search`) that shadow it. Run `grep -rn "def _hybrid\|async def _hybrid" tests/` before committing.
- Confidence: high
