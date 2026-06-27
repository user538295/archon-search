# Learnings

## What Has Failed

**implement-all T-1: subagent stops after review without committing or checking off plan**
- Action: Subagent prompt must require `grep "\- \[x\]" <plan>` and `git log --oneline -1` as proof before declaring done. Stated intent is not execution.

## What Has Worked

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
