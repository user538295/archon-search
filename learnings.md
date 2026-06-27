# Learnings

## What Has Worked

**2026-06-27 — E0c BE-3: moving Pydantic Field bounds to handler bodies changes 422 error shape — document in BREAKING.md**
- Observation: Removing `le=100` from `SearchRequest.top_k` and moving the upper-bound check to the handler body changes the 422 detail from a Pydantic array `[{"loc":…,"msg":…}]` to a plain string `{"detail":"…"}`. Both shapes use status 422, but they are structurally incompatible for clients that parse validation-error lists. This is a wire-level breaking change that must be recorded in BREAKING.md.
- Action: Whenever validation moves from Pydantic Field/model_validator to handler body (to get access to config), add a BREAKING.md entry noting the 422 envelope change. The T-3 close-out task owns BREAKING.md for E0c; do not let it slip to a surprise during T-close fact-checking.
- Confidence: high

**2026-06-27 — E0c BE-3: "not 422" test assertions are weaker than "== 404" for boundary-condition tests**
- Observation: Tests like `test_fanout_respected_from_config_at_limit` initially used `assert resp.status_code != 422`. Since the collections don't exist, the real response is 404. The iterative review caught that `!= 422` is vacuously satisfied even if the entire validation block is deleted. `== 404` is stronger: it proves the request got past validation AND reached collection lookup.
- Action: For "at-limit" boundary tests that use non-existent resources, always assert the specific downstream error code (404 for missing collection) rather than "not the rejection code". Reserve `!= 422` only when the response code is genuinely variable.
- Confidence: high

**2026-06-27 — E0c BE-2: path_home_allowlist.txt line numbers shift when dataclass lines are added — must update allowlist**
- Observation: Adding 2 lines to the `SearchConfig` dataclass shifted `get_default_config_path()`'s `Path.home()` call from line 194 to 196. The `test_path_home_ratchet` test failed because the allowlist still had `config.py:194`. The hash was unchanged — only the line number needed updating.
- Action: Whenever adding lines to `config.py` before `get_default_config_path()` (around line 190), update `tests/path_home_allowlist.txt` to reflect the new line number. Run `grep -n "Path.home" archon_search/config.py` to find the new line, then update the allowlist entry.
- Confidence: high

**2026-06-27 — E0c BE-2: All `[search]` config fields need both zero AND negative validation tests — not just zero**
- Observation: DA reviewer found that every existing `[search]` field in `test_config.py` (`max_fanout`, `fanout_leg_trim`, `fanout_timeout_seconds`) has separate tests for zero and negative values. The initial BE-2 implementation only added a zero test, missing the established pattern. The plan spec listed 3 tests but the codebase convention required 5.
- Action: When adding a new integer config field that uses `<= 0` / `> 0` validation, always add both a zero test and a negative test. Check `tests/test_config.py` for the sibling field pattern before writing tests.
- Confidence: high

**2026-06-27 — E0b T-close: All docs pre-updated during implementation tasks — T-close is a verification pass, not a documentation sprint**
- Observation: All eleven documentation items in the "Documentation update" section of the E0b plan (600_api_reference, 110_component_catalog, UserManual/02, 05, 06, 07, BREAKING.md, CLAUDE.md, learnings.md) were fully updated during their respective implementation tasks (BE-*, FE-*, T-*). T-close found no documentation gaps requiring new edits. The OpenAPI snapshot was also already current (no diff after `--update-openapi-snapshot`). The only T-close action was the learnings.md entry itself and the plan checkbox.
- Action: For future features, continue this pattern: each implementing task owns its documentation as part of "done". T-close is then a fact-check pass (grep for symbols, hit endpoints, read code) and a clean-up catch-all — not a bulk documentation sprint. This keeps documentation fresh and reduces T-close risk.
- Confidence: high

**2026-06-27 — E0b T-close: Full test suite clean at close-out — 5574 passed, 0 failed**
- Observation: Running `uv run pytest` at T-close produced 5574 passed, 13 skipped, 0 failures. Coverage 93.51% (well above 85% gate). All E0b tests were integrated by their respective tasks. No "pre-existing" failures needed fixing at close-out. The acceptance criteria fact-check confirmed all twelve acceptance criteria are wired: expansion_warning in routes_search.py, key_available in routes_status.py, stderr warnings in cli/status.py, FAILED_EXPIRED in maintenance_loop.py (transitions + GET /jobs filter), failed_expired_ingest_count in routes_status.py, truncated_count in reader.py, ACL warnings in acl.py wired into IngestResult.warnings in pipeline.py (ACL sidecar path) + cli/ingest.py (stderr print), --timeout exit-0 and exit-2 in maintenance_cmd.py, EnvironmentFile in linux.py, wrapper script in macos.py, and OpenAPI snapshot verified passing.
- Action: The pattern of running tests after each implementing task (not just at T-close) is what keeps the suite green at close-out. T-close suite run is a final gate, not a discovery mechanism.
- Confidence: high

**2026-06-26 — E0b T-close: iterative review found doc attribution mismatch (platform/ vs server/) — module-based CLAUDE.md bullets must not mix in feature cross-cutting concerns**
- Observation: The CLAUDE.md `platform/` bullet was extended with `GET /status` hyde/rag_fusion fields, which are implemented in `routes_status.py` and `schemas.py`, not `platform/`. DA2 flagged this as Major. The fix splits the annotation: platform/ covers only `.secrets.env` and wrapper script; server/ section gets the status response fields. In CLAUDE.md, bullets are organized by module path, not by feature — never bundle cross-module feature documentation into a single module-path bullet.
- Action: When adding E0b-style annotations to CLAUDE.md, verify that every claim in a module-path bullet is implemented in that module. Use grep to confirm. Cross-cutting features must be documented in their respective module bullets.
- Confidence: high

**2026-06-26 — E0b T-close: 110 component catalog acl.py and pipeline.py entries need E0b annotations — not just _types.py**
- Observation: The T-close checklist item "document IngestResult.warnings" was handled by annotating `_types.py` in 110 (correctly), but `acl.py`'s return-type change and `pipeline.py`'s warning-collection logic were not annotated. The 110 catalog convention is to annotate E0b changes inline, and all three modules changed in E0b (acl.py return types, pipeline.py warning collection, _types.py new field). DA3 correctly flagged the gap.
- Action: For close-out tasks, annotate ALL modules that changed in the feature in the 110 catalog — not just the entity layer. The "IngestResult.warnings" checklist item covers the field, but the use-case and interface-adapter changes (how it's populated) also need annotations in their respective module rows.
- Confidence: high

**2026-06-26 — E0b T-4: Assert directly on result.stderr, not combined stdout+stderr — enforces the stderr contract**
- Observation: All 4 reviewers (3 DA + Brooks-Lint) independently flagged the `combined = result.output + (result.stderr or "")` pattern as Major. Asserting on `combined` means a regression where a message moves from stderr to stdout (dropping `err=True`) passes green. The fix: assert directly on `result.stderr`. Lesson applies to any Click test checking error-channel output.
- Action: Never concatenate `result.output + result.stderr` for assertions on output that must be on stderr. Assert on `result.stderr` directly. Reserve `combined` only when the channel is genuinely irrelevant.
- Confidence: high

**2026-06-26 — E0b T-4: Add a unique string assertion to pin which code path fired — not just exit code**
- Observation: S22 asserted `exit_code == 0` which is shared by both the timeout path and the success path. DA3 flagged that the test could silently pass via the success path. Fix: add `assert "Timed out after 6s" in result.stderr` — this string only appears in the timeout branch, uniquely identifying the path and verifying `--timeout` flag propagation simultaneously.
- Action: For any test with multiple code paths that share an exit code, always add an assertion on a distinguishing string unique to the expected path. Exit code alone is insufficient when paths share the same code.
- Confidence: high

**2026-06-26 — E0b FE-4: Warning loop after if/else branch convergence is branch-agnostic — no separate test per branch needed**
- Observation: The warning-printing loop in `cli/ingest.py` sits after the `if timings_enabled / else` block that both produce `results`. All 4 reviewers (3 DA + Brooks-Lint) flagged that only the `timings_enabled=False` branch was exercised by the test, but the loop is structurally outside both branches — it always runs. The finding was Minor in every reviewer's assessment; a second test would be defensive-only, not a real correctness gap.
- Action: When new code sits after an if/else convergence point, document that position explicitly in the test or a comment. Reviewers will flag the untested branch; having a clear explanation ("loop is outside both branches at line X") stops the finding from being elevated to Moderate.
- Confidence: high

**2026-06-26 — E0b FE-4: `CliRunner()` does not accept `mix_stderr` in Click 8.3.3 — the kwarg was removed**
- Observation: Attempting `CliRunner(mix_stderr=False)` raised `TypeError: CliRunner.__init__() got an unexpected keyword argument 'mix_stderr'`. The default `CliRunner()` in Click 8.3.3 already separates streams correctly — `result.stderr` works without any kwarg.
- Action: Never pass `mix_stderr=False` to `CliRunner()`. The default runner is correct in Click 8.3.3. Verified by FE-3 and FE-4 independently.
- Confidence: high

**2026-06-26 — E0b FE-3: Click 8.3.3 default CliRunner() populates result.stderr separately — empirically verified**
- Observation: DA1 and DA2 in Cycle 2 both flagged the default `CliRunner()` (which has `mix_stderr=True` in older Click) as Major/Critical, claiming `result.stderr` assertions would be vacuous. Empirical test proved them wrong: in Click 8.3.3, `result.stderr` correctly captures `click.echo(..., err=True)` output even with the default runner. `result.output` contains combined stdout+stderr; `result.stderr` contains stderr only. The "Major/Critical" finding was a false alarm based on outdated Click documentation.
- Action: When a reviewer flags `result.stderr` assertions as vacuous under default `CliRunner()`, always verify empirically: `uv run python -c "from click.testing import CliRunner; r = CliRunner(); print(repr(r.mix_stderr))"` and run a smoke test. Do not trust DA claims about third-party library defaults without verification.
- Confidence: high

**2026-06-26 — E0b FE-3: Defensive `or 0` on optional server fields must be tested explicitly**
- Observation: `_print_failed_expired_count` uses `server_payload.get("failed_expired_ingest_count", 0) or 0` — the `or 0` guards against `None` (field present but null). DA3 and DA2 independently flagged missing test for this specific branch. The absent-key test (pre-E0b server) exercises `.get(key, 0)` returning the default; the null-value test exercises `None or 0`. Both are distinct code paths and must both be tested.
- Action: Whenever `get(key, default) or fallback` is used, add one test for absent key AND one test for null value. They exercise different branches of the compound expression.
- Confidence: high

**2026-06-26 — E0b FE-2: FAILED_EXPIRED silent-exit discovered via iterative review, not initial implementation**
- Observation: The initial FE-2 implementation treated `FAILED_EXPIRED` as a non-failure in both `_poll_job` (export) and `_wait_for_jobs` (backup): export printed "Job ended with status: FAILED_EXPIRED" and exited 0; backup silently discarded the job from pending. Cycle 1 review by all three DA agents independently flagged this as Major. The fix was to use `status in {"FAILED", "FAILED_EXPIRED"}` in both paths.
- Action: Whenever `_TERMINAL_STATUSES` contains `FAILED_EXPIRED`, verify that every code path that checks for FAILED also checks for FAILED_EXPIRED. Silent discard of FAILED_EXPIRED is the most common silent-failure bug in polling loops.
- Confidence: high

**2026-06-26 — E0b FE-2: FAILED+timeout race in multi-job backup polling**
- Observation: If any job confirmed FAILED before the timeout counter exhausted `max_polls`, the timeout branch would fire first and exit 0, hiding the confirmed failure. The fix: check `if failed:` before `raise SystemExit(0)` in the timeout branch.
- Action: In multi-job polling loops with both a timeout and a failed-list accumulator, always check the accumulated failure list in the timeout branch before deciding to exit 0.
- Confidence: high

**2026-06-26 — E0b FE-1: Dead module-level constants must be deleted when replaced by a parameter — not just commented as "legacy"**
- Observation: `_WAIT_MAX_POLLS` was retained with a "legacy; overridden by --timeout" comment after `_wait_for_pass` was rewritten to use `max_polls = max(1, timeout_seconds // _POLL_INTERVAL_SECONDS)`. Five tests kept patching the constant, giving a false impression it controlled the loop. All 4 reviewers (3 DA + Brooks-Lint) independently flagged it in Cycle 1. The tests passed only by coincidence (mocked `time.sleep` + infinite `return_value`).
- Action: When replacing a module-level constant with a parameter-derived value, delete the constant immediately and replace all test patches of it with the actual controlling input (e.g. `--timeout N` CLI arg). Leaving it as "legacy" creates Hyrum's Law coupling and future trap for test authors.
- Confidence: high

**2026-06-26 — E0b FE-1: `_WAIT_MAX_POLLS` patch in test controlled nothing — silent 60-poll regression**
- Observation: `test_maintenance_run_wait_timeout` patched `_WAIT_MAX_POLLS=3` expecting 3 polls, but actually looped 60 times (default timeout 120 / poll interval 2). The test was slower than designed and the `=3` patch was a lie about what was being tested. Tests passed only because `httpx.get` was mocked with `return_value` (infinite same response).
- Action: When a polling loop's bound changes from a constant to a parameter, immediately update all tests to exercise the parameter (e.g. `--timeout 6` → 3 polls), not the dead constant.
- Confidence: high

**2026-06-26 — E0b FE-1: Exit-2 detection via collection health `last_error` is sound because `last_error` is cleared at each pass start**
- Observation: `_get_maintenance_state` checks `any(entry.get("last_error") is not None ...)` across `collection_health` to detect a failed pass. This is sound because `maintenance_loop.py:565` clears `last_error = None` at the start of each collection's processing in every pass. DA agents independently verified this (cross-pass staleness is not possible for actively processed collections; excluded collections do retain stale errors but that's a pre-existing server-side gap).
- Action: When detecting failure via a field that might be stale, always verify the server resets it each pass. If it doesn't, the detection could yield false positives from prior passes.
- Confidence: high

**2026-06-26 — E0b T-3: Pre-seeding JobStore before make_real_app by writing to the same file path**
- Observation: T-3 needed to seed a FAILED_EXPIRED job visible to a real app started by `make_real_app`. Creating a `JobStore(path=tmp_path/"jobs.json")` before entering `make_real_app` and seeding jobs to it works because `make_real_app` creates its own `JobStore` at the same path, and `JobStore.__init__` reads eagerly from disk via `_load()`. The FAILED_EXPIRED status is not in `_CRASH_STATUSES`, so it survives crash recovery. The 7-day eviction window doesn't affect freshly-created jobs.
- Action: For e2e tests needing pre-seeded job store data with `make_real_app`, create a `JobStore` at `tmp_path / "jobs.json"` before the context manager, seed it, then enter `make_real_app` — it reads the same file on init. No need to expose the job store from `make_real_app`.
- Confidence: high

**2026-06-26 — E0b T-3: e2e "negative" scenario for bool|None telemetry field only needs to test None (the production path), not False**
- Observation: S16 tests that a non-truncated telemetry entry (`truncated=None`) produces `truncated_count == 0`. Brooks-Lint C2-B-1 raised that the `is True` vs `is not None` discrimination is not tested (the `False` case). The resolution: S16's purpose is to verify the production path (`None` omitted field → not counted), not to re-test the identity-check logic. The `truncated=False` case is already covered at unit level in `tests/telemetry/test_reader.py::test_compute_stats_truncated_count_excludes_false_entries`. A docstring note pointing to that unit test is sufficient.
- Action: For `bool | None` fields, the e2e/integration test verifies the production code path (usually `None` when nothing happens). Point to the unit test for the `False` exclusion rather than duplicating it in an e2e test. Add a docstring cross-reference so reviewers don't flag it as a gap.
- Confidence: high

**2026-06-26 — E0b BE-10: `type(j) is IngestJob` subclass exclusion must always have a negative-case test**
- Observation: All 4 reviewers (3 DA + Brooks-Lint) independently flagged that the `type(j) is IngestJob` predicate — the key distinguishing logic — had zero test coverage in the negative direction. `_seed_failed_expired_job` only calls `job_store.create()` which returns base `IngestJob`, so no test could catch a regression to `isinstance`. A fix agent added `test_status_failed_expired_count_excludes_export_jobs` (seeds an `ExportJob` with `FAILED_EXPIRED`, asserts count=1 not 2). Without this test, replacing `type(j) is IngestJob` with `isinstance(j, IngestJob)` would silently pass all tests while counting ExportJob failures.
- Action: Whenever a count/filter uses an exact-type predicate (`type(j) is X`), always add a test that seeds a subclass instance with the target status and asserts it is NOT counted. The predicate is the whole point — test its negative direction explicitly.
- Confidence: high

**2026-06-26 — E0b BE-10: Namespace isolation tests must be two-sided**
- Observation: The initial namespace isolation test seeded 2 jobs in nsA and 3 in nsB, then asserted nsA saw count=2. All reviewers noted the test is one-sided — a constant-return implementation that always returned 2 would pass. Fix: add a second client (nsB) and assert count=3. This turns the test from "nsA doesn't see too many" to "each namespace sees exactly its own jobs."
- Action: For any namespace-scoped count test, always assert both namespaces return their expected distinct values. One-sided assertions cannot detect constant-return bugs.
- Confidence: high

**2026-06-26 — E0b BE-9: `truncated_count` — `is True` identity check is the correct pattern for `bool | None` optional fields**
- Observation: `TelemetryEntry.truncated` is `bool | None` defaulting to `None`. Using `entry.truncated is True` (identity check) correctly excludes both `None` (field absent / legacy entries) and `False` (explicitly not truncated). A truthiness check (`if entry.truncated`) would behave the same for `True`/`None` but documents the wrong intent. All 4 reviewers confirmed the identity check is correct and asked for a `truncated=False` test to lock down the three-state contract.
- Action: For any `bool | None` field where `None` and `False` are semantically different and must both be excluded, use `is True`. Always add a test for all three states (`True` counted, `False` excluded, `None` excluded) — reviewers will flag a two-test suite as a coverage gap.
- Confidence: high

**2026-06-26 — E0b BE-9: Adding a field to `StatsResponse` breaks the OpenAPI snapshot — regenerate in the same task**
- Observation: Adding `truncated_count: int = 0` to `StatsResponse` broke `test_openapi_spec_matches_snapshot` immediately. Per the D9 BE-9 learning (already in file), regenerate with `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot` in the same commit. This is confirmed: additive fields with defaults do NOT require a `schema_version` bump (the architecture DA said this, confirmed by the project's existing pattern — no bump has ever been done for additive fields).
- Action: Any additive Pydantic field on a response model that appears in the OpenAPI surface requires regenerating `tests/server/openapi_snapshot.json` (Python 3.12) in the same commit. No `schema_version` bump needed for additive backward-compatible fields.
- Confidence: high

**2026-06-26 — E0b BE-5: Use `JobStore.transition()` not `update()` for state transitions in maintenance loops**
- Observation: Initial BE-5 implementation called `job_store.update(job_id, status=FAILED_EXPIRED)` which raises `KeyError` if a job is evicted between `list()` and `update()` — this would abort all remaining jobs in the pass. `JobStore.transition(job_id, {FAILED}, FAILED_EXPIRED)` returns `None` instead of raising, preventing the abort. All 4 reviewers (3 DA + Brooks-Lint) independently caught this in Cycle 1.
- Action: For any status transition in a batch loop, use `transition()` not `update()`. Check the return for `None` (evicted/already-changed) and log DEBUG. This is exactly why `transition()` exists. The `create()` call in the same loop was already guarded with try/except — always apply the same defensive pattern to sibling write operations.
- Confidence: high

**2026-06-26 — E0b BE-5 Cycle 2 Brooks-Lint: docstring step-list drifted from code after restructuring a numbered algorithm**
- Observation: `_run_failed_ingest_retry`'s docstring (maintenance_loop.py:317-335) still lists "7. Increment retry_counts keyed ..." as an unconditional algorithm step, but after the FAILED_EXPIRED restructure the increment at line 500 only happens on the re-enqueue path — the two transition paths (aged-out, exhausted) `continue` before it. Step 5/6 were renumbered correctly but step 7's wording ("Increment retry_counts") reads as always-happens. Minor doc-accuracy drift, not a code bug. Also the new `test_maintenance_loop_invalid_created_at_skips_with_warning` asserts only `any(r.levelno >= WARNING)` — any incidental warning satisfies it rather than the specific unparseable-timestamp message; weak but the path is real (cutoff non-None → line 421 WARNING fires).
- Action: When restructuring a numbered-step docstring where some steps become conditional (guarded by an early `continue`), re-read every remaining step number for accuracy, not just the ones you renumbered. For "WARNING is logged" tests, assert on `r.getMessage()` substring (e.g. "unparseable") not just `r.levelno >= WARNING`.
- Confidence: high

**2026-06-26 — E0b BE-2: Changing exception-swallowing to re-raise breaks existing tests that asserted the swallowed behavior**
- Observation: `rag_fusion.generate_variants()` previously caught `asyncio.TimeoutError` and generic `Exception` and returned `[]`. Three existing tests (`test_generate_variants_timeout_fallback`, `test_generate_variants_api_error_fallback`, `test_fingerprint_no_raw_query_in_log`) all asserted `result == []` or called the function without `pytest.raises`. When BE-2 changed both handlers to `raise`, all three broke. Found immediately by running the rag_fusion test file before the full suite.
- Action: When changing exception-swallowing code to re-raise, always grep for tests that call the function without `pytest.raises`. Run that file's tests before the full suite to catch failures fast.
- Confidence: high

**2026-06-26 — E0b BE-2: asyncio.TimeoutError IS a subclass of Exception in Python 3.12 — verify MRO before assuming handler ordering doesn't matter**
- Observation: `asyncio.TimeoutError` inherits from `TimeoutError` → `OSError` → `Exception` → `BaseException`. The `except asyncio.TimeoutError` handler MUST come before `except Exception` to catch TimeoutErrors specifically. The implementation correctly ordered them, but this was verified empirically (`uv run python3 -c "print(issubclass(asyncio.TimeoutError, Exception))"` returned `True`).
- Action: When writing `except asyncio.TimeoutError` followed by `except Exception`, always verify the ordering is correct. `asyncio.TimeoutError` is NOT a subclass of `BaseException` alone — it IS a subclass of `Exception`, so ordering matters.
- Confidence: high

**2026-06-26 — E0b BE-1: Default value changes have a blast radius — always grep all 5 artifact types before committing**
- Observation: Raising `HyDEConfig.timeout_seconds` and `RAGFusionConfig.timeout_seconds` from 5.0 → 10.0 required updates across 6 files beyond `config.py`: `tests/test_config.py` (3 assertions), `tests/test_config_defaults.py` (snapshot dict), `archon-search.toml.example` (2 lines), `Documentation/UserManual/05_searching.md` (3 lines), `Documentation/UserManual/02_wizard.md` (2 lines). The initial diff only touched `config.py` and `test_config_defaults.py`. All 4 reviewers (3 DA + Brooks-Lint) caught the same omissions.
- Action: When changing a default value, before committing, grep for the old value across: (1) test assertion files (`tests/test_config.py`), (2) example config files (`*.toml.example`), (3) user manual docs (`Documentation/UserManual/`), (4) architecture docs, (5) any other snapshot/expected-dict tests. The snapshot test in `test_config_defaults.py` is not sufficient alone — `test_config.py` has per-field assertions that must also be updated.
- Confidence: high

**2026-06-26 — E0b K1: `generate_variants()` swallows exceptions — verify propagation before spec-ing pipeline warning fields**
- Observation: `rag_fusion.generate_variants()` catches BOTH `asyncio.TimeoutError` (line 162) AND generic `Exception` (line 169) internally and returns `[]`. The pipeline's outer `except Exception` block is dead code for all timeout and API-error cases. The C4 contract specifying distinct messages ("RAG Fusion timed out" vs "RAG Fusion expansion failed") is unimplementable without first changing `generate_variants()` to re-raise these exceptions. This was found during K1 contract review, not during implementation — catching it early prevents a wasted BE-2 implementation attempt.
- Action: Before specifying a pipeline-level field that distinguishes failure modes of an external call, read the external call's source to verify which exceptions propagate out vs are swallowed internally. If the generator swallows them, the pipeline cannot distinguish — either change the generator's exception handling or use a different detection mechanism.
- Confidence: high

**2026-06-26 — E0b K1: TypeSpec delta-model naming — use *Extension for HTTP seams, *E0bDelta for internal seams**
- Observation: Prior D-series contracts (D3, D8) used `*Extension` suffix for additive fields on existing HTTP responses (`StatusResponseSchemaExtension`, `StatusResponseExtension`). K1 introduced `*E0bDelta` for the internal-seam contracts (pipeline result, ingest result) and `*Extension` for HTTP seams (status, telemetry). This distinction (delta = internal no-emit, extension = HTTP emitted) is consistent and defensible.
- Action: When authoring TypeSpec contracts for a feature: use `*E0bDelta` (or `*FeatureDelta`) for `--no-emit` internal-seam models showing only new fields; use `*Extension` for HTTP-seam models. Do not include pre-existing fields in partial models — they create a misleading completeness illusion.
- Confidence: high

**2026-06-26 — E0b iterative-review on team plan: route-level vs pipeline-level seam is a plan killer**
- Observation: The initial E0b plan put `expansion_warning` on `SearchPipelineResult` (C4 contract). Three independent DA agents and Brooks-Lint all caught that HyDE runs at the ROUTE level before `pipeline.search()` is called — the pipeline never sees HyDE. The fix was to split: RAG Fusion warning on `SearchPipelineResult`, HyDE warning assembled at route level. A second subtlety: `resolve_hyde_vector()` returns `(None, False)` for ALL failure modes (timeout, API error, empty response, missing key) — you cannot distinguish them at call time. Fix: use a generic "HyDE expansion failed" message, not "HyDE timed out".
- Action: Before putting any "failure signal" on a domain result type, verify WHERE the failing operation runs (route vs pipeline vs use-case). If it runs outside the pipeline, it cannot populate a pipeline-level field. Also: check whether exception types are distinguishable at the call site before naming a specific failure mode in a warning message.
- Confidence: high

**2026-06-26 — E0b iterative-review: _TERMINAL_STATUSES has 5 independent definitions — always enumerate all when adding a new terminal state**
- Observation: Adding `FAILED_EXPIRED` to `JobStatus` enum would have been broken without updating `_TERMINAL_STATUSES` in all 5 places: `jobs/store.py:26`, `server/routes_jobs.py:28`, `cli/backup_cmd.py:31`, `cli/export_cmd.py:14`, `cli/collection.py:22`. Three use string literals, two use enum members. Missing even one causes purge bugs or infinite poll loops. The plan originally missed all five.
- Action: When adding a new terminal `JobStatus` value, always grep for `_TERMINAL_STATUSES` and enumerate every occurrence in the task description. This is a known-fragile cross-cutting concern.
- Confidence: high

**2026-06-26 — E0b iterative-review: return type changes on internal functions require full call-chain enumeration**
- Observation: `acl.read_acl_sidecar()` return type was changing but the plan only listed `pipeline.py` as a caller to update. The actual caller chain is `read_acl_sidecar → resolve_acl → pipeline.py`. `pipeline.py` never calls `read_acl_sidecar` directly — it calls `resolve_acl`. A tuple return from `read_acl_sidecar` would make `resolve_acl` return a tuple where callers expect `list[str] | None`, with no TypeError (tuples are truthy) — silent wrong behavior. Also: `resolve_acl` has a front-matter early-return path that bypasses `read_acl_sidecar` entirely and also needs updating.
- Action: For any return-type change, grep ALL callers of the function, trace the full call chain, and check every branch within intermediate functions.
- Confidence: high

**2026-06-26 — E0b iterative-review: maintenance loop restructuring must respect variable ordering in the existing loop body**
- Observation: The plan said to change the maintenance loop age filter from `continue` to a FAILED_EXPIRED transition. But `retry_key` (needed to identify and dedup the job) is computed AFTER the age filter in the current code. An implementer following the plan naively would transition to FAILED_EXPIRED with no dedup key and no retry count check. The plan needed an explicit implementation note: move `retry_key` computation BEFORE the age filter.
- Action: When restructuring a loop that has ordering-dependent variable initialization, always read the existing code and note which variables must be computed before the new branch point.
- Confidence: high

**2026-06-26 — E0b plan-maker-for-team: inline investigation fallback when subagents don't self-report**
- Observation: Six investigation subagents were launched in parallel. Two (contracts, architecture) self-reported rich findings. Four others (scenarios, backend, frontend, tester) went idle without reporting despite repeated nudges. Fell back to inline investigation (reading key files directly) and synthesised all four areas inline. Total additional time ~10 min; plan quality was not degraded.
- Action: When a subagent sends only `idle_notification` without content after 2 nudges, stop waiting and do the investigation inline. `SendMessage` nudges are not guaranteed to elicit responses; treat 2 failed nudges as a fallback trigger.
- Confidence: high

**2026-06-26 — E0b plan-maker-for-team: telemetry truncation was already implemented (L11 scope reduction)**
- Observation: The brief described L11 as "instead of dropping, truncate result_doc_ids." Investigation found `writer.py:_truncate_to_fit()` (lines 202–240) already does exactly this (implemented in D8). The actual work for L11 is smaller: add `truncated_count` to `TelemetryStatsResponse` and count in `reader.compute_stats()` — no writer changes needed.
- Action: Before scoping any L# work item, grep the implementation area first — prior deliverables (D1–D9) often already address part of the brief. Never scope from the brief alone without codebase verification.
- Confidence: high

**2026-06-26 — E0a T-2 close-out: iterative-review caught a real accuracy issue in architecture docs**
- Observation: The initial 110 catalog entry used "to plain text" which was inaccurate for docling's `export_to_markdown()` path. The iterative review with 3 DA agents + Brooks-Lint surfaced this as a Major issue and identified the fix: use "Extract text" (generic, accurate for all paths). The review also caught that exhaustive inline extension enumeration would drift — trimmed to the catalog's established abstraction level.
- Action: When updating architecture catalog entries that enumerate format support, use verb "Extract text" not "plain text" or "Markdown" — both are path-specific and inaccurate in the general case. Keep catalog entries at the abstraction level of other entries in the same table (one row = purpose + key symbols, not routing implementation detail).
- Confidence: high

## What Has Failed

**2026-06-26 — implement-all T-1: subagent stops after review without committing or checking off plan**
- Observation: Two consecutive T-1 subagents ran iterative-review, stated "proceeding to Step 5 (commit) / Step 6 (plan checkoff)", then terminated without actually executing those steps. The checkbox remained `- [ ]` and no new commit appeared.
- Action: Subagent prompt must require the agent to paste the output of `grep "\- \[x\]" <plan>` and `git log --oneline -1` as proof before declaring done. Stated intent is not execution.
- Confidence: high

## What Has Worked

**2026-06-26 — E0a T-1: Brooks-Lint review of office/tsv smoke test — verify docstring routing claims against parser.py before trusting them**
- Observation: `test_e0a_office_tsv_smoke_t1.py`'s `.tsv` test docstring repeats the "already fell through to the else-branch, explicitness change only" framing that the logged learning (same date) records as flagged by all four prior reviewers. `parser.py:36` now lists `.tsv` explicitly in `_PLAIN_EXTENSIONS` with a clarifying comment — the docstring's causal/historical claim is the disputed one and outlives the code. Also: `_assert_ingest_completed_cleanly`'s `status == "DONE"` assertion is dead because `ingest_file_via_path` (conftest.py:167-172) already polls to DONE and `pytest.fail`s on FAILED before returning; only the `error`-field check is live. The `.eml`/`.epub` tests lack the backend guard (`importorskip`) the `.xlsx` test has, so a degraded markitdown backend could pass green (Coverage Illusion).
- Action: When Brooks-reviewing a smoke/integration test, (1) cross-check any docstring claim about extension routing against `parser.py`'s actual `_PLAIN_EXTENSIONS`/`_OFFICE_EXTENSIONS` and the logged markitdown learnings — never trust the docstring's history; (2) check whether a re-fetch-and-assert-status helper is reachable given the conftest helper already gated on that status; (3) flag missing per-format backend guards as T5 when sibling tests guard symmetrically.
- Confidence: high

**2026-06-26 — E0a BE-3: Empirical markitdown testing is mandatory before documenting format support**
- Observation: `.rtf` is in `_OFFICE_EXTENSIONS` (routes to markitdown), but empirical test showed markitdown returns raw RTF control codes (`{\rtf1\ansi...}`), not extracted text — useless for search. Static code review missed this entirely; three independent DA agents also missed it until a direct `MarkItDown().convert()` call was run. `.eml` also routes to markitdown but returns readable RFC 822 text (headers + body), which IS useful. `.svg` falls through to `_parse_plain` and produces readable XML, not garbled output — the source comment itself says "more appropriate". These three formats all needed different documentation despite similar routing paths.
- Action: For any format listed in `_OFFICE_EXTENSIONS` or falling through to `_parse_plain`, always run `uv run python -c "from markitdown import MarkItDown; r = MarkItDown().convert(path); print(repr(r.text_content[:100]))"` before writing documentation claims about output quality. Never infer quality from extension-set membership alone.
- Confidence: high

**2026-06-26 — E0b T-1: Dead config mutation in e2e tests using full mocks — remove or replace**
- Observation: `test_e2e_search_expansion_failure_warning` set `cfg.hyde.enabled = True` with a comment "Enable HyDE so resolve_hyde_vector is called." Both claims were wrong: (a) the route calls `resolve_hyde_vector` whenever `body.rag_fusion=False`, regardless of `config.hyde.enabled`; (b) the test mocked `resolve_hyde_vector` entirely, so the config parameter is never read. The mutation had zero effect on test outcome. All 4 reviewers (3 DA + Brooks-Lint) independently caught this in Cycle 1.
- Action: When mocking a function entirely (via `patch(..., new=AsyncMock(...))`), any test setup that would only matter if the real function ran is dead code. Remove it or replace the mock with a lower-level stub that lets config flow through.
- Confidence: high

**2026-06-26 — E0b T-1: Place all assertions inside `with patch(...)` when using synchronous TestClient**
- Observation: The initial draft placed assertions outside the `with patch(...)` block. Since `TestClient.post()` is synchronous (the response is buffered by the time it returns), the assertions technically work outside the block, but this pattern is fragile — an async refactor or switch to `httpx.AsyncClient` would break it silently. All reviewers flagged it as a Major structural issue.
- Action: When using `with patch(...)` in synchronous TestClient tests, always place the HTTP request call AND all assertions inside the `with` block. There is no downside; it eliminates fragility and makes the scope of the mock explicit.
- Confidence: high

**2026-06-26 — E0b BE-6: Scope boundary — pipeline unpack-only change is intentional; mcp_schemas.py belongs to BE-7**
- Observation: `read_acl_sidecar()` and `resolve_acl()` return type changed to `tuple`. `pipeline.py` was updated to unpack but discard `_acl_warnings` (prefixed with `_` to signal intent). All four reviewers (3 DA + Brooks-Lint) flagged this as Critical/Major, but these findings were out-of-scope for BE-6 — BE-7 explicitly owns the "collect into IngestResult.warnings" step and "update mcp_schemas.py". The only real actionable Moderate finding was a missing test: no test verified that `resolve_acl()` correctly propagates the warning from `read_acl_sidecar()` on the oversized path.
- Action: When a tuple return change spans two tasks (entity-level signature in BE-6, use-case collection in BE-7), add a `# BE-N: wire X into Y` comment at the discard site and a test that verifies the tuple propagation through any intermediate function (here: `resolve_acl → read_acl_sidecar`). The test is cheap and catches future refactors that accidentally swallow the warning.
- Confidence: high

**2026-06-26 — E0a BE-3: Extras names vs transitive packages are different things — don't confuse them in docs**
- Observation: The initial extras note said `(e.g. mammoth for .docx, openpyxl for .xlsx, olefile for .msg)` calling these "extras declared in pyproject.toml." A DA reviewer correctly flagged that `mammoth`, `openpyxl`, `olefile` are transitive packages, not the extra names. The actual extras in pyproject.toml are `[docx]`, `[xlsx]`, `[outlook]`. Users grepping pyproject.toml for `mammoth` would find nothing. The fix: name the actual extras spec (`markitdown[docx,pptx,xls,xlsx,outlook]`) and describe the transitive packages as "required backends... transitively installed."
- Action: When documenting dependency extras, name the extra spec exactly as it appears in pyproject.toml, and separately describe transitive packages as "transitively installed by" those extras. Never call a transitive package an "extra."
- Confidence: high

**2026-06-26 — E0a BE-3: doc/ppt/odt "fall through to plain-text fallback" is correct; "rather than an error" is wrong**
- Observation: The `.doc`/`.ppt`/`.odt` note initially said "producing garbled binary output rather than an error." The source comment at `parser.py:43` says markitdown "raises UnsupportedFormatException" for these formats. The phrase "rather than an error" falsely implies markitdown wouldn't raise — these formats are excluded from `_OFFICE_EXTENSIONS` precisely to prevent that exception. The correct phrasing: "excluded from the Office handler (markitdown would raise `UnsupportedFormatException`)."
- Action: When documenting why an extension was excluded from a handler set, cite what the underlying library would do (raise X) separately from what archon-search actually does (fall through to fallback). Never conflate the two.
- Confidence: high

**2026-06-26 — E0a BE-2: Declare markitdown extras explicitly — don't rely on transitive incidental coverage**
- Observation: `.xlsx` and `.pptx` converters worked in the dev environment only because docling's transitive deps (openpyxl, python-pptx) happened to be installed. The markitdown dep had no `[xlsx]` or `[pptx]` extras declared. Brooks-Lint correctly identified this as a "works by accident" dependency. The fix was `markitdown[docx,pptx,xls,xlsx,outlook]` to make the dependency contract explicit.
- Action: When adding a new format via a library that uses optional extras, declare ALL needed extras explicitly. Never assume a format works because it passes in the current environment — check if the required backend is actually pulled by the declared dep spec.
- Confidence: high

**2026-06-26 — E0a BE-2: Empirical testing beats static analysis for markitdown format support**
- Observation: Multiple reviewers (including Brooks-Lint) incorrectly claimed `.rtf` and `.eml` have no markitdown converter and would fail at runtime. Static analysis of converter registration showed only the Azure-cloud converter for these. But empirical testing (`MarkItDown().convert()` with real files) showed both work via Magika content detection routing to `PlainTextConverter`. The static analysis missed the content-detection layer. The hallucination survived 3 review cycles until the empirical test settled it.
- Action: For any "format X is unsupported" claim from a reviewer, run a direct empirical test before accepting the finding. `uv run python -c "from markitdown import MarkItDown; m=MarkItDown(); r=m.convert(path); print(r.text_content)"` is the definitive test. Reviewer findings about third-party library behavior require verification — not just their word.
- Confidence: high

**2026-06-26 — E0a BE-2: `olefile` as standalone dep vs via markitdown extras — prefer extras mechanism**
- Observation: BE-1 added `olefile>=0.46,<1` as a standalone dep because markitdown's `.msg` support needed it. BE-2 superseded this by including `[outlook]` in the markitdown extras spec, which pulls olefile transitively. The extras mechanism is the correct approach — it keeps the dep chain encapsulated in markitdown's own contract, so if markitdown ever changes its `.msg` implementation, the extras update automatically.
- Action: When a dep is only needed as a backend for another library's format support, prefer the library's optional extras mechanism over declaring the backend directly. Only use explicit declaration when the extras mechanism doesn't exist or the version pin needs to be stricter.
- Confidence: high

**2026-06-26 — E0a BE-1: markitdown's optional-extra deps require explicit core declarations**
- Observation: `markitdown`'s `.msg` converter uses `olefile` internally, but `olefile` is only in markitdown's `[all]` optional extra — not a hard transitive dep (`uv pip show markitdown | grep Requires` confirmed). The plan said "if extract-msg is optional, add it explicitly." The real dep was `olefile`, not `extract-msg`. Without explicit declaration, `.msg` ingestion fails on a fresh install despite markitdown being installed.
- Action: For any dep that claims format support via optional-extra helpers, always verify the actual transitive dep tree with `uv pip show <pkg> | grep Requires` before declaring the dep done. The plan's "verify the transitive dep chain" instruction is non-optional — it may reveal a different package than the plan names.
- Confidence: high

**2026-06-26 — E0a BE-1: Version specifier floor should match the tested version, not an earlier floor**
- Observation: The plan said `markitdown>=0.1.0` but the implementer had only tested against 0.1.6. Using `>=0.1.0` admits untested versions. Reviewers (C1) flagged this as Major. The correct floor is the version verified to work (`>=0.1.6`), plus an upper bound `<0.2` for pre-1.0 libraries per project convention.
- Action: Set the version floor to the exact version verified in the current environment, not a guess at the earliest version that might work. For pre-1.0 libraries, always add `<X.0` upper bound matching the project's convention (`fastmcp>=3.4,<4`, `python-json-logger>=2.0,<3`).
- Confidence: high

**2026-06-26 — E0a BE-1: pyproject.toml dep tests should assert the full version spec string, not just presence**
- Observation: Initial test only checked `"markitdown" in dep` — this would pass for `markitdown<0.1.0` or `markitdown>=999`. The sibling test pattern (`test_multilingual_extra_declared`) asserts the full version string (`"fasttext-wheel>=0.9.2" in pkg`). The upper bound assertion was added only after reviewers flagged it as Moderate in cycle 2.
- Action: When writing a dep-declaration test, always assert the full version specifier string (e.g., `"markitdown>=0.1.6,<0.2" in dep`), not just the package name substring. This matches the established pattern and enforces both floor and ceiling.
- Confidence: high

**2026-06-26 — E0a K1: TypeSpec contract seam accuracy — union-return vs raise/except is the #1 source of contract inaccuracy**
- Observation: The `document-parser-contract.tsp` modeled `parse()` as returning a `ParseResult` union (success|failure). The actual Python seam raises `ParseError` on failure and is async — callers use `try/except`, not union dispatch. All four reviewers (3 DA + Brooks-Lint) independently flagged this as the primary finding. The plan prose correctly said "raises ParseError" but the linked `.tsp` artifact said the opposite.
- Action: Whenever a TypeSpec contract is written for a Python exception-raising seam, add a prominent impedance-note header explaining that the union is a logical documentation model only — Python uses raise/except, never returns the failure variant. Do this at the contract-authoring step, not during review.
- Confidence: high

**2026-06-26 — E0a K1: Scenario traceability completeness — a scenario must appear in ALL four locations**
- Observation: Adding S7 to only the scenario table left it orphaned. Reviewers (both cycles) required S7 to appear in: (1) scenario table, (2) Tester allocation table, (3) the completing task's `completes` line, and (4) the Backend "Done when" checklist. Missing any one of the four creates a traceability gap that iterative-review reliably flags as Major.
- Action: When adding a new scenario to a plan, always propagate it to all four locations in the same edit. Use a mental checklist: scenario table → allocation table → task `completes` → "Done when" checklist.
- Confidence: high

**2026-06-26 — E0b BE-7: MCP responses are SSE-formatted — add Accept header and parse `data:` lines**
- Observation: MCP tool-call responses are `text/event-stream` (SSE), not plain JSON. Without `"Accept": "application/json, text/event-stream"` on the HTTP request, the server returns 406 Not Acceptable. Even with the header, calling `response.json()` on the raw body fails — the payload is a `data: {...}` prefixed SSE line. Must parse manually: iterate `response.text.splitlines()`, find lines starting with `"data: "`, and `json.loads(line[len("data: "):])`
- Action: For any integration test hitting the MCP endpoint directly (not via MCP client), always (1) include `"Accept": "application/json, text/event-stream"` in the headers dict, and (2) parse the SSE response by splitting on `"data: "` prefix rather than calling `.json()`.
- Confidence: high

**2026-06-26 — E0b BE-7: Use `list[str] | None` sentinel to distinguish dispatch path from test-seam path in job tasks**
- Observation: `_default_ingest_task` has two branches: the real dispatch path (calls `_dispatch_ingest()`) and a `pipeline_fn` test-seam path. Initial implementation set `ingest_warnings = []` and overwrote `job.result` on BOTH paths, corrupting pre-existing test assertions. Fix: initialize `ingest_warnings: list[str] | None = None`; only the dispatch path sets it to a real list; the `_finalize_ingest_done()` helper only calls `store.update(..., result={"warnings": ingest_warnings})` when `ingest_warnings is not None`.
- Action: When adding job-result storage to a task coroutine that has a test-seam bypass (`pipeline_fn` override), use `None` as sentinel (not `[]`) and guard the `store.update()` on `is not None`. Otherwise the bypass path silently overwrites result state set by the test fixture.
- Confidence: high

**2026-06-26 — E0a K1: `pipeline.py:27` is an import line, not the call site**
- Observation: The plan cited `pipeline.py:27` as "the caller" for the ParseError seam. Line 27 is `from archon_search.parser import DocumentParser, ParseError`. The actual call/handler is `pipeline.py:296` (`await self._parser.parse(path)`) inside a `try/except ParseError` block. Brooks-Lint caught this as Moderate in Cycle 2. Always verify line citations by reading the actual file.
- Action: When writing a plan that references a specific line number as a "caller" or "handler", verify by reading that exact line. Import lines are not callers.
- Confidence: high

**2026-06-26 — MIS T-4: project close-out**
- Observation: Moving completed brief+plan files from `Documentation/Backlog/` to `Documentation/Completed/` is a universal project convention — every completed feature does this, but it is easy to forget during close-out tasks that are otherwise focused on code or test cleanup.
- Action: Make `mv Documentation/Backlog/<feature>-*.md Documentation/Completed/` an explicit checklist item in every T-4/close-out task; do not consider a feature closed until the files are moved.
- Confidence: high

**2026-06-26 — MIS T-4: `docker compose down --volumes` removes ALL declared named volumes, not just those for named services**
- Observation: `docker compose down --volumes archon-dev archon-test` removes ALL top-level declared volumes in `docker-compose.yml`, including `archon-prod-data` (which is declared even if `archon-prod` is not running). Using `--volumes` in a test teardown fixture silently destroys prod data volumes.
- Action: In test fixture teardown that targets a subset of services, use `docker compose stop <services>` + `docker compose rm -f <services>` + `docker volume rm <specific-volume-names>` instead of `down --volumes`. Never use `--volumes` unless the intent is to destroy ALL declared volumes.
- Confidence: high

**2026-06-26 — MIS T-4: Data isolation assertion should use a search query returning zero results, not just collection-list absence**
- Observation: The spec for the data isolation test says to "search archon-test for it, assert zero results." The initial implementation only checked collection-list absence. A stronger assertion is a POST /search that proves both metadata AND data are isolated — collection-list absence only tests metadata.
- Action: For data isolation e2e tests, always include a search-based assertion (POST /search returning 404 or result_count == 0) in addition to or instead of a collection-list check. The search path exercises a different code path and matches the spec more precisely.
- Confidence: high

**2026-06-26 — MIS T-4: Integration test coverage for cross-links and doc-index prevents silent regression**
- Observation: Cross-links (e.g., `09_multi_instance_setup` referenced from `03_running_the_server.md` and `08_running_with_docker.md`) and doc-index entries can be silently removed during future edits with no CI signal. Adding assertions to `test_compose_lint.py` catches these regressions immediately.
- Action: For every new user-manual file, add three tests to `test_compose_lint.py`: (1) doc-index contains the filename, (2) related cross-link files contain the filename. These are simple substring assertions that pay off across the entire project lifetime.
- Confidence: high

**2026-06-26 — MIS T-4: Action items buried in descriptive edge-case notes go stale and untracked**
- Observation: The D8 brief's "Salt co-location — scope of protection" bullet ends with "Document this explicitly in `150_security_and_privacy_architecture.md`" — an imperative embedded inside a descriptive note. Brooks-Lint C1-B-2 flagged this as a Suggestion: a reader cannot tell whether the action was done or remains open without reading the architecture doc.
- Action: Never end a design-decision note with an unresolved imperative. Either verify the referenced doc was updated and strike the instruction, or promote it to an explicit open item or tracked follow-up task.
- Confidence: high

**2026-06-26 — MIS BE-4: Docker image baked-in env vars make compose env declarations cosmetic — always verify against the Dockerfile**
- Observation: Part 7 (fastembed model cache section) had Step 1 (uncomment `FASTEMBED_CACHE_PATH` in compose) presented as functionally required. But `08_running_with_docker.md` confirmed the image already bakes in `FASTEMBED_CACHE_PATH=/data/fastembed-cache`. Step 1 is purely cosmetic — the real required steps are volume mount + volume declaration (Steps 2+3). Two independent DA reviewers and Brooks-Lint all caught this as Major.
- Action: Before writing a compose "uncomment step" for an env var, check whether the image already sets that value. If the Dockerfile or the env var table in a sibling doc shows "baked into image", add a note that the step is for clarity only. Never present it as required unless it sets a non-default value.
- Confidence: high

**2026-06-26 — MIS BE-4: "declared but not mounted" Docker Compose error claim is false — only "mounted but not declared" fails**
- Observation: Part 7 initially stated docker compose "rejects the config if the volume is mounted but not declared, or declared but not mounted." The second clause is wrong — Docker Compose accepts a declared-but-unmounted volume without error. Two reviewers caught this as Moderate. The correct error condition is mounted-but-not-declared only.
- Action: For any compose documentation that describes error behavior, verify the actual error condition. Docker Compose does not reject unused volume declarations.
- Confidence: high

**2026-06-26 — MIS BE-4: "all three steps required" vs "Step 1 optional" is a contradiction that reviewers immediately flag**
- Observation: The section initially stated "All three steps must be done together" in the intro, then added a Step 1 note saying "The functionally required steps are Steps 2 and 3." These contradict each other and were flagged as Minor by Brooks-Lint. The fix: make the intro precise upfront — "Steps 2 and 3 are required; Step 1 is optional but recommended for clarity."
- Action: When a section intro says "all X steps required" and a sub-note says "only Y steps are required", resolve at the intro level. Never let the intro and the sub-note tell different stories.
- Confidence: high

**2026-06-26 — Iterative-review on plan docs: C1 fixes must propagate to ALL cross-referencing sections**
- Observation: During /iterative-review of e0a-file-type-completeness-team-plan.md, the C1 fix for the integration test claim (F2) corrected the allocation table but left a stale "unit + integration tests" in the Contracts seam section. Both cycle-2 DA agents independently flagged the same stale cross-reference as Major. Propagation misses are the #1 source of cycle-2 findings in plan doc reviews.
- Action: After any fix that changes a test strategy claim, search the entire document for other occurrences of the same phrase or concept (grep for "integration test") and update every instance. Plan docs reference the same fact in multiple places (allocation table, contracts seam, Backend scope summary).
- Confidence: high

**2026-06-26 — Iterative-review on plan docs: `.tsv`-style no-op changes need explicit "cosmetic" labelling**
- Observation: The e0a plan added `.tsv` to `_PLAIN_EXTENSIONS` and described it as a functional change. Direct code inspection revealed `.tsv` already routes via the `else` branch — adding it to the set is a no-op. All four reviewers flagged this independently. The test would have passed on unmodified code, giving false coverage confidence.
- Action: Whenever a scope item modifies a lookup set for an extension that already routes via a catch-all else-branch, label it explicitly as "explicitness/documentation only — no behavior change." This prevents misleading scope descriptions and tautological tests.
- Confidence: high

**2026-06-26 — Iterative-review on plan docs: version placeholders like `>=X` are plan blockers**
- Observation: The e0a plan used `markitdown>=X` as a literal placeholder. All four reviewers flagged it as Major — BE-1 cannot be implemented as written, and the resolved version determines whether the feature's stated goal (all 8 formats supported) holds. Unresolved version floors are the plan equivalent of a `TODO` that blocks the task.
- Action: Never ship a plan with a version placeholder. Either resolve to a real floor (e.g. `>=0.1.0`) with a verification note, or block the plan as draft until the version is confirmed.
- Confidence: high

**2026-06-25 — MIS BE-2: `.env.example` "copy and run" contract — active placeholder lines must be safe or clearly marked**
- Observation: Setting `ARCHON_SEARCH_IMAGE=ghcr.io/user538295/archon-search:TAG` as an active (uncommented) line causes `docker compose up` to fail with `manifest unknown` if copied verbatim to `.env`. The task spec said "uncommented example value" but didn't account for the copy-and-run failure mode. DA review (C1) caught this as Major. Fix: add a "REQUIRED before first use: replace TAG" comment immediately before the line, and describe what the placeholder represents. The file header "copy this file to `.env` and edit the values" implies active lines should either work or be unmistakably marked as requiring substitution.
- Action: For any `.env.example` active line with a placeholder value, add a prominent "REQUIRED: replace X" comment immediately before it. Never rely on a comment buried in a multi-line block above — operators skim.
- Confidence: high

**2026-06-25 — MIS BE-2: "auto-generates on every fresh start" is factually wrong for persistent-volume containers**
- Observation: `key_manager.load_or_generate_key()` checks env var → file → generate. With a persistent Docker volume the key file persists across container recreates, so auto-generation only happens on first start. Writing "auto-generates on every fresh start" misleads operators into thinking their key rotates on every `docker compose up`, causing over-provisioning of tokens. Verified at `key_manager.py:437-447`.
- Action: For any documentation describing key/secret generation behavior in Docker containers, always verify against the actual load-then-generate logic. The correct phrasing is "auto-generates on first start; subsequent starts read the existing key from the data volume."
- Confidence: high

**2026-06-25 — MIS BE-2: Plan's manual integration tests belong in existing test files, not as one-off grep commands**
- Observation: The plan listed `verify_env_example_registry` and `verify_env_example_no_active_api_key` as manual developer self-checks. `test_compose_lint.py` already had `.env.example` tests; adding the acceptance criteria there cost 30 lines and provides permanent CI coverage. DA/Brooks-Lint independently flagged the manual-only tests as Moderate.
- Action: For any plan task with integration self-checks described as "grep X and confirm Y," check whether a test file already exists for the target file (here `test_compose_lint.py`). If so, add the checks there rather than leaving them as manual commands. The test file is the right long-term home.
- Confidence: high

**2026-06-25 — MIS BE-1: Doc-only tasks accumulate Critical issues when the doc assumes sibling tasks are already done**
- Observation: BE-1 assumed BE-2 (.env.example update) was already complete — the draft said the registry path "ships with the real registry path as the default," but `.env.example` had a commented-out local-build line. A user copying the file would get a broken image placeholder. The C1 review caught this immediately. For any doc task that references a future state of another file, write for the CURRENT state and add explicit manual steps.
- Action: Before writing doc instructions that reference another file's content (e.g., `.env.example`, `docker-compose.yml`), open the actual file and verify the current state. Write instructions for what exists now, not what a sibling task will produce.
- Confidence: high

**2026-06-25 — MIS BE-1: API field names in doc examples must be verified against actual serialization code**
- Observation: The async job polling example used `"id"` (Critical) and `"COMPLETED"` (Critical) — neither exists in the actual API. The correct field is `"job_id"` (from `job_to_dict()` in `jobs/model.py`) and the terminal status is `"DONE"` (from `JobStatus.DONE` in `types.py`). These were caught in C3, costing a full extra review cycle.
- Action: For any doc example that parses a JSON API response, always verify the field names and enum values by reading the actual serialization code (`job_to_dict`, `schemas.py`, enum definitions) before writing. Never infer field names from type hints alone — the serializer may rename fields.
- Confidence: high

**2026-06-25 — MIS BE-1: Isolation tables create an implicit "completeness contract" — missing rows are false negatives**
- Observation: The initial isolation table listed Data directory, API key, LanceDB index, port, and MCP endpoint. Reviewers (C2-A-1/A-2) correctly flagged that `ARCHON_SEARCH_CONFIG` and `FASTEMBED_CACHE_PATH` are NOT derived from `DATA_DIR` — the table implied DATA_DIR controlled everything. When writing an isolation table for a multi-instance guide, the table MUST include all boundaries, especially exceptions that break the general pattern.
- Action: For any "each instance has its own" table, grep the codebase for all env vars that affect paths (DATA_DIR, KEY_FILE, CONFIG, FASTEMBED_CACHE_PATH) and verify which ones are independent. The exceptions are the most important rows to include.
- Confidence: high

**2026-06-25 — D8 T-4: ADR append-only rule means restoring original body after an erroneous edit, not finding a creative middle ground**
- Observation: When Cycle 1 edited the ADR-05 Decision body (line 37 rewritten to "mitigated by D8") and Cycle 1 also applied a strikethrough to the Negative consequences (line 58), the Cycle 2 fix was unambiguous: restore the original accepted text verbatim. The append-only rule ("supersede with a new ADR rather than editing accepted ones") means the original body is a frozen historical record — the Amendment section at the end provides all D8 context. No creative middle ground (partial strikethroughs, "see Amendment below" annotations) satisfies the rule; only a full restore does.
- Action: When an ADR body has been incorrectly edited, restore it to the exact pre-edit text. If the Amendment provides the update, it stands alone — cross-referencing the Amendment from within the accepted body is itself an edit. Verify the restore by checking the file line count and tailing the end to confirm the Amendment is still appended.
- Confidence: high

**2026-06-25 — D8 T-4: Close-out doc scope expands beyond the plan checklist — grep for stale claims across user-facing docs**
- Observation: The T-4 plan checklist named 11 specific files, but iterative review found gaps in README.md ("planned for a future release"), UserManual/06_telemetry.md (no mention of hash_doc_ids), UserManual/02_configuration.md (missing [telemetry] field), 110_component_catalog (missing hasher.py), and 600_api_reference (missing TelemetryStatusDetail). None of these were in the original checklist.
- Action: For any feature close-out, run `grep -r "planned\|roadmap\|future release" Documentation README.md` against the feature's key terms to catch docs not in the plan checklist. The checklist is a starting point, not a complete inventory. Pay special attention to UserManual/ and README.md — they are the operator's first contact and are often missed in dev-authored checklists.
- Confidence: high

**2026-06-25 — D8 T-4: DA review hallucinations need source verification before spawning a fix agent**
- Observation: DA1 C3 flagged "load_or_create_salt signature missing salt_path parameter" as Major, recommending adding a non-existent parameter to two docs. The actual function signature at `hasher.py:49` has only `hash_doc_ids_enabled: bool` — no `salt_path`. Acting on this finding would have introduced a false claim into security documentation.
- Action: Before spawning a fix agent for any DA finding about a function signature or API surface, verify the claim against the actual source code with a targeted `grep -n "def <function>"`. A "Major" severity label does not mean the finding is correct. DA agents are good at finding gaps but can hallucinate specific technical details.
- Confidence: high

**2026-06-25 — D8 T-3: caplog is required for WARNING/ERROR assertions in e2e tests that exercise lifespan startup logging**
- Observation: S3 and S5 plan specs explicitly require "WARNING is logged" and "ERROR is logged" from `load_or_create_salt`. The initial test implementation checked only file existence and JSONL flags, missing the log requirement entirely. `caplog.at_level(logging.WARNING, logger="archon_search.telemetry.hasher")` wrapping the `with make_real_app(...)` block captured lifespan startup logs correctly.
- Action: For any e2e test whose spec says "X is logged", add `caplog` as a fixture parameter and wrap the server context with `caplog.at_level(level, logger=specific_logger)`. Always assert on `r.getMessage()` content, not just `r.levelno`.
- Confidence: high

**2026-06-25 — D8 T-3: `search_ok_all[-1]` vs `search_ok_all[before_count]` for isolating session-2 JSONL entries**
- Observation: S4 test accumulates telemetry from both sessions in the same log dir. Using `before_count` (count of search/ok entries before session 2's search) and then `search_ok_all[-1]` was flagged by Brooks-Lint as incorrect when session 1 emits more than one entry — `[-1]` picks the last overall entry which may not be from session 2. The correct pattern is `search_ok_all[before_count]` (first entry at index `before_count` is the first one session 2 wrote).
- Action: When two app sessions write to the same log dir, always use index-based slicing (`entries[before_count]` or `entries[before_count:]`) rather than `[-1]` to isolate entries from the later session.
- Confidence: high

**2026-06-25 — D8 T-3: `os.getuid()` is POSIX-only — use `getattr(os, "getuid", lambda: -1)()`**
- Observation: `@pytest.mark.skipif(os.getuid() == 0, ...)` crashes at collection time on Windows with `AttributeError`. Brooks-Lint C1-B-1 caught this. The portable form is `getattr(os, "getuid", lambda: -1)() == 0`.
- Action: Any pytest `skipif` that checks UID for root must use `getattr(os, "getuid", lambda: -1)() == 0`, not `os.getuid() == 0`, to avoid crashing test collection on non-POSIX platforms.
- Confidence: high

**2026-06-25 — D8 T-3: S5 JSONL fallback assertion requires cross-validating actual doc_id values, not just the boolean flag**
- Observation: Asserting `doc_ids_hashed is False` alone passes vacuously if result_doc_ids is empty or if the flag/value are desynced. Strengthening required computing the raw SHA-256 doc_id (`hashlib.sha256(str(file.resolve()).encode()).hexdigest()`) and asserting it appears in `result_doc_ids`. This proves the fallback wrote real raw values, not just set the flag.
- Action: For any S5-style fallback test, always cross-validate the boolean flag with the actual data values. The boolean is necessary but not sufficient.
- Confidence: high

**2026-06-25 — D8 FE-1: When adding HTTP to a CLI command, always add direct unit tests for the fetch helper, not just the Click command**
- Observation: The initial test suite for FE-1 mocked `_fetch_server_status` entirely at the CLI level, leaving all 6 code paths in `_fetch_server_status` (key-resolve exception, httpx.HTTPError, 401, non-200/non-401, invalid JSON, valid 200) untested. DA and coverage reviewers flagged this as Major in cycle 1. Adding 7 direct unit tests for the helper was the right fix: it exercised the real URL construction, header building, and error branches without going through Click.
- Action: For any CLI command that encapsulates an HTTP call in a helper function, always add direct unit tests for the helper in ADDITION to the Click-level tests. The Click-level tests verify rendering logic; the helper tests verify the HTTP contract (URL, headers, error handling). Both are required.
- Confidence: high

**2026-06-25 — D8 FE-1: Distinguish "401 Unauthorized" from "server unreachable" in CLI status commands**
- Observation: `maintenance_cmd.py` silently maps 401 to `None` (treating it as unreachable). This is acceptable there because the command has an offline fallback (reads `.maintenance-state.json`). The `status` command has no offline fallback — a 401 would silently omit the telemetry section with no indication why. Returning `{"_auth_failed": True}` sentinel from `_fetch_server_status` let the caller emit a clear `[401 Unauthorized — check your API key]` message.
- Action: For CLI commands with no offline fallback, distinguish 401 from network errors so the operator sees a clear auth-failure message rather than a silent omission. The `{"_auth_failed": True}` in-band sentinel is a simple mechanism for this pattern.
- Confidence: high

**2026-06-25 — D8 BE-5: Status sub-object tests should call the builder via HTTP, not construct the Pydantic model directly**
- Observation: Tests named `test_telemetry_status_detail_hash_enabled_when_salt_loaded` initially constructed `TelemetryStatusDetail(enabled=True, hash_doc_ids_enabled=True)` directly and asserted the stored values — a tautology that tests Pydantic model construction, not the builder function `_build_telemetry_status()`. A bug in the builder would not be caught. DA and Brooks-Lint independently flagged this as Moderate.
- Action: For any status sub-object test, always exercise the builder via the HTTP layer using `_make_client_with_*_config` + `GET /status`, not by constructing the schema model directly. Direct model construction is valid only for testing Pydantic validation constraints (e.g., field type coercion), never for testing routing logic.
- Confidence: high

**2026-06-25 — D8 BE-5: Always test the S5 fallback scenario (hash_doc_ids=True, salt_bytes=None) explicitly**
- Observation: The S5 fallback (config says hash, but salt unreadable → `salt_bytes=None` → `hash_doc_ids_enabled=False`) is a distinct diagonal from "hashing disabled" (`hash_doc_ids=False`). Only one of the three scenarios (`hash_doc_ids=True, salt=None`) actually exercises the `and salt_bytes is not None` guard. The plan explicitly called out S5 but the initial test set missed the HTTP-level coverage for it.
- Action: For any feature with a fallback path that is neither "fully on" nor "fully off" (config says X but runtime state prevents it), always add a dedicated test for that diagonal. It is the highest-value branch — a future refactor that drops the guard would pass all "fully on" and "fully off" tests.
- Confidence: high

**2026-06-25 — D8 T-1: Collection-not-found returns 404 before telemetry is written in routes_search.py**
- Observation: The single-collection search path in `routes_search.py` checks `meta is None` at line ~215 and returns `JSONResponse(404)` before entering the `try/except` block that enqueues `TelemetryEntry.from_error(...)`. Any e2e test that tries to trigger an error telemetry entry via a nonexistent-collection search will see zero telemetry entries — a silently vacuous test. The `from_error` telemetry block only fires for `asyncio.TimeoutError` or generic `Exception` inside the pipeline call.
- Action: For e2e tests that need a non-search telemetry entry (S6, S16), use the `/explain` endpoint instead. `from_explain_result` fires even on partial success and reliably writes an entry. Document this limitation in test docstrings referencing the specific route code path.
- Confidence: high

**2026-06-25 — D8 T-1: MCP SSE sessions require `notifications/initialized` after `initialize`**
- Observation: FastMCP SSE sessions require a `notifications/initialized` notification after the `initialize` request before any tool calls. Without it, tool calls on the same session return an error. The pattern is: `POST /mcp` with `initialize` → extract `session_id` from SSE stream → `POST /mcp` with `notifications/initialized` (no response expected, status < 400 suffices) → `POST /mcp` with `tools/call`.
- Action: Any test helper that opens an MCP SSE session must send `notifications/initialized` as the second step. Omitting it causes tool calls to fail silently or return errors that look like wiring bugs.
- Confidence: high

**2026-06-25 — D8 BE-4: `if result_doc_ids:` guard is a vacuous-pass trap in MCP hashing tests**
- Observation: MCP search tests that guard format/exclusion assertions with `if result_doc_ids:` pass silently when search returns zero results. The `doc_ids_hashed=True` outer assertion still runs, but it's insufficient on its own — a wiring regression that breaks MCP doc_id hashing would not be caught. DA/Brooks consistently flagged this as Moderate. Fix: replace `if result_doc_ids:` with `assert result_doc_ids` so a zero-results scenario fails loudly.
- Action: For any integration test that asserts "hashing was applied", always require non-empty `result_doc_ids` unconditionally. The `if guard` pattern is acceptable for defensive logging-style tests but never for core correctness assertions.
- Confidence: high

**2026-06-25 — D8 BE-4: Wire new closure param through the full chain: `app.py` → `create_mcp_http_app()` → `create_app()` → tool closure**
- Observation: Adding `doc_id_hasher` to MCP required touches in 3 files (routes_search.py, mcp.py, app.py). The forwarding chain is: `app.state.doc_id_hasher` (set in lifespan) → `create_mcp_http_app(doc_id_hasher=...)` (outer wrapper) → `create_app(doc_id_hasher=...)` (inner FastMCP factory) → captured by the tool closures. Missing any link in this chain silently falls back to `None` (no hashing). The pattern mirrors how `writer` is threaded.
- Action: When adding a new lifespan-injected callable to MCP tools, always thread it through the full chain: `app.state` → `create_mcp_http_app` parameter → `create_app` parameter → closure capture. Unit tests catch a missing link only if you instrument the closure; integration tests (writing JSONL and checking the field) are the strongest proof.
- Confidence: high

**2026-06-25 — D8 BE-4: `hash_doc_ids_enabled` added to `make_real_app` — the right place for new TelemetryConfig options**
- Observation: `make_real_app` in `tests/integration/conftest.py` is the canonical integration-test factory. A local `_make_hashing_app` helper that duplicates it for a single config flag was flagged Moderate by DA+Brooks. The correct fix is to add the parameter to `make_real_app` with a `False` default — mirrors the existing `telemetry_enabled`, `mcp_enabled`, `backup_enabled` pattern. No existing tests are affected because the default is `False`.
- Action: Any new `TelemetryConfig` or `SearchConfig` option needed for integration tests should be added as a keyword param to `make_real_app` in `tests/integration/conftest.py`, not as a local duplicate helper.
- Confidence: high

**2026-06-25 — D8 BE-2: `patch.object(Path, "read_bytes", ...)` is broad — patch at module level instead**
- Observation: Using `patch.object(Path, "read_bytes", side_effect=PermissionError(...))` patches ALL Path instances process-wide. For testing a specific fallback in `hasher.py`, it's more surgical to `patch("archon_search.telemetry.hasher.Path")` and configure only that mock instance's `exists` and `read_bytes`. The DA review (Cycle 2) flagged the broad patch as Moderate.
- Action: When patching I/O failures in a specific module's Path usage, always patch at the module reference (`patch("module.Path")`) rather than the class globally (`patch.object(Path, "read_bytes")`). Reduces test interference risk.
- Confidence: high

**2026-06-25 — D8 BE-2: `hmac.new(...).hexdigest()` vs `hmac.digest(...).hex()` — always prefer the one-shot C API**
- Observation: `hmac.new(salt, msg, hashlib.sha256).hexdigest()` constructs an HMAC object, allocates state in Python, then extracts. `hmac.digest(salt, msg, "sha256").hex()` is the one-shot C-level implementation (Python 3.7+, guaranteed on 3.12). Both produce identical 64-char hex output. The older `hmac.new` form triggered a Brooks-Lint Moderate finding; switching to `hmac.digest` also removes the `hashlib` import.
- Action: For HMAC-SHA256 in new code, always use `hmac.digest(key, msg, "sha256").hex()`. Remove `import hashlib` when `hmac.digest` is the only call site.
- Confidence: high

**2026-06-25 — D8 BE-2: `functools.partial` over closure factory for single-argument adaptation**
- Observation: Initial implementation used `_make_hasher(salt)` closure factory returning `_hasher(doc_id)`. This required a `# noqa: ANN202` suppression for missing return type and added 4 lines. `functools.partial(hash_doc_id, salt)` is equivalent, fully typed, and eliminates the `noqa`. DA and Brooks both flagged the factory as Moderate.
- Action: For adapting a 2-argument pure function to a 1-argument callable by binding a constant first argument, use `functools.partial`. Never write a closure factory for this pattern in new code.
- Confidence: high

**2026-06-24 — D9 T-5: doc close-out review caught false claims in docs OUTSIDE the plan's checklist (520, 600) — the no-false-docs principle overrides the checklist**
- Observation: The plan's documentation checklist enumerated 100/110/120/160/600 + CLAUDE.md + toml.example + ADR + snapshot. But the factual-accuracy DA reviewer found pre-D9 FALSE statements in docs NOT on the checklist: `520_api_design_and_contracts.md` said "MCP tools (ten total)" and `600` said `explain` "Operates in the default namespace only" / export+import "Uses DEFAULT_NAMESPACE" — all contradicted by the now-shipped D9 code (17 tools; all tools use `_get_request_namespace()`). These had to be fixed because they are now-false claims about the closed-out feature, even though 520 was never listed. Brooks-Lint independently caught that the rotate_key MCP-staleness MECHANISM described in 120/160 (and copied verbatim from the plan's own "Known limitations" line 91) was wrong: `middleware_auth.py:78-79` DOES re-read `app.state.api_key`; the real cause is the mounted sub-app's `app.state` never carries `api_key` (only the parent FastAPI app's does, set at `app.py:413` and updated by `routes_keys.py:286`). The plan's own limitation text was imprecise — do not copy plan prose into docs without verifying against source.
- Action: For a doc close-out task, run the factual-accuracy reviewer against the WHOLE doc set's MCP/feature claims, not just the checklist files — a sibling doc (520, 990, UserManual) often carries a stale count or namespace claim. When the plan's "Known limitations" describes a mechanism, verify it against source before propagating it into Architecture docs; plan prose can be wrong.
- Confidence: high

**2026-06-24 — D9 T-5: `tests/contract/openapi_snapshot.json` is UNGUARDED — only `tests/server/openapi_snapshot.json` has a test**
- Observation: The plan checklist (line 266) said regenerate `tests/contract/openapi_snapshot.json`, but `grep -rln "contract/openapi_snapshot" tests/` returns nothing — no test references it (the three files under `tests/contract/` are unrelated shape tests). The ACTIVE guard is `tests/server/openapi_snapshot.json` via `tests/server/test_openapi_snapshot.py::SNAPSHOT_PATH`. The contract file was very stale (8000-line diff) because nothing kept it current. I regenerated BOTH (server via `--update-openapi-snapshot`, contract via a one-off script writing `app.openapi()` with `indent=2, sort_keys=True`) so they are byte-identical; the contract regen is inert (cannot break CI) but satisfies the checklist. The server snapshot was already current on Python 3.12 (BE-9 regenerated it; re-running on 3.12 produced no diff).
- Action: When a plan lists `tests/contract/openapi_snapshot.json`, also regenerate `tests/server/openapi_snapshot.json` (the real guard) on Python 3.12. To match the contract file's format, use `json.dumps(spec, indent=2, sort_keys=True)` after popping `info.version`. The contract copy is dead weight (flagged for future deletion-or-wire-a-test); do not rely on it as a guard.
- Confidence: high

**2026-06-24 — D9 docs: updating Architecture 120/160 for MCP HTTP wiring — resolve #Unverified tags against now-verified facts**
- Observation: Doc 120 carried three stale-by-D9 claims as fact plus one `#Unverified` tag: MCP "not mounted by run_server", "exercised only by tests", `namespaces={}` (default-key only), and "no production path wires a shared writer" (the `#Unverified` one). All four were contradicted by the completed D9 code (mount in `create_app()` lifespan, `namespaces=config.namespaces`, lifespan-passed `writer`+`key_store`). The MCP tools table listed 13; D7 added create/list/revoke/rotate_key (now 17). NOTE (corrected at T-5 review): the "Discrepancy with CLAUDE.md" blockquote about legacy tool names (`search_status` etc.) was REMOVED — CLAUDE.md now lists the correct 17 tool names, so the discrepancy no longer exists; the stale note had to go. The operator-facing rotate_key MCP hot-reload limitation (stale legacy `self._api_key` until restart) belongs in 160's key-rotation runbook, cross-linked from 120.
- Action: When a feature lands, grep the target docs for the OLD behavior phrasing AND any `#Unverified` tag near it — both must be corrected together. Keep separate "discrepancy/known-limitation" notes that remain true. Cross-link operator caveats (160) from architecture prose (120) rather than duplicating.
- Confidence: high

**2026-06-24 — D9 BE-11: full-stack status/health integration tests pass green immediately; review value is isolation + diagnostics**
- Observation: BE-11's three tests (`test_status_mcp_field_present`, `test_status_mcp_field_absent_when_disabled`, `test_health_mcp_field`) passed on first run because BE-8/BE-9 already implemented `_build_mcp_status` and the route wiring. The integration delta over the BE-8/BE-9 unit tests is real: unit tests set `app.state.mcp_bound=True` on a bare TestClient, while `make_real_app(mcp_enabled=True)` runs the real lifespan that actually mounts the MCP sub-app and sets `mcp_bound=True` — so these prove the lifespan→route wiring the unit tests mock out. Iterative review (3 DA + Brooks) surfaced two actionable items: (1) `test_health_mcp_field` reused `tmp_path` across two sequential `make_real_app` calls (LanceDB store reopen risk on CI) — fixed by passing distinct subdirs `tmp_path/"enabled"` and `tmp_path/"disabled"` while keeping the single spec-named test; (2) missing `"mcp" in body` guard before `is None`/dereference — added for clear diagnostics matching the unit-test convention. The mount-failed integration case (`enabled=True, bindAddress=None`) was correctly left out of scope (BE-11 spec = exactly 3 tests; unit tests cover that branch).
- Action: For BE-11-style status/health integration tests, assert `bindAddress == f"{cfg.host}:{cfg.port}/mcp"` against the config object (robust to default changes), add `"mcp" in body` presence guards before value checks, and when a single test must run two `make_real_app` contexts pass distinct `tmp_path` subdirs so the second never reopens the first's LanceDB. Expect green on first run; the review cycle adds isolation + diagnostics, not bug fixes.
- Confidence: high

**2026-06-24 — D9 BE-10: config-example deliverable was already present (committed in BE-2); task reduced to convention fixes**
- Observation: BE-10's `[mcp]` section in `archon-search.toml.example` was already committed as part of the BE-2 mount commit (baaa164), so the task's net work was verification + two consistency fixes surfaced by review: (1) `enabled` was commented `# enabled = true` while sibling feature toggles (`[hyde]`/`[rag_fusion]`/`[telemetry]`) all show `enabled =` uncommented — DA agent 2 (Major) and Brooks (Moderate) disagreed on direction; resolved by following the dominant uncommented `enabled =` convention; (2) the disable-effect comment named only `GET /status` but `routes_health.py:26` also nulls the `mcp` field. No code logic — validated the example parses via `tomlkit.parse` + assert `doc['mcp']['enabled'] is True`.
- Action: For config-example (`toml.example`) tasks, check `git log -- <file>` first — the content may already be committed under an earlier sibling task with the checkbox un-flipped. Validate the example parses with `tomlkit` rather than relying on a test (no test loads `toml.example`). For `enabled` toggles specifically, the file convention is uncommented `enabled = <default>`.
- Confidence: high

**2026-06-24 — D9 BE-9: adding a field to HealthResponse breaks the OpenAPI snapshot — regen is part of the task, not just T-5**
- Observation: Adding `mcp: McpStatusDetail | None` to `HealthResponse` changed `GET /openapi.json` and broke `tests/server/test_openapi_snapshot.py::test_openapi_spec_matches_snapshot` (strict equality). The plan assigned snapshot regen to T-5 close-out, but it is a CI blocker the moment the schema changes — the full suite goes red. Regenerate in the SAME commit with `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot` (Python 3.12 mandatory per CI). Note: `tests/server/openapi_snapshot.json` is the ACTIVE guard (referenced by the test via `SNAPSHOT_PATH`); `tests/contract/openapi_snapshot.json` exists but has NO test referencing it — do not touch it.
- Action: Any task that adds/removes a field on a Pydantic response model included in the OpenAPI surface MUST regenerate `tests/server/openapi_snapshot.json` (Python 3.12) in the same commit, regardless of whether a later close-out task also lists it. A green suite is the Step-4 gate.
- Confidence: high

**2026-06-24 — D9 BE-9: /health route reading app.state.config works with bare TestClient (no lifespan)**
- Observation: `_build_mcp_status` needs `app.state.config` and `app.state.mcp_bound`. Both are safe from a bare `TestClient(app)` (no lifespan): `app.state.config` is set synchronously in `create_app` (app.py:414), and `mcp_bound` is read via `getattr(..., "mcp_bound", False)`. The BE-8 status-test helper pattern (build app, set `app.state.mcp_bound` directly, no lifespan) is the correct deterministic way to exercise the bound/not-bound branches — reused verbatim for /health tests minus auth header and mock store (since /health is unauthenticated and does not read the store).
- Action: For route tests that need specific `app.state` flags set by the lifespan (mcp_bound, model_validation), set them directly on `app.state` after `create_app` and use a bare TestClient — do not open the lifespan. Mirror the existing `_make_client_with_mcp_config` in test_routes_status.py.
- Confidence: high

**2026-06-24 — D9 BE-9: reusing _build_mcp_status across routes — private cross-module import is accepted convention here**
- Observation: `routes_health.py` importing the underscore-private `_build_mcp_status` from `routes_status.py` is endemic and accepted in this codebase (`routes_collections` imports private helpers from `routes_jobs`; `routes_explain`/`mcp.py` import `_FANOUT_VALIDATION_LIMIT` from `routes_search`). DA + Brooks reviewers flagged it Minor but three of four concluded reuse beats duplication. No circular import (routes_status does not import routes_health; app.py imports both independently).
- Action: Within `server/`, reusing a sibling route module's private helper is acceptable over duplicating logic. Only promote to a shared module if the seam grows a third consumer. Do not refactor the convention as a side-effect of a feature task.
- Confidence: medium

**2026-06-24 — D9 BE-8: camelCase JSON field via Pydantic alias; required-and-nullable matters**
- Observation: C3 contract mandates `bindAddress` (camelCase) — the FIRST camelCase JSON field in `schemas.py` (all others snake_case). Achieved with `model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)` + `Field(serialization_alias="bindAddress", validation_alias=AliasChoices("bindAddress","bind_address"))`. `serialize_by_alias=True` (Pydantic 2.11+) makes bare `model_dump()` emit the alias; FastAPI response serialization already uses `by_alias=True`. A nested model's `serialize_by_alias` config IS respected when the parent (StatusResponse, which lacks it) is serialized. Critical nuance: the C3 contract listed `bindAddress` in BOTH `required` AND `nullable: true` — independent dimensions. Using `Field(default=None, ...)` makes the OpenAPI schema mark it OPTIONAL (drops it from `required`), diverging from the contract. To get required-and-nullable, declare `bind_address: str | None = Field(...)` with NO `default` — required (must be passed) but accepts None. The route always passes a value, so this is safe.
- Action: For a required-and-nullable field matching a contract, use `str | None = Field(...)` WITHOUT `default=None`. Adding `default=None` silently makes it optional in the generated OpenAPI. For camelCase JSON keys, the three-part alias machinery (populate_by_name + serialize_by_alias + serialization_alias/validation_alias) is the correct pattern; keep the Python attribute snake_case.
- Confidence: high

**2026-06-24 — D9 BE-8: status sub-objects must reflect ACTUAL state, not config intent**
- Observation: `_build_mcp_status` initially returned the configured `host:port/mcp` whenever `config.mcp.enabled`, even though `app.py` swallows MCP mount failures (`try/except ... "continuing without MCP"`). This made `/status` report MCP as bound when the mount had actually failed — a false-positive monitoring surface. Fix: add `app.state.mcp_bound` (set False before the mount try-block, True only after a successful `app.mount`), and derive `bindAddress = f"{host}:{port}/mcp" if bound else None`. The C3 contract's `nullable: true` bindAddress with "null when ... not yet bound" exists precisely to express this state. Init `app.state.mcp_bound = False` BEFORE the `if config.mcp.enabled:` block (unconditional), mirroring `backup_loop`/`maintenance_loop`/`model_validation` siblings — not inside the conditional.
- Action: When a /status sub-object reports a subsystem that can fail to start, derive its "available/bound" field from a real `app.state` flag set at the success point, never from config intent alone. Initialize the flag unconditionally at the top of the lifespan (like sibling status objects), and read it in the route with `getattr(request.app.state, "flag", False)`.
- Confidence: high

**2026-06-24 — D9 T-4: tester e2e telemetry test — iterative review added meaningful assertion depth**
- Observation: T-4 passed immediately on first run (expected for tester close-out tasks). The iterative review cycle across 2 cycles added: (1) `result_count >= 1` + `result_doc_ids` non-empty + specific `sha256(doc_file.resolve())` doc_id assertion — prevents empty-result success from passing silently; (2) `len(search_entries) == 1` — catches double-logging regressions; (3) `filter_flags` shape check (all booleans, all False) — proves the privacy-safe invariant for the no-filter case; (4) JSON-RPC transport error guard (`"error" not in result`); (5) content substring check in MCP response. The doc_id approach (`sha256(resolved_path)` matching `pipeline.py:287`) is the canonical way to assert "the correct document was retrieved, not just any document."
- Action: For tester e2e tests that assert on telemetry entries, always include: (a) `status="ok"` to pin success path, (b) `result_count >= 1` + specific `doc_id` in `result_doc_ids` to prove correct-document retrieval, (c) `len(search_entries) == N` to catch double-logging, (d) `filter_flags` all-False for no-filter calls. Never rely on `isError` alone — it guards against crashes, not empty-result success.
- Confidence: high

**2026-06-24 — D9 T-3: namespace validation regex rejects underscores at start/end of sentinel strings**
- Observation: Using `"__wrong-sentinel__"` as a namespace sentinel in `_assert_namespace_stored` caused `_validate_namespace()` in `constants.py` to raise `ValueError` (regex `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` requires alphanumeric first character). This broke the test at runtime, not at write time — the docstring had the wrong sentinel while the code used a valid one.
- Action: Any namespace string used in tests (even as a sentinel) must pass the namespace regex. Always use valid namespace-format strings like `"wrong-sentinel-xyz"`. Verify docstrings and code match — a docstring with an invalid sentinel is a maintenance trap for future developers.
- Confidence: high

**2026-06-24 — D9 T-3: tester e2e tasks for already-implemented backend work pass immediately; review cycle strengthened the test**
- Observation: T-3 passed green on first run (expected per D7 T-2 learning). The iterative-review cycle across 4 cycles improved the test significantly: (1) negative proofs now require `code == "not_found"` specifically, not just any error; (2) `_assert_namespace_stored` proves exclusivity against both the other real namespace AND a sentinel; (3) duplicated 55-line negative-proof blocks extracted to `_assert_cross_namespace_blocked` helper; (4) duplicated positive-proof blocks extracted to `_assert_own_namespace_accessible` helper; (5) `asyncio.run()` calls in `_assert_namespace_stored` consolidated into one coroutine.
- Action: For tester-role e2e tasks, the iterative-review cycle is where most value is added — expect the test to pass immediately, but invest in the review to catch vacuous-pass paths, crash-masking, and code duplication. The DA + Brooks-Lint combination consistently finds Moderate issues that individually seem minor but compound.
- Confidence: high

**2026-06-24 — D9 T-3: namespace gate isolation is metadata-gate-level, not data-level — accept and document**
- Observation: The DA agents correctly identified that namespace isolation in archon-search is enforced at the metadata layer (`get_collection_meta(col, namespace=ns)` returning `None` = not found) — not at the LanceDB chunk-data level (chunks have `acl=None` which passes all ACL checks). The T-3 test proves "metadata-gate enforcement via MCP search tool" not "chunk-level data isolation." This is the design: the metadata gate is the sole enforcement point.
- Action: When writing namespace isolation tests, document in the test docstring that isolation is metadata-gate-level. Accept the DA finding as a documentation clarification, not a code change. Do not attempt to add chunk-level namespace filtering unless the security model explicitly requires defense-in-depth at the data layer.
- Confidence: high

**2026-06-24 — D9 T-3: namespace gate in MCP search tools breaks tests that set get_collection_meta to return None**
- Observation: Adding a namespace gate (`await pipeline.get_collection_meta(col, namespace=ns)`) to `mcp.py` broke ~38 existing unit tests that used `pipeline = MagicMock()` with `get_collection_meta = AsyncMock(return_value=None)`. The helpers `_make_search_pipeline_with_result` and `_make_swc_pipeline_with_result` in `tests/test_mcp.py` returned `None` from `get_collection_meta` because it was previously only used for embedding model lookup, not for access gating. After the namespace gate, `None` means "not found" and the tool returns `McpErrorResponse`.
- Action: Whenever a namespace gate (`get_collection_meta` returning `None` = denied) is added to an MCP tool, audit ALL existing tests for that tool. Any helper that sets `get_collection_meta = AsyncMock(return_value=None)` for non-access-check purposes must be changed to `return_value=MagicMock()`. Only tests explicitly testing "collection not in namespace → error" should use `return_value=None`.
- Confidence: high

**2026-06-24 — D9 BE-7: integration test namespace isolation requires data injection via store layer, not REST API**
- Observation: BE-7 integration tests need to inject documents into a specific namespace without going through the full ingest pipeline. Using `ChunkRecord` + `CollectionMeta` directly with `asyncio.run(store.upsert_chunks(...))` and `asyncio.run(pipeline.update_collection_meta(...))` bypasses REST auth and embedding model validation entirely, allowing precise test data setup.
- Action: For namespace-scoped integration tests, inject test data via `pipeline.store` directly using `asyncio.run()`. Set `CollectionMeta.namespace` to the desired namespace value, then upsert chunks with matching `collection`. This is the only reliable way to create namespace-isolated test data without a live server.
- Confidence: high

**2026-06-25 — MIS T-1: `docker compose down --volumes [SERVICES]` scopes volume removal to the specified services only — empirically verified**
- Observation: Multiple DA and Brooks-Lint agents claimed `docker compose down --volumes svc-a svc-b` removes ALL named volumes including unspecified ones. Verified empirically: Docker Compose v5.1.3 removes only volumes attached to the specified services' containers. Volumes for unspecified services survive. This contradicts several reviewer claims and should not be treated as a known-bad pattern.
- Action: When reviewers cite `docker compose down --volumes [SERVICES]` as destroying unrelated volumes, run an empirical test before accepting the finding. The behavior is scoped by service, not project-wide when services are named.
- Confidence: high

**2026-06-25 — MIS T-1: Starlette lowercases all HTTP header names on the wire — always normalize to lowercase in stdlib urllib tests**
- Observation: `middleware_auth.py` sets `headers={"WWW-Authenticate": "Bearer"}` in the response. Starlette's `responses.py` lowercases all header names before sending them on the wire (`raw_headers = [(k.lower().encode("latin-1"), ...]`). The `urllib.error.HTTPError.headers` dict therefore has `"www-authenticate"` (lowercase), not `"WWW-Authenticate"`. `headers.get("WWW-Authenticate")` on a plain `dict` (case-sensitive) returns `None`, making the assertion always fail.
- Action: In any test using stdlib `urllib.request` that checks response headers: normalize to lowercase via `{k.lower(): v for k, v in ...}`. Assertions must use `"www-authenticate"` not `"WWW-Authenticate"`. Libraries like `httpx` and `requests` handle case-insensitive lookup automatically; `urllib` does not.
- Confidence: high

**2026-06-25 — MIS iterative-review: plan documents go stale fast — always re-verify key facts before treating a plan as implementation-ready**
- Observation: The MIS plan was written before D9 shipped. Three independent reviewers all flagged the MCP "not yet mounted" claim (Q1 resolution) as Critical. The plan had `status: planned` and was about to be implemented, which would have resulted in a manual telling users a working feature doesn't exist.
- Action: Before running /iterative-review on a plan, check git log for any feature merges since the plan was last updated. Cross-reference the plan's "Resolved open questions" against CLAUDE.md — CLAUDE.md is updated at each feature close-out and is the fastest way to detect stale plan claims.
- Confidence: high

**2026-06-25 — MIS iterative-review: docker-compose service name ≠ container name in tests**
- Observation: C2-T-1 caught that `docker exec archon-dev` fails in automated tests because compose prefixes container names with the project directory (e.g., `archon-search-archon-dev-1`). The smoke test pattern uses `-e ARCHON_SEARCH_API_KEY=<known>` at `docker run` time to avoid needing to extract auto-generated keys. The correct compose-aware form is `docker compose exec archon-dev` (resolves via service name).
- Action: In e2e test specs involving Docker Compose, always say "inject known keys via env (`-e ARCHON_SEARCH_API_KEY=<key>`) rather than extracting auto-generated keys from volumes." Never write `docker exec <service-name>` in a test spec — use `docker compose exec <service-name>` instead. Follow the `tests/test_docker_smoke.py` pattern as the canonical reference.
- Confidence: high

**2026-06-24 — D9 BE-4: FastMCP returns `StarletteWithLifespan`, not `Starlette`; use `user_middleware` not `middleware`**
- Observation: `create_mcp_http_app()` returns a `fastmcp.server.http.StarletteWithLifespan` object. This class does NOT expose a `.middleware` attribute (unlike standard `Starlette`). The `add_middleware` spy approach works fine for white-box tests. For post-construction inspection (asserting what was wired in), use `app.user_middleware` — which holds a list of `starlette.middleware.Middleware` namedtuples with `.cls` and `.kwargs` attributes, accessible before `build_middleware_stack()` is called.
- Action: When inspecting middleware registered on a FastMCP-created Starlette app, iterate `app.user_middleware` (not `app.middleware`). Each item has `.cls` (the middleware class) and `.kwargs` (the construction kwargs). This is valid on `StarletteWithLifespan` as observed in FastMCP 3.4.x.
- Confidence: high

**2026-06-24 — D9 BE-4: `config=None` branch test for `create_mcp_http_app` needs `ARCHON_SEARCH_DATA_DIR` env var**
- Observation: `create_mcp_http_app(config=None)` still calls `load_or_generate_key()` which writes `.search.env` to `get_data_dir()`. Without redirecting `ARCHON_SEARCH_DATA_DIR` to `tmp_path`, this writes to `~/.archon-search/` which works but pollutes the real data dir. Use `monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))` to isolate all disk writes.
- Action: Any test that calls `create_mcp_http_app()` — even with `config=None` — must redirect `ARCHON_SEARCH_DATA_DIR` via monkeypatch to avoid writing to the real `~/.archon-search/` directory.
- Confidence: high

**2026-06-24 — D9 BE-4: integration tests for namespace-dict auth path must disable key_store to isolate from synthetic-record fallback**
- Observation: TOML namespace tokens are written as synthetic KeyRecord objects to `keys.json` by the REST app lifespan. If the MCP sub-app is tested via `make_real_app` (which always creates a key_store), the key_store path authenticates TOML tokens before the `namespaces` dict is consulted — making the test vacuous w.r.t. the `namespaces` dict fix. The namespaces dict path is only reachable when `key_store=None` OR when the token is absent from `keys.json` but present in `namespaces`.
- Action: To prove the `namespaces` dict path, call `create_mcp_http_app(key_store=None)` and inspect `user_middleware` to assert `APIKeyMiddleware._namespaces == expected`. Pair with a full-stack test (via `make_real_app`) for behavioral proof. Never rely on `make_real_app` alone to test the `namespaces` dict path.
- Confidence: high

**2026-06-24 — D9 BE-3: caplog filter for MCP lifecycle test must match by message content, not just logger name**
- Observation: The MCP mount-failure warning (`"MCP server failed to start; continuing without MCP"`) is logged by `archon_search.server.app`, not `archon_search.server.mcp`. A caplog filter using `"mcp" in r.name.lower()` silently misses this entire failure class. The fix: filter by `"mcp" in r.name.lower() or "mcp" in r.getMessage().lower()` to catch both SDK-originated warnings (logger name contains "mcp") and orchestrator warnings (message content references MCP).
- Action: For any lifecycle/shutdown integration test that asserts "no MCP-related warnings," filter caplog records by message content as well as logger name. Never rely on logger name alone when the orchestrating module (`app.py`) emits relevant warnings under a non-"mcp" logger name.
- Confidence: high

**2026-06-24 — D9 BE-3: `pytestmark` in MCP integration test files should consolidate both `integration` and `xdist_group` marks**
- Observation: Using `pytestmark = pytest.mark.xdist_group("mcp")` at module level without `pytest.mark.integration` means per-test `@pytest.mark.integration` decorators are needed — inconsistent with `test_migrate_crash_resume_e2e.py` which uses `pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("benchmark")]`. The correct pattern is a list combining both marks, making per-test decorators redundant and the module's membership immediately visible.
- Action: For any new MCP integration test file, use `pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]` at module level and omit `@pytest.mark.integration` from individual test functions.
- Confidence: high

**2026-06-24 — D9 BE-3: adding a `notifications/initialized` status assertion to MCP handshake helpers is non-trivial because FastMCP accepts a range**
- Observation: The `notifications/initialized` JSON-RPC notification is a fire-and-forget (no `id`), so the server may return 200, 202, or 204 depending on FastMCP version and transport configuration. Asserting equality to a single code is fragile; asserting `in (200, 202, 204)` is the right pattern for MCP notification responses.
- Action: In MCP test helpers that perform the `notifications/initialized` step, always assert `resp.status_code in (200, 202, 204)` rather than `== 202` or ignoring the response entirely.
- Confidence: high

**2026-06-24 — test fix: `get_collection_meta` requires explicit namespace arg in `_assert_namespace_stored`**
- Observation: `store.get_collection_meta(col)` uses `DEFAULT_NAMESPACE` when no namespace arg is passed. A post-injection assertion helper that calls it without the namespace returns `None` even when the meta was correctly stored under a non-default namespace — producing a false "failed silently" failure.
- Action: Always pass the expected namespace explicitly: `store.get_collection_meta(col, expected_namespace)`. Never use the default when verifying namespace-scoped data.
- Confidence: high

**2026-06-24 — D9 BE-6: MCP search telemetry test must ingest a real collection before calling search**
- Observation: `_call_mcp_search(client, ..., collection="test-col")` hits an error path (collection not found) when the store is empty, writing an error-path telemetry entry (status="internal_error"). The test was asserting `endpoint=="search"` which passes for error entries, masking that the success-path guard at `mcp.py:393` was never exercised. Ingest a real document via `ingest_file_via_path` before the MCP call; then assert `status == "ok"` to pin the success code path.
- Action: Any MCP search telemetry test that checks `status == "ok"` must first create the target collection using `ingest_file_via_path`. Never assert only `endpoint` — always add `status == "ok"` to prove the success path, not just any path.
- Confidence: high

**2026-06-24 — D9 BE-6: `make_real_app` must set `cfg.telemetry.log_dir` to `tmp_path/search-logs` unconditionally**
- Observation: `SearchConfig()` defaults `telemetry.log_dir` to `~/.archon-search/search-logs`. Tests that created a `tmp_path / "search-logs"` directory manually and checked it were checking a path the app never writes to — the assertion was vacuously true regardless of whether the writer fired or not.
- Action: Set `cfg.telemetry.log_dir = str(tmp_path / "search-logs")` in `make_real_app` unconditionally (before the `telemetry_enabled` branch). Then always check `Path(_cfg.telemetry.log_dir)` in tests — never construct a parallel path by hand. Add `telemetry_enabled: bool = False` parameter that sets `cfg.telemetry.enabled = True` so tests don't need to duplicate the full `create_app` boilerplate.
- Confidence: high

**2026-06-24 — D9 BE-1: adding a top-level SearchConfig dataclass field requires three coupled updates plus an allowlist line-number bump**
- Observation: Adding `McpConfig` + `SearchConfig.mcp` touched four files in a fixed pattern: (1) `config.py` dataclass + field + `_apply_toml` parse block (mirror the `[auth]` block exactly: `doc.get("mcp", {})` → fresh `McpConfig()` → `if "enabled" in cfg: _coerce_bool(...)` → assign); (2) `test_config_defaults.py::test_all_defaults_snapshot` — the keyset guard (`set(expected.keys()) == {f.name for f in dataclasses.fields(SearchConfig)}`) FAILS if you forget the snapshot entry; (3) `tests/path_home_allowlist.txt` — the `config.py:NNN` line number for the `Path.home()` reference shifts when you add lines above it (here 186→193); the SHA stays the same because the line content is unchanged. The toml.example `[mcp]` section is a SEPARATE task (BE-10) — keep it out of the BE-1 commit even if it is already in the working tree.
- Action: When adding a `SearchConfig` field, update config.py + the defaults snapshot + the path allowlist line number in the same commit. Per-`_coerce_bool` "wrong type raises" tests are NOT expected (only 2 of 14 sibling call sites have one); rely on `_coerce_bool`'s own contract.
- Confidence: high

**2026-06-24 — D9 K-1: FastMCP API changed between versions; spike undeclared deps before writing ADRs**
- Observation: The existing `mcp.py` imported `fastmcp` and called `streamable_http_app()`, which does not exist in FastMCP 3.4.2 (`http_app()` replaces it). `fastmcp` was also absent from `pyproject.toml`. ADR spikes must check that all imports are declared and that the API being described actually exists in the installed version.
- Action: Before writing any ADR that references a third-party package API, (1) verify the package is in `pyproject.toml`, (2) verify the exact method/class names by importing and running them, not by reading prior code that may have been written against an older version.
- Confidence: high

**2026-06-24 — D9 K-1: FastMCP lifespan delegation requires explicit `router.lifespan_context`**
- Observation: Mounting a FastMCP Starlette app via `app.mount('/mcp', mcp_starlette)` without delegating its lifespan causes every MCP request to fail with `RuntimeError: Task group is not initialized`. The `StreamableHTTPSessionManager` task group only starts when `mcp_starlette.router.lifespan_context(app)` is entered. The fix is `async with mcp_starlette.router.lifespan_context(app): yield` inside the parent lifespan.
- Action: When mounting any Starlette sub-app that has its own lifespan under FastAPI, always explicitly delegate via `sub_app.router.lifespan_context(parent_app)`. Never assume `app.mount()` propagates lifespan automatically.
- Confidence: high

**2026-06-24 — D9 K-1: `app.mount()` must happen INSIDE the lifespan context to avoid zombie routes**
- Observation: If `app.mount('/mcp', mcp_starlette)` is called before `async with mcp_starlette.router.lifespan_context(app):` enters and the `__aenter__` raises, the route is registered but the session manager never started. Every `/mcp` request returns 500. Starlette has no `app.unmount()` API.
- Action: Always call `app.mount()` for a sub-app that needs lifespan delegation INSIDE the `async with` block, after the lifespan context has successfully entered.
- Confidence: high

**2026-06-24 — D9 K-1: `_current_http_request` ContextVar is per-request (not per-session) in Streamable HTTP**
- Observation: FastMCP's Streamable HTTP transport sends each JSON-RPC call (initialize, tools/list, tools/call) as a separate HTTP POST. `RequestContextMiddleware` sets `_current_http_request` on every HTTP request — it is NOT frozen at session initialization. Namespace stability across a session comes from the MCP protocol requirement that clients reuse the same bearer token throughout a session, not from ContextVar behavior.
- Action: Never document `_current_http_request` as "session-frozen." Tool closures should call `_get_request_namespace()` on each invocation, not cache the result. Per-request ContextVar behavior is actually better — it means namespaces are resolved correctly even if the pattern were used in other contexts.
- Confidence: high

**2026-06-23 — D7 BE-8: safe write order for dual-file atomic rotation**
- Observation: Generating the new token in the route handler and writing `.search.env` FIRST (before calling `rotate_default_key()` to mutate `keys.json`) requires passing a pre-generated token as an optional param to the Use Cases layer. This is a pragmatic layering shortcut — the Interface Adapter layer owns token generation to enable the crash-safe write order. The key_manager method gets an optional `new_token` param with empty-string validation.
- Action: For any multi-file atomic operation where write order matters for crash safety (file A must succeed before file B is mutated), generate the content in the Interface Adapter layer, write file A first, then pass the content to the Use Cases layer for file B. Document the partial-state recovery path clearly in comments.
- Confidence: high

**2026-06-23 — D7 BE-8: `or self._fallback` is wrong for falsy-valid values — use `is not None`**
- Observation: `getattr(..., "api_key", None) or self._api_key` silently falls back to the stale construction-time key if `app.state.api_key` is ever set to `""` (empty string, which is falsy). The correct pattern is `... if ... is not None else self._api_key`. The `or` idiom is incorrect whenever the attribute's falsy value (empty string, `0`, `False`) is semantically different from "attribute not set" (None).
- Action: Never use `or fallback` for attribute lookups where the valid value could be falsy. Always use `is not None` guard explicitly.
- Confidence: high

**2026-06-23 — D7 BE-8: datetime patches must match the module that calls `datetime.now()`**
- Observation: `patch.object(km_mod, "datetime", mock_dt)` patches `datetime.now()` in `key_manager.py` only. `middleware_auth.py` imports `datetime` separately and is NOT covered by this patch. Tests that fast-forward time to expire grace windows must either (a) patch both modules or (b) use a code path where only one module's datetime determines the outcome. Always comment in the test which datetime calls are mocked and which are real, and explain why the test still produces the correct result.
- Action: When writing time-sensitive tests involving multiple modules, enumerate which modules call `datetime.now()` and explicitly decide which need patching. Document the mechanism in the test comment.
- Confidence: high

**2026-06-23 — D7 T-2: e2e tester tasks against already-implemented code start green**
- Observation: T-2 is a tester task that writes e2e tests against functionality already implemented by BE-5, BE-6, and FE-2. All 3 tests passed immediately on first run (no red phase). This is correct: the "TDD red phase" only applies when the tests are written before the implementation. For tester-role e2e tasks that close out completed backend/frontend tasks, the tests should pass immediately if the implementation is correct.
- Action: For tester-role e2e tasks in phase close-out position, expect tests to pass on first run. The value of the task is the coverage and regression protection, not the TDD cycle. Still run the tests to confirm — a failing test would indicate an implementation bug.
- Confidence: high

**2026-06-23 — D7 T-2: iterative review found two meaningful improvements to e2e assertion strength**
- Observation: The initial 3-test file had: (1) S4 verified only via auth-rejection (indirect), not via GET /keys read-back; (2) status=all view asserted key presence but not `status=="revoked"` field; (3) no `WWW-Authenticate: Bearer` header check on 401 responses. All three were legitimate Moderate findings caught by DA review. The fixes added one-line to one-paragraph additions that materially strengthen coverage without changing test structure.
- Action: For any e2e test that verifies a revocation, always add a GET read-back assertion (not just auth rejection) to directly prove on-disk persistence. Always check `WWW-Authenticate: Bearer` on 401 responses in e2e middleware tests.
- Confidence: high

**2026-06-24 — D9 BE-5: `ContextVar.get` is a read-only C slot — patch the module-level helper, not the ContextVar**
- Observation: `patch.object(mcp_module._current_http_request, "get", ...)` raises `AttributeError: '_contextvars.ContextVar' object attribute 'get' is read-only`. ContextVar methods are implemented in C and cannot be patched via `unittest.mock`. The fix is to patch the Python-level helper function that wraps the ContextVar call: `patch("archon_search.server.mcp._get_request_namespace", return_value=ns)`.
- Action: Never attempt to patch a `ContextVar`'s `.get()` or `.set()` methods. Always patch the Python-level wrapper function (`_get_request_namespace`) that encapsulates the ContextVar read. This is both correct and more resilient to ContextVar implementation changes.
- Confidence: high

**2026-06-24 — D9 BE-5: use `fastmcp.server.dependencies.get_http_request()` (public API) instead of private `_current_http_request` ContextVar**
- Observation: `fastmcp.server.http._current_http_request` is a private symbol. FastMCP 3.4+ ships `fastmcp.server.dependencies.get_http_request()` as a public API that wraps `_current_http_request` plus also handles `request_ctx` (MCP SDK path) and Docket worker snapshots. The public API raises `RuntimeError` when no HTTP context is active, which can be caught cleanly for fallback.
- Action: When reading the current HTTP request inside a FastMCP tool or helper, always use `from fastmcp.server.dependencies import get_http_request` with `try: req = get_http_request() except (ImportError, RuntimeError): return DEFAULT_NAMESPACE`. Never import `_current_http_request` directly from `fastmcp.server.http`.
- Confidence: high

**2026-06-24 — D9 BE-5: fastmcp stub contamination in xdist_group("mcp") — lazy import with fallback is the correct fix for mcp.py**
- Observation: Some test files stub `fastmcp` as a plain `types.ModuleType("fastmcp")` without a `server.http` submodule. A top-level `from fastmcp.server.http import ...` in `mcp.py` causes `ModuleNotFoundError` in those files' workers. Moving the import inside the function with `try/except ImportError` prevents the module-level import failure and lets test stubs work correctly without changing the test infrastructure.
- Action: Any import of `fastmcp` submodules (e.g. `fastmcp.server.dependencies`) in `mcp.py` that is not guarded by the existing `if "fastmcp" not in sys.modules` pattern should be lazy (inside the function body) with `try/except ImportError`. This is the surgical fix that does not require updating every stub-installing test file.
- Confidence: high

**2026-06-24 — D9 BE-5: `delete_document` explicit namespace parameter is a cross-tenant bypass — security finding**
- Observation: The `delete_document` MCP tool accepted an optional `namespace: str | None` caller-controlled parameter and used `namespace or _get_request_namespace()`. This allowed any authenticated caller to pass a foreign namespace and operate on another tenant's documents. The fix: validate that caller-supplied `namespace` matches `_get_request_namespace()`; mismatch returns `code="forbidden"` before any pipeline call.
- Action: Any MCP tool that has a caller-controllable `namespace` parameter AND also does namespace-scoped pipeline operations must validate the supplied namespace against the authenticated namespace (`_get_request_namespace()`). The authenticated namespace is always authoritative; caller-supplied namespaces are a compatibility hint only, not a capability escalation.
- Confidence: high

**2026-06-23 — D7 FE-2: `pytest.mark.integration` as a bare expression vs decorator — dead code trap**
- Observation: Placing `pytest.mark.integration` as the last statement inside an integration test function (instead of as a decorator on the function definition) is silently valid Python but does nothing. The test receives no mark. `uv run pytest -m integration` misses it entirely. The correct form is `@pytest.mark.integration` above the `def`. When a file mixes unit and integration tests, use the decorator form — NOT `pytestmark` at module level, which would incorrectly mark all unit tests in the file as integration tests.
- Action: When adding an integration test to a file that also contains unit tests, use `@pytest.mark.integration` as a decorator directly on the function. Never use a trailing bare expression statement as a marker. Always verify with `uv run pytest -m integration <file> -n0 -v --no-cov` that the test appears in the collected output.
- Confidence: high

**2026-06-23 — D7 FE-2: hint line assertions must use specific substrings, not generic character matches**
- Observation: `"2" in result.output` is a vacuous assertion for a hint-count check — the digit "2" appears in key IDs ("abc-123"), timestamps ("2026-01-01"), and other output fields. The assertion would pass even if the hint line were never printed. Fix: assert the full specific substring, e.g., `"2 revoked key(s) hidden" in result.output`. This verifies both the count and the surrounding text uniquely.
- Action: For any CLI test that checks a count or number in formatted output, never assert `str(N) in result.output`. Always assert the full surrounding phrase that makes the assertion unique within the expected output.
- Confidence: high

**2026-06-22 — D7 BE-6: `hidden_revoked_count` must be scoped to the namespace filter, not computed globally**
- Observation: `GET /keys?namespace=ns-a` should show a `hidden_revoked_count` that counts only revoked keys in `ns-a`. Computing the count from all records before namespace filtering means revoked keys in `ns-b` inflate the count shown to the operator, who is looking at a namespace-scoped view. Fix: apply namespace filter first to get `scope`, then compute `hidden_revoked_count = sum(r.status == "revoked" for r in scope)`.
- Action: For any list endpoint that has both a visibility filter (status) and a scope filter (namespace), always apply the scope filter first so all derived counts (e.g., "hidden" counts) reflect the scoped view, not the global store.
- Confidence: high

**2026-06-22 — D7 BE-6: `Literal["revoked"]` vs `Literal["active", "revoked"]` in response schemas where only one value is valid**
- Observation: `KeyRevokeResponse.status` was initially typed as `Literal["active", "revoked"]` matching the `KeyStatus` TypeSpec enum. But `DELETE /keys/{id}` always returns `status="revoked"` — there is no execution path that returns `"active"`. Narrowing to `Literal["revoked"] = "revoked"` is a valid subtype of the TypeSpec contract and makes the schema self-documenting. API consumers reading the OpenAPI spec no longer have to wonder whether DELETE can return `status: "active"`.
- Action: For any response schema field where the set of actually-returned values is smaller than the domain type, narrow the `Literal` to the actual set. The narrowed Pydantic type is still a valid subtype of the TypeSpec `KeyStatus` enum.
- Confidence: high

**2026-06-22 — D7 BE-6: Docstring ordering in route handlers propagates to OpenAPI spec**
- Observation: A route handler docstring that said "namespace filter applied after the status filter" became stale when the implementation was refactored to apply namespace first. The docstring propagates verbatim into the OpenAPI `description` field, which is the authoritative contract. The mismatch was caught by iterative-review DA agents checking architectural alignment.
- Action: When changing filter ordering in a route handler that describes its ordering in the docstring, update the docstring in the same edit. The OpenAPI snapshot test will catch the diff, but the semantic accuracy must be verified manually.
- Confidence: high

**2026-06-22 — D7 BE-5: `None == None` is True — `revoke(None)` would silently match synthetic TOML records without an explicit guard**
- Observation: `KeyRecord.id` is typed `str | None`, so synthetic TOML records have `id=None`. If `revoke(key_id)` is called with `key_id=None` (possible despite `str` type hint because Python doesn't enforce type hints at runtime), the loop condition `record.id == key_id` evaluates `None == None` → True, silently revoking a TOML synthetic record. Fix: `if not isinstance(key_id, str): raise KeyError(...)` at the top of `revoke()`, before the lock is acquired.
- Action: For any method that matches against a nullable entity field (especially when `None` is a valid field value), add an explicit type guard at the entry point. Never rely on the type annotation alone.
- Confidence: high

**2026-06-22 — D7 BE-5: `patch("module.datetime")` correctly intercepts `datetime.now(UTC)` in tests**
- Observation: When `active_keys()` calls `datetime.now(UTC)` and the test patches `archon_search.key_manager.datetime` with `mock_dt.now.return_value = fixed_now`, the mock intercepts the call correctly. The `asyncio.run()` call inside the `patch()` context still sees the mocked datetime because patches are process-wide for the patched module. This pattern is safe for testing strict time boundaries (`expires_at <= now`).
- Action: For any time-boundary test (e.g., "key expired at exactly NOW"), always use `patch("module.datetime")` to freeze the clock. Using a past timestamp only tests `<`, not `<=`. The frozen-clock pattern is the only way to test exact-equality expiry.
- Confidence: high

**2026-06-23 — D7 BE-9: `assert` in production code is stripped by `python -O` — use explicit RuntimeError**
- Observation: `assert result["new_token"] == new_raw_token` was used as a postcondition invariant check in both `mcp.py` and `routes_keys.py`. Python's `-O` flag silently removes all `assert` statements, so this invariant would be invisible in optimised deployments. For any invariant that guards data integrity (e.g., verifying a write result matches what was computed), replace `assert` with `if ... != ...: raise RuntimeError(...)`. The exception text should include "BUG" to distinguish it from user-facing errors.
- Action: Never use `assert` for production data-integrity invariants. Use `assert` only in tests and in code paths that are exclusively executed during development/debugging. Any postcondition that would indicate a programming error (not a user error) must be an explicit `if ... raise RuntimeError(...)`.
- Confidence: high

**2026-06-23 — D7 BE-9: exception message leakage via `f"Failed to create key: {exc}"` in MCP tools**
- Observation: Catching `Exception as exc` and returning `f"...: {exc}"` in an MCP tool leaks internal error details (file paths, permission errors, internal class names) to the caller. MCP tool callers may be LLM agents or external systems. For `internal_error` responses, use a fixed message without the exception string.
- Action: In MCP tool exception handlers (and any API boundary), for `code="internal_error"` responses, never interpolate `{exc}` into the error message. Use a fixed string. Log the exception internally if needed, but strip it from the response.
- Confidence: high

**2026-06-23 — D7 BE-9: cross-surface asyncio.Lock does NOT prevent REST+MCP concurrent race**
- Observation: `_mcp_rotate_lock` (in `mcp.py`) and `_rotate_lock` (in `routes_keys.py`) are two separate `asyncio.Lock` instances. They prevent intra-surface races (two concurrent MCP rotates, or two concurrent REST rotates) but NOT cross-surface races (one MCP + one REST rotate simultaneously). `KeyStore._lock` serialises `keys.json` mutations per-process but does not cover the `.search.env` write, which happens outside `KeyStore`. This is an accepted limitation: key rotation is an infrequent administrative operation.
- Action: When adding a new surface to an operation that already has a per-surface lock, document the cross-surface limitation explicitly in a comment near the lock definition. Never claim a per-surface lock provides cross-surface serialisation. If the operation is infrequent enough that races are acceptable, accept and document it explicitly rather than attempting a cross-surface lock (which would require a shared lock instance or a different coordination mechanism).
- Confidence: high

**2026-06-22 — D7 BE-5: already-revoked no-op must skip the disk write, not just skip the status mutation**
- Observation: Initial implementation mutated `record.status = "revoked"` before checking `if record.status == "revoked"`. Reordering to check-first-then-return skips the disk write for the already-revoked case. This is the correct "no-op" behavior per spec — the plan explicitly says "already-revoked returns 200 [from the route handler]" implying no state change should occur. Calling `_write()` on an already-revoked key is semantically a no-op but wastes I/O and could race with concurrent writers.
- Action: For any idempotent write operation on a resource, always add an early-return guard that checks the current state BEFORE the mutation, not after. Avoids spurious disk writes and makes the idempotency intent explicit.
- Confidence: high

**2026-06-22 — D7 T-1: `store.ingest_chunks` does not forward `namespace` to `_do_update_meta_on_add`**
- Observation: `ingest_chunks` accepts a `namespace` kwarg but does NOT forward it to `_do_update_meta_on_add`, which always defaults to `DEFAULT_NAMESPACE` when called internally. The test helper omits `embedding_model` from the `ingest_chunks` call so `_do_update_meta_on_add` short-circuits (returns False when `embedding_model is None`) and the explicit `update_collection_meta` call remains the sole source of namespace truth. Adding `embedding_model=...` to `ingest_chunks` without fixing the propagation gap would create a wrong-namespace meta row under 'default', breaking isolation assertions.
- Action: When writing e2e tests that inject data under a specific namespace, use `update_collection_meta` as the authoritative namespace setter — not the `namespace` kwarg on `ingest_chunks`. Always document this fragility in the helper's docstring.
- Confidence: high

**2026-06-22 — D7 T-1: e2e namespace isolation proof pattern for managed-key auth**
- Observation: `request.state.namespace` is not directly accessible from outside the HTTP boundary in e2e tests. The correct proof technique is a two-step isolation check: (1) managed token + search → 200 with non-empty results (collection accessible); (2) default key + same search → 404 (collection invisible to different namespace). Step 1 alone is vacuous (200 doesn't prove correct namespace). Step 2 alone doesn't prove the managed key's namespace is correct. Together they form a sound proof. The assertion message for step 1 should explicitly reference step 2 as the complementary isolation proof.
- Action: For any e2e test that must prove namespace resolution, combine a "managed key sees data" assertion with a "different-namespace key cannot see the same data" assertion. Never claim either assertion alone proves namespace resolution.

**2026-06-25 — MIS BE-3: DA review reliably finds Critical doc errors that are hard to self-detect**
- Observation: Two independently Critical errors survived the first-pass write and were caught only by DA review: (1) `~/.claude/settings.json` does not have a `mcpServers` key — the correct registration mechanism is `claude mcp add --transport http`; (2) Python MCP SDK import `streamablehttp_client` is wrong — it was renamed to `streamable_http_client` and returns a 2-tuple not 3-tuple. Both were verified against the actual files/codebase before fixing. Neither would have been caught by a manual self-review of the doc alone.
- Action: For any doc that includes client SDK examples or CLI commands involving third-party tools (Claude Code MCP registration, Python SDK patterns), always run iterative-review before committing — these are high-hallucination-risk areas where the LLM writes plausible-but-wrong code. Verify import paths and CLI flag names against the actual installed package before accepting DA findings as fixes.
- Confidence: high

**2026-06-25 — MIS BE-3: `.mcp.json` at repo root needs explicit .gitignore warning**
- Observation: DA review (C2-A-2) correctly flagged that suggesting `.mcp.json` at the repo root without a `.gitignore` warning leads operators to commit local infrastructure topology (localhost addresses and bearer tokens) to version control. The doc was presenting it as a neutral alternative to `claude mcp add`, which it is not — it's infrastructure config, not project config.
- Action: Whenever suggesting `.mcp.json` as an MCP client config mechanism in documentation, always add the `.gitignore` caveat: "This file is per-developer infrastructure config; add it to `.gitignore` rather than committing it, since it binds to local instance addresses and keys."
- Confidence: high

**2026-06-25 — MIS BE-3: `claude mcp list` output format is static config, not a live connection probe**
- Observation: DA test coverage review (C2-T-2) correctly identified that `claude mcp list` shows registered servers, not a live connection status with "✔ Connected." Fabricating a specific terminal output with checkmarks creates false confidence — users will think their setup is broken when the real output looks different, or think it's working when the server is actually down.
- Action: When documenting CLI verification steps, never fabricate terminal output that implies dynamic state (connection status, counts, checksums). Write neutral verification prose: "Both servers should appear in the list." Only show exact output when it is deterministic and verified.
- Confidence: high
- Confidence: high

**2026-06-22 — D7 BE-3: `KeyRecord.id: str | None` widens for synthetic TOML records; `_logged_expired_ids` guard**
- Observation: Widening `id: str` to `str | None` to accommodate synthetic TOML records (no UUID) also introduced a latent bug in `active_keys()`: the `_logged_expired_ids.add(record.id)` call would add `None` to a `set[str]`, causing all synthetic expired records to share the same suppression slot. Fix: guard with `if record.id is not None` before touching `_logged_expired_ids`. Synthetic records never have `expires_at` set (they expire only when the TOML entry is removed and the server restarted), so the guard is purely defensive.
- Action: Whenever widening an entity field from a non-nullable type to `T | None`, grep all in-memory tracking structures that use that field as a key (sets, dicts) and add a `None` guard before any insertion.
- Confidence: high

**2026-06-22 — D7 BE-3: `asyncio.Lock` is NOT event-loop-bound in Python 3.10+**
- Observation: A DA agent flagged `asyncio.run()` inside `TestClient` context as a Critical issue, citing pre-3.10 behavior where `asyncio.Lock` was bound to the creating event loop. Verified empirically: in Python 3.12, `asyncio.Lock` can be acquired from a different event loop created by `asyncio.run()` — no DeprecationWarning, no RuntimeError. The test pattern (`asyncio.run(key_store.create(...))` while TestClient active in background thread) is safe and matches 15+ existing tests in the codebase.
- Action: For Python 3.10+ projects, do not reject `asyncio.run()` in tests due to Lock binding concerns. Verify by running the actual test rather than reasoning from pre-3.10 docs.
- Confidence: high

**2026-06-22 — D7 BE-3: `load_synthetic_records` calling `load()` inside the asyncio.Lock is safe**
- Observation: `KeyStore.load()` is documented as "no lock needed" because it is read-only. Calling it inside `async with self._lock:` in `load_synthetic_records()` does NOT deadlock because `asyncio.Lock` is not reentrant but `load()` never tries to re-acquire the lock — it is purely read-only. The lock in `load_synthetic_records` serializes the full read-modify-write cycle against concurrent `create()` / `revoke()` calls; `load()` called inside is just a disk read.
- Action: Calling a non-locking read method inside a lock block is safe as long as the read method does not itself acquire the same lock. Always verify by checking whether the read method has `async with self._lock:` before calling it from within a locked block.
- Confidence: high

**2026-06-22 — D7 BE-2: `Literal` status guard vs datetime expiry in security middleware**
- Observation: The rotation-revocation guard in `APIKeyMiddleware` checked `r.status in ("revoked", "expired")`, but `KeyRecord.status` is `Literal["active", "revoked"]` — `"expired"` is not a valid value. Expiry in this codebase is temporal (`expires_at <= now`), not a status transition. Iterative-review DA agents caught this as Critical in cycle 1. Fix: replace the literal check with `r.status == "revoked" or (r.expires_at is not None and r.expires_at <= now)`.
- Action: Whenever writing a guard that needs to catch both explicit revocation AND time-based expiry, always use a datetime comparison for the expiry branch — never assume a `"expired"` status value will exist unless you verify the `Literal` type in the entity model.
- Confidence: high

**2026-06-22 — D7 BE-2: cross-block variable scoping in security middleware**
- Observation: `token_hash` was computed at line 57 inside `if self._key_store is not None:` and then redundantly recomputed at line 75 inside the same guard condition. Python `if` blocks do not create new scopes, so the variable was already in scope. Removing the redundant computation is safe, but requires a comment noting the cross-block dependency to prevent future `UnboundLocalError` when someone refactors the blocks.
- Action: When reusing a variable across two separate `if` blocks that share the same condition, add an inline comment at the read site noting the dependency: `# token_hash computed at line N under the same guard`.
- Confidence: high

**2026-06-22 — D7 BE-2: iterative review catches datetime-based expiry branch with zero test coverage**
- Observation: After the Cycle 1 fix that replaced the dead `"expired"` literal check with `r.expires_at <= now`, the new branch had zero test coverage. Cycle 2 DA agents independently flagged this as Moderate. The fix was a targeted test: create a record with `status="active"` and `expires_at` in the past, use the same token as `api_key`, assert 401. Without this test, a regression removing the `expires_at` branch would pass the full suite silently.
- Action: After any security-guard fix that adds a new predicate branch, immediately add a test that exercises that exact branch. The existing passing tests are not sufficient coverage for the new code path.
- Confidence: high

**2026-06-15 — Parallel iterative review of already-merged commits**
- Observation: `git reset --soft <parent>` inside a worktree exposes a commit's diff as staged changes, which `/iterative-review` can then inspect without needing an open branch.
- Action: Use this pattern when spawning review agents on commits that are already merged to main; it avoids checking out detached HEAD and keeps the worktree clean.
- Confidence: high

**2026-06-15 — Coroutine leak prevention pattern in threaded async code**
- Observation: `asyncio.run_coroutine_threadsafe` raises `RuntimeError` when the loop is closed, but can raise other exceptions (e.g. `ValueError`, `TypeError`) for other failure modes. Only catching `RuntimeError` leaves the coroutine unawaited on those paths, emitting `RuntimeWarning: coroutine never awaited`.
- Action: Always add `except BaseException: coro.close(); raise` after `except RuntimeError` whenever a coroutine is created before being handed to `run_coroutine_threadsafe`.
- Confidence: high

**2026-06-15 — Graduating plans to Completed/**
- Observation: Verify that (a) all plan tasks are `[x]`, and (b) key production symbols exist in the codebase (`grep` or `ls`) before moving to `Documentation/Completed/`. Plans without a paired brief (e.g. E1) are moved as plan-only.
- Action: Follow this two-step verification before any move; never move based on plan checkbox state alone.
- Confidence: high

**2026-06-20 — Patching a static method on a class with monkeypatch**
- Observation: `SearchStore._all_migrations.__func__` raises `AttributeError` because a `staticmethod` is already unwrapped by the time it's accessed as a class attribute — `__func__` only exists on bound methods. To capture the original static method, call it directly (`SearchStore._all_migrations()`) before patching, then use `monkeypatch.setattr(SearchStore, "_all_migrations", staticmethod(wrapper))`.
- Action: Never access `.__func__` on a static method accessed via the class. Call it first to capture its return value, then replace with `staticmethod(new_fn)`.
- Confidence: high

**2026-06-20 — apply_in_place_migrations is a route-level, not startup-level, operation**
- Observation: `SearchStore._run_startup_migrations()` only runs structural migrations (`migrate_*` methods + `_migrate_schema_version`). Per-collection `apply_in_place_migrations` is NOT called during lifespan startup — it is exclusively triggered by `POST /collections/{name}/migrate`. The "startup migration path" test must exercise the HTTP route, not the lifespan hook, to exercise `apply_in_place_migrations`.
- Action: When writing tests for "startup migration path", use `make_real_app` to start the server, then call the HTTP endpoint. Do not expect `apply_in_place_migrations` to be triggered by lifespan alone.
- Confidence: high

**2026-06-20 — Batching refactor: embed first batch before ensure_collection**
- Observation: Moving `ensure_collection` before the batch loop broke `embedder.embedding_dim` access (it's lazy-initialized on first `embed()` call). The fix is to embed the first batch before calling `ensure_collection`, then reuse those vectors in the loop.
- Action: When moving `ensure_collection` before embed in a batch loop, pre-embed the first batch so `embedding_dim` is initialized. Skip re-embedding batch 0 inside the loop by reusing the pre-embedded vectors.
- Confidence: high

**2026-06-20 — Integration test wrappers for ingest_chunks must accept new keyword args**
- Observation: When adding a new keyword parameter (`_is_continuation`) to `ingest_chunks`, any test that monkey-patches `store.ingest_chunks` with a custom function must be updated to accept the new kwarg. The pipeline now always passes `_is_continuation=True/False`, so wrappers missing it get `TypeError`.
- Action: After adding keyword parameters to a method that is monkey-patched in tests, grep for all test overrides of that method and update their signatures.
- Confidence: high

**2026-06-26 — E0b BE-8: `with TestClient(app)` triggers lifespan; use bare TestClient when setting app.state manually**
- Observation: `with TestClient(app) as client:` enters the lifespan, which calls `_run_startup_migrations()` (async) on whatever store is set on `app.state`. When `app.state.pipeline.store` is a `MagicMock()`, the async startup migration raises `TypeError: object MagicMock can't be used in 'await' expression`. Fix: use a bare `TestClient(app)` (no context manager) for tests that set `app.state` attributes manually and don't need the lifespan to run.
- Action: For status/health route tests that build a minimal app with `create_app()` and manually set `app.state.*`, always use `client = TestClient(app)` without a `with` block. Reserve `with TestClient(app) as client:` for tests using `make_real_app` (which wires real stores that survive async startup).
- Confidence: high

**2026-06-26 — E0b BE-8: `is_key_available()` delegation pattern keeps env-var logic in Use Cases layer**
- Observation: The initial implementation had `if not os.environ.get("ANTHROPIC_API_KEY"):` inline in `generate()` and `generate_variants()`. Extracting to `is_key_available()` (a) removes duplication, (b) lets the status route call `generator.is_key_available()` without importing `os` in the Interface Adapters layer, and (c) makes the method easily mockable in tests. The `_GUARD_PATTERN` regex in `test_anthropic_key_guards.py` needed extension to also match the `self.is_key_available()` form so the C18 guard test doesn't falsely fire after the DRY refactor.
- Action: When an env-var check is needed both in the execution path (early-exit guard) and in a status/health endpoint, extract it to a method on the class (`is_key_available()`) and delegate the guard to it. Update any regex-based "guard existence" tests to accept the delegation form alongside the direct `os.environ.get()` form.
- Confidence: high

**2026-06-21 — asyncio.Event.set() from test thread is consumed by the running event loop before the route handler checks it**
- Observation: In `test_trigger_while_busy_returns_202`, calling `maintenance_loop._trigger_event.set()` from the synchronous test thread caused the TestClient's background asyncio event loop to immediately wake up `_trigger_loop`, run `_run_one_pass`, and clear the event — all before the route handler ran. The test got `"triggered"` instead of `"already_triggered"` because by the time the route checked `is_set()`, the loop had already cleared it.
- Action: When testing an "event already set" branch in a route, replace the actual `asyncio.Event` with a `MagicMock` whose `is_set()` always returns `True`. Do not set the real event from the sync test thread when an async loop is consuming it in the background.
- Confidence: high

**2026-06-22 — D7 BE-4: `KeyStore.create()` should return `created_at` to prevent response/storage timestamp divergence**
- Observation: The route initially captured `datetime.now(UTC)` itself before calling `create()`. Under asyncio lock contention (concurrent creates), the gap between the route's timestamp and the store's internally-captured timestamp could be seconds — creating an observable inconsistency between the POST response and future GET /keys (which reads from disk). Fix: have `create()` return `created_at` in the result dict so the route uses the exact timestamp stored in keys.json.
- Action: Whenever a Use Case method sets a server-side timestamp internally (like `created_at = datetime.now(UTC)`), return it to callers rather than making callers compute their own approximate value. The timestamp in the response must match what is persisted.
- Confidence: high

**2026-06-22 — D7 BE-4: `AwareDatetime` from pydantic is the correct type for timezone-required datetime fields in request schemas**
- Observation: `KeyCreateRequest.expires_at: datetime | None` accepted naive datetimes at Pydantic level. `KeyStore.create()` raised `ValueError` for naive datetimes — but this was NOT caught by the route's `try/except` (which only wrapped `_validate_namespace`). Result: naive expires_at input caused uncaught 500. Fix: use `from pydantic import AwareDatetime` and type the field as `AwareDatetime | None`.
- Action: Any request schema field that requires timezone-aware datetimes (where the downstream code validates tzinfo) must use `AwareDatetime` from pydantic, not bare `datetime`. Never rely on the downstream layer's ValueError to provide the 422 — wire validation at the schema boundary.
- Confidence: high

**2026-06-22 — D7 BE-4: "label echoed in response" test is tautological when route builds response from `body.label`**
- Observation: After eliminating the double-read, the route builds `KeyCreateResponse(label=body.label, ...)` directly. A test asserting `body["label"] == "my-label"` only proves the route echoes the input — it does NOT prove the label was persisted. A bug in `KeyStore.create()` ignoring the label would pass silently. Fix: add a persistence assertion by reading keys.json directly and checking the stored record's label.
- Action: For any create endpoint where the response is built from request inputs (not from the stored record), always add a separate persistence assertion that reads the storage layer directly and confirms the field was actually written.
- Confidence: high

**2026-06-22 — D7 FE-1: `strftime("%Y-%m-%dT%H:%M:%SZ")` silently corrupts non-UTC datetime offsets**
- Observation: `expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")` appends a literal "Z" (UTC) without converting the datetime to UTC first. For a user-supplied `+05:30` offset, the server receives a timestamp 5.5 hours off. Fix: use `expires_dt.isoformat()` which preserves the original offset. Pydantic's `AwareDatetime` accepts both `+00:00` and `+05:30` formats.
- Action: Never use `strftime("...Z")` to format a timezone-aware datetime unless you have already called `.astimezone(UTC)` first. Default to `.isoformat()` which is unambiguous.
- Confidence: high

**2026-06-22 — D7 FE-1: S22 (stdout/stderr split) — Click 8.x `result.stdout` and `result.stderr` are available without `mix_stderr=False`**
- Observation: `CliRunner` in Click 8.3.x does NOT accept `mix_stderr` as a constructor param (it was removed in Click 8.0). However, `result.stdout` and `result.stderr` ARE available on the `Result` object — they correctly separate the two streams. The old approach of patching `click.echo` to detect `err=True` calls works but is fragile; using `result.stdout`/`result.stderr` directly is more reliable.
- Action: For S22-style tests (token on stdout, banner on stderr), assert `token in result.stdout`, `token not in result.stderr`, `"WARNING" in result.stderr`, `"WARNING" not in result.stdout`. Do not use `CliRunner(mix_stderr=False)` — it raises `TypeError` in Click 8.x.
- Confidence: high

**2026-06-22 — D7 FE-1: metadata vs token stdout/stderr assignment — spec says "raw token on stdout only"**
- Observation: Initial implementation printed metadata (`id:`, `namespace:`, `created_at:`) to stdout alongside the token. S22 explicitly says "raw token on stdout only; warning banner on stderr only" — the "only" applies to BOTH: only the token on stdout, only the banner on stderr. Metadata lines belong on stderr so `$()` capture yields a clean token for scripting.
- Action: In any CLI command that must print a sensitive token, send ALL contextual metadata to `err=True` and print ONLY the raw token to stdout. Verify with `result.stdout`/`result.stderr` split.
- Confidence: high

**2026-06-23 — D7 FE-3: CliRunner.invoke(env={...}) sets os.environ during isolation — triggers server-side env var guards**
- Observation: `CliRunner.invoke(env={"ARCHON_SEARCH_API_KEY": token})` uses Click's `isolation()` context, which temporarily sets `os.environ["ARCHON_SEARCH_API_KEY"]` for the duration of the invoke call. When the CLI-under-test calls a `TestClient` that is running in the same process, the server-side route handler (`os.environ.get(ENV_VAR)`) sees the env var and returns 409. Fix: pass the API key via `--api-key` flag (CLI option) instead of via the env dict in integration tests where the server checks that same env var.
- Action: In any CLI integration test that calls a server endpoint that checks `os.environ.get(X)`, never pass X via CliRunner's `env={}`. Use an explicit CLI flag (`--api-key`, `--api-url`, etc.) instead to avoid CliRunner's isolation contaminating the server's env check.
- Confidence: high

**2026-06-23 — D7 T-3: born-expired proxy is the wrong proof for post-grace rotation rejection**
- Observation: `test_e2e_rotate_grace_window` initially used a "born-expired" key created via `POST /keys` as a proxy to prove "old token fails after grace window." This is wrong: the born-expired key is a namespace-scoped managed key that enters `active_keys()` filtering only. The rotated old default key has a different middleware path — after rotation, `app.state.api_key` is updated to the new token, so the old token never matches the legacy fallback. The correct proof is: patch `archon_search.key_manager.datetime` to advance past `expires_dt`, then assert the actual `managed_token` returns 401. The `km_mod` patch is the load-bearing one; patching `middleware_auth.datetime` is dead code in this scenario.
- Action: For any test that must prove a rotated key fails after its grace window, patch `archon_search.key_manager.datetime` (not middleware_auth) and assert the actual rotated token returns 401. Never use a different key as a proxy. The middleware's legacy-fallback expiry guard is only entered when the submitted token matches `current_api_key`, which is not the case for the old token after a successful rotation.
- Confidence: high

**2026-06-23 — Documentation-only fix pass: read exact file text before every Edit**
- Observation: Several Edit calls failed with "String not found" because the old_string had subtle whitespace or line-break differences from the actual file. Reading the specific lines just before each Edit (using the offset+limit form of Read) eliminates this class of failure entirely.
- Action: For any documentation fix pass with many targeted edits, always Read the exact surrounding lines from the file before writing the Edit old_string. Never construct old_string from memory or from an earlier read of a different section.
- Confidence: high

**2026-06-23 — D7 T-3: `caplog` captures TestClient background-thread logs correctly**
- Observation: `TestClient` runs the ASGI lifespan (including `KeyStore.load()` which emits the corruption ERROR) in a background thread. `caplog.at_level(logging.ERROR, logger="archon_search.key_manager")` correctly captures those logs because Python logging handlers are process-global. The `caplog.at_level` context must wrap the entire `TestClient(app) as client:` block, not just the post-startup assertions.
- Action: For any e2e test that must assert logs emitted during ASGI lifespan startup, wrap the `TestClient(app) as client:` context with `caplog.at_level(...)`. The lifespan runs during `TestClient.__enter__()`, so the context must start before the `with TestClient(app)` line.
- Confidence: high

**2026-06-24 — D9 T-2: MCP ingest_file/ingest_directory are synchronous — no polling needed**
- Observation: The plan spec said "poll until DONE" for the `ingest_file` MCP tool. Both `ingest_file` and `ingest_directory` are synchronous blocking tools that return `IngestResultSchema` / `list[IngestResultSchema]` directly in the HTTP response. No background job is enqueued. The "poll until DONE" wording was carry-over from the REST `/ingest` endpoint which does enqueue jobs.
- Action: For MCP tools that call `pipeline.ingest_file`/`pipeline.ingest_directory` directly, assert `status='ok'` in the tool response — no polling loop needed. If the tool ever changes to enqueue a job, it returns `job_id` instead of `status`, which breaks these assertions clearly.
- Confidence: high

**2026-06-24 — D9 T-2: SSE parser should return last `data:` line, not first**
- Observation: The T-1 pattern returned the first `data:` SSE line. For round-trip tests asserting on content (not just shape), this is fragile — progress events before the final result would cause silent wrong-payload failures. Safer pattern: collect all `data:` lines and return the last one.
- Action: In MCP tests asserting on response content (not just shape-validity), use `data_lines[-1]` (last SSE event). T-1 still uses the first-line pattern — note the divergence for future cleanup.
- Confidence: high

**2026-06-24 — D9 T-2: tester-role e2e tasks for completed backend work start green immediately**
- Observation: T-2 passed immediately on first run. This is correct — the red phase only applies when tests are written before the implementation. For tester close-out tasks, immediate green means the implementation is correct.
- Action: For tester-role close-out e2e tasks, expect immediate green. Still run to confirm — a failure indicates an implementation bug, not a TDD failure.
- Confidence: high

**2026-06-25 — D8 BE-1: config-only task — iterative review correctly dismissed deferred doc items**
- Observation: Multiple DA and Brooks-Lint agents flagged `archon-search.toml.example` and CLAUDE.md as missing (Major/Moderate). These are explicitly listed in the plan's "Documentation update" section as T-4 (close-out) deliverables — NOT BE-1 deliverables. The plan's own documentation list, not the brief's general "every code change requires docs" principle, determines scope for individual tasks inside a multi-task plan. The one actionable finding was C1-I-3: no test for `[telemetry]` section present but `hash_doc_ids` key absent — a real gap that the review correctly surfaced.
- Action: When iterative-review agents cite missing docs/example files for a task inside a multi-task plan, check the plan's "Documentation update" section first. If the item is listed there under a later close-out task, dismiss it as out-of-scope for the current task. Only the one substantive gap (key-absent-from-section test) needed fixing.
- Confidence: high

## What Has Failed

**2026-06-15 — Mocking `asyncio.wait_for` to simulate timeout**
- Observation: Patching `asyncio.wait_for` directly (e.g. `side_effect=asyncio.TimeoutError`) leaves the inner coroutine (the `embed` AsyncMock) unawaited, producing `RuntimeWarning`. The mock intercepts the call before the real `wait_for` can await the coroutine.
- Action: Never patch `asyncio.wait_for` to simulate timeouts. Instead, make the coroutine itself raise the target exception (`AsyncMock(side_effect=asyncio.TimeoutError)`) so the real `wait_for` propagates it cleanly.
- Confidence: high

**2026-06-15 — Agent names with dots**
- Observation: The Agent tool name parameter rejects dots. `ReviewC18-2.1` fails; `ReviewC18-21` works. The regex is `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.
- Action: Never use dots in agent names; replace with nothing or an underscore.
- Confidence: high

**2026-06-15 — Generating commit message without committing**
- Observation: When a user says "commit X using the /commit-message format", running the skill and stopping at the message is incomplete — the user expects the full action: generate + commit.
- Action: "commit X using /commit-message" → `git add X` → `Skill("commit-message")` → `git commit` with the generated message, all in one flow. No pause.
- Confidence: high

## Patterns and Preferences

**2026-06-22 — D6 BE-9: config-example doc-only task held to spec with no drift**
- Observation: BE-9 (add `# validation_timeout_seconds = 60` under `[database]` in `archon-search.toml.example`) is a config-example doc-only task — correctly skips the TDD cycle per the implement-next Step 2 carve-out. The comment text was verified line-by-line against `config.py` (field default `60` at line 98; the `> 0` guard + warning + fallback at lines 294-304) before writing it, so the example matches the real `_apply_toml` behaviour exactly. All three DA agents (correctness, design, scope) returned zero Critical/Major/Moderate — only two Minor verbosity/voice nits, which were left as-is because the extra fallback/endpoint context is operationally useful for the one key in the file with silent fallback. Full suite ran green (5062 passed) even though the change touches no code; `tests/test_config.py` (129 tests) is the direct guard for the documented parsing.
- Action: For `.toml.example` doc tasks, verify every claimed default/bound/behaviour against the actual config parser before writing the comment — never describe config behaviour from the plan prose alone. Keep the block consistent with the file's `# key = default` convention and blank-line isolation. Minor verbosity nits from DA review are not blockers for doc-only changes.
- Confidence: high

**2026-06-22 — D6 FE-1: post-prewarm reranker validation in the wizard**
- Observation: (1) `validate_providers_shared` instantiates `TextCrossEncoder(...).rerank(...)`, which downloads the reranker model on first use — so the Step 9 GPU gate (before `_prewarm_models`) would trigger a blocking download if it probed the reranker. FE-1's post-prewarm placement makes the probe cheap (files cached). (2) The METAL branch is the only one that should set the `gpu_provider` sentinel used by the FE-1 block; the CUDA branch must leave it `None` because CUDA install-time validation is explicitly out of scope in the D6 plan. A DA agent correctly flagged setting it for CUDA as a scope violation. (3) `validate_providers()` returns a combined `embedder_ok and reranker_ok` bool — it cannot isolate which model failed, so the warning message must name the provider, not blame the reranker as the certain cause. (4) Splatting a tuple of context managers into a single `with (*tuple,)` fails with `TypeError: 'tuple' object does not support the context manager protocol` — use an `ExitStack`-based `@contextmanager` helper instead. (5) The minimal-multilingual profile (the only no-reranker profile) triggers the fasttext license gate; tests using it must pass `accept_fasttext_license=True` and patch `_download_fasttext_model`.
- Action: For wizard GPU-provider work: gate post-prewarm probes on a METAL-only sentinel; never extend probes to CUDA without a plan change. When a combined-bool validator can't attribute a failure, word warnings about the provider/consequence, not a specific model. Share multi-patch test setups via an `ExitStack` context-manager helper, never tuple-splat into `with`.
- Confidence: high

**2026-06-22 — D6 BE-2: Pydantic `model_` field prefix does NOT auto-warn**
- Observation: A `model_validation` field on a Pydantic v2 `BaseModel` (`StatusResponse`) does NOT trigger the protected-namespace warning, because Pydantic only flags `model_*` field names that actually collide with a real `model_*` attribute/method (model_dump, model_config, etc.). Verified empirically with `uv run python -W error -c "from ... import StatusResponse"` — clean import. A devils-advocate agent flagged this as a Major issue reasoning from training data; the empirical check refuted it. No `model_config = ConfigDict(protected_namespaces=())` is needed for `model_validation`.
- Action: When a DA agent claims a Pydantic protected-namespace warning, verify with `uv run python -W error` before adding `model_config`. Do not add the workaround speculatively.
- Confidence: high

**2026-06-22 — D6 BE-2: timestamp type choice — follow the TSP contract, not legacy str convention**
- Observation: All 9 existing timestamp fields in `schemas.py` use `str | None`, but the D6 C1 TSP contract specifies `utcDateTime` and the source dataclass `ModelValidationResult.validated_at` is `datetime | None`. Used `datetime | None` to mirror the contract + dataclass faithfully (the binding cross-role agreement), not the legacy `str` fields which predate the contract. JSON-mode serialization produces correct ISO-8601 (`"2026-06-22T12:00:00Z"`); verified via `model_dump(mode="json")`. Added an explicit JSON-mode serialization test since `datetime` is novel in the response-schema layer.
- Action: When a new schema field has a binding TSP contract + source dataclass type, mirror those over the legacy in-file convention. Always add a `model_dump(mode="json")` assertion when introducing a `datetime` field to a response model (REST path uses JSON mode; non-JSON `model_dump()` round-trip does not catch serialization regressions).
- Confidence: high

**2026-06-17 — Team plan generation (D3) — TypeSpec + role mapping**
- Observation: (1) `namespace` is a reserved keyword in TypeSpec — using it as a model field name fails to compile; rename to e.g. `jobNamespace`. Core-construct `.tsp` files (model/enum/interface, no `@typespec/http` import) compile standalone with `tsp compile <file> --no-emit`. (2) This repo has no GUI: the `/plan-maker-for-team` Frontend role is always N/A — Presentation (FastAPI routes, Pydantic schemas, Click CLI) is server-side Python owned by Backend.
- Action: For archon-search team plans, mark Frontend N/A and fold Presentation into Backend; when authoring TypeSpec contracts, avoid reserved keywords (`namespace`, `interface`, `model`, etc.) as field names and validate each file before referencing it.
- Confidence: high

**2026-06-20 — BE-3 iterative review: DA agents found `update_description` called unconditionally on all-failures path**
- Observation: `ingest_directory()` wrote `last_indexed=datetime.now(UTC)` via `update_description` even when all files failed to parse. The FTS rebuild was already guarded with `any(r.status == "ok" for r in results)` but the description block wasn't. DA review in iterative-review caught this.
- Action: Always guard ALL post-loop metadata writes with the same "at least one success" check used for FTS rebuild. Mirror the FTS guard pattern for every downstream update block.
- Confidence: high

**2026-06-20 — Deprecated shim returning True doesn't help if the underlying store.py also gates on the config flag**
- Observation: Adding `_centroid_incremental_enabled` returning `True` always in pipeline.py doesn't un-gate `store.py`'s conditional at `store.py:1516` which reads `self._config.centroid_incremental_enabled`. Users with `centroid_incremental_enabled = false` in TOML still lose centroid maintenance silently until BE-4 removes the flag from config/store.
- Action: When adding a deprecated shim for a feature flag, check whether the flag also gates logic in lower layers independently. If so, document it as a BE-4-style follow-up or address it in the same PR.
- Confidence: high

**2026-06-20 — D4 plan review: `_do_update_meta_on_add` is NOT stateless across batches**
- Observation: A plan claimed `store.ingest_chunks()` is "confirmed stateless per call." It is stateless for the chunk-table write (`_do_ingest`) but NOT for the metadata update (`_do_update_meta_on_add` reads and writes `doc_count`, `chunk_count`, `centroid_sum` on every call). Batching a single file into N batches inflated `doc_count` by N instead of 1. Fix: add `_is_continuation: bool = False` to `ingest_chunks()`; continuation batches skip the doc_count increment.
- Action: Before declaring any store method "stateless per call" in a plan, grep for what fields `_do_update_meta_on_add` or equivalent touches. Metadata side-effects break batching invariants silently.
- Confidence: high

**2026-06-20 — `list_chunks_raw()` is NOT a streaming / O(1) alternative to in-memory accumulators**
- Observation: `store.list_chunks_raw()` appears to be a generator but internally calls `table.query().to_list()` (store.py:2167) which materialises the entire collection into a Python list before yielding. Replacing `all_chunks` with `list_chunks_raw()` would have reintroduced the same O(corpus) memory problem D4 exists to fix. The fix: `store.sample_chunk_texts(n=100)` using `SELECT text LIMIT 100`.
- Action: Never assume a method is streaming based on its signature (async generator). Always check the implementation for `.to_list()` calls.
- Confidence: high

**2026-06-17 — Iterative review of a team PLAN (not code) — verify every claim against source**
- Observation: A team plan's defects are mostly *factual claims about existing code that are false* (wrong "in_place" classification of `migrate_per_collection_model`/`migrate_acl`, a nonexistent `add_or_update` LanceDB call, `list_queued_bulk`/`dispatch_fn` typed to `ExportJob|ImportJob` excluding a new job type, `/jobs/{id}/resume` 409ing non-export/import, `ReindexJob` having no `collection` field). DA agents that actually grep the cited files (`store.py`, `jobs/scheduler.py`, `types.py`, `routes_jobs.py`, `collection_meta.py`) find real, fixable problems; ones that reason from the prose alone do not. Convergence took 4 cycles because each fix exposed a deeper layer (e.g. specifying a resume mechanism then revealed the within-batch delete+add crash window).
- Action: For `/iterative-review` on a plan, instruct each DA agent to verify every file:line the plan cites and to hunt for newly-introduced contradictions from the prior cycle's edits. Use ONE fix agent per cycle (single shared markdown file — parallel editors collide). Skip the test-suite step when no code is touched. Prefer simplification fixes (removing the unused `MigrationSpec.target` field) over adding scaffolding.
- Confidence: high

**2026-06-15 — Merge strategy for review branches diverged from different parents**
- Observation: When review agents work from different parent commits, `git merge` risks conflicts from both the review diffs and the intervening main commits. The safer pattern is: `git diff <original-sha> <review-branch-tip> -- <files> | git apply` to extract only the incremental fix delta and apply it to main.
- Action: Use the patch-diff merge strategy (not `git merge`) when integrating review branches that diverged from commits already in main's history.
- Confidence: high

**2026-06-18 — Team plan generation with six parallel investigation agents (D3)**
- Observation: Six parallel investigation agents (architecture, contracts, scenarios, backend, frontend, tester) produce substantially better grounding than inline investigation because each agent specializes on one angle and cites exact file:line. The contracts agent found a specific discrepancy the brief missed: `routes_jobs.py` has an isinstance guard that would silently 409 a `MigrationJob` on resume — the brief said "no changes" to that endpoint. The tester agent correctly identified that `CliRunner` is in-process only and that `--wait` against a real TCP server must be manual. The scenarios agent confirmed crash-recovery and checkpoint behavior from the actual `_CRASH_STATUSES` set.
- Action: Always launch all six agents in parallel for plan-maker; wait for all before synthesizing. Inject the exact file paths of key symbols into each agent brief to avoid generic output. The frontend agent brief should explicitly note "no web UI" so it investigates the CLI/route Presentation layer instead of looking for views.
- Confidence: high

**2026-06-18 — TypeSpec reserved keywords (extended)**
- Observation: Beyond `namespace` (already recorded), field names that match TypeSpec keywords cause parse errors silently attributed to a downstream closing brace. Check field names against TypeSpec built-ins before compiling.
- Action: Scan model fields for TypeSpec keywords before writing `.tsp` files; always compile with `tsp compile --no-emit` and fix all errors before referencing the contract in the plan.
- Confidence: high

**2026-06-19 — BE-1 entity-layer implementation (D3)**
- Observation: `MigrationKind` was initially written with `snake_case` member names (`in_place`, `rewrite`, `export_rebuild`), deviating from `JobStatus` and `IndexingStatus` which use `UPPER_CASE` names. The deviation was caught by iterative review. With `str, Enum`, wire values (`.value`) and Python member names (`.name`) are independent — use `UPPER_CASE` names with snake_case wire values to match both conventions simultaneously.
- Action: For any new `str, Enum` in this codebase, use `UPPER_CASE` member names with the appropriate wire-format string value. Never use lowercase member names even when the wire value is lowercase.
- Confidence: high

**2026-06-19 — dataclasses.asdict() enum round-trip (D3 BE-1)**
- Observation: `dataclasses.asdict()` converts `str, Enum` fields to their `.value` (a plain `str`). Reconstructing a dataclass from the dict requires explicit coercion: `MigrationKind(raw_str)`. This is the pattern `JobStore._load()` must follow for `MigrationJob.kind`. Documented this as `test_migration_job_dict_round_trip_requires_kind_coercion`.
- Action: Whenever a new dataclass field uses a `str, Enum`, add a round-trip test documenting the `asdict()` → coercion → reconstruct pattern. Especially important for job types that go through `JobStore`.
- Confidence: high

**2026-06-19 — BE-3 implementation (D3 pending_migrations)**
- Observation: When `STORE_SCHEMA_VERSION=0` and all migrations have `introduced_at=0`, the "returns specs when behind" unit test can only exercise the filter path via an impossible production value (`schema_version=-1`). The test is valid scaffolding but provides no coverage of a production-reachable path. This is an inherent limitation of infrastructure-only releases and is acceptable when guarded by `assert STORE_SCHEMA_VERSION == 0`.
- Action: For any new `str, Enum` or infrastructure-only release task where the "happy path" is unreachable until future work: add `assert STORE_SCHEMA_VERSION == N` guard to affected tests and document in comments what the developer must update when the version bumps.
- Confidence: high

**2026-06-19 — Catalog integrity tests for migration catalogs**
- Observation: A static method returning a list of migration descriptors is a maintenance trap without invariant tests. Iterative review caught missing checks for: callable method existence (not just hasattr), monotonic ordering, unique names, and introduced_at <= STORE_SCHEMA_VERSION. All four invariants are load-bearing for BE-6 correctness.
- Action: For any migration catalog (`_all_migrations()` pattern), always add a catalog integrity test covering: non-empty, introduced_at bounds, unique names, monotonic ordering, and `callable(getattr(cls, spec.name, None))`. Do not use `hasattr` — it passes for non-callable attributes.
- Confidence: high

**2026-06-19 — BE-4 route implementation (D3 presentation layer)**
- Observation: The new `GET /{name}/migrations/pending` endpoint initially omitted the `_all_collection_paths(config)` config-path check present in all sibling `/{name}` routes (`get_collection_info`, `remove_collection`, `patch_collection`, `reindex_collection`). The omission was caught by iterative review (C1-I-ARCH-2). Orphaned collections (meta row in DB, path removed from config) would return 200 from the new endpoint but 404 from all others, creating an observable API inconsistency.
- Action: For any new `/{name}` route, apply the two-gate 404 pattern: (1) `_all_collection_paths(config)` check, (2) namespace-scoped `get_collection_meta` check. Both gates must be present and in that order, matching `get_collection_info`.
- Confidence: high

**2026-06-19 — BE-4 test coverage (D3 presentation layer)**
- Observation: The initial 404 test used a name not derivable from any configured path, so it exercised gate 1 (config-path miss). After the config-path check was added to the route, the meta-miss gate 2 had zero test coverage. The test comment was also misleading ("No meta → get_collection_meta returns None") when `get_collection_meta` was never actually called. A separate test for the "in config, no meta row" path is needed to cover gate 2 independently.
- Action: For any two-gate 404 pattern, write two distinct tests: one for config-miss (asserts `get_collection_meta.assert_not_called()`) and one for meta-miss (asserts `get_collection_meta.assert_called_once()`). Never rely on a single 404 test to cover both gates.
- Confidence: high

**2026-06-19 — BE-6 implementation (D3 apply_in_place_migrations + startup consolidation)**
- Observation: (1) When a method consolidates N previously-direct call-sites (e.g., 5 `migrate_*()` calls in `app.py`), all tests that patched those N individual methods break and must be updated to patch the wrapper instead — this is a wide blast radius that must be accounted for before implementing. (2) `_all_migrations()` catalog order and `_run_startup_migrations()` execution order were different (acl/centroid_sum/per_collection_model were swapped); iterative review caught the divergence. (3) Manual `CollectionMeta(...)` field-by-field copy is fragile when `CollectionMeta` gains new fields; `dataclasses.replace(meta, schema_version=X)` is the correct pattern. (4) An early-return guard `if not specs: return` is necessary to avoid unnecessary I/O and avoid bumping schema_version without applying any migrations.
- Action: When adding a consolidation wrapper method: (a) audit all test files that patch the consolidated methods and update them to patch the wrapper; (b) use `dataclasses.replace()` not manual copy when updating a dataclass field; (c) guard against the empty-input case early; (d) verify catalog/execution order matches between static catalog and dynamic execution code.
- Confidence: high

**2026-06-19 — BE-10 Interface Adapter implementation (D3 JobStore discriminator)**
- Observation: (1) When adding a new job subclass to `JobStore`, the `_write_atomic()` isinstance chain must check the new subclass BEFORE the `else: IngestJob` fallthrough — or before any parent class — otherwise subclasses serialize with the wrong `job_type` discriminator. (2) The `_load()` `str, Enum` field coercion (`MigrationKind(item["kind"])`) must be done after `setdefault` backward-compat guards and before the dataclass constructor call. (3) `list_queued_bulk()` adding a new bulk type creates a live scheduler path immediately — even before the dispatch handler is wired in BE-12. The safe interim fix is a `NotImplementedError` guard in `_real_dispatch` so the job fails with a clear message rather than an opaque `TypeError`. (4) `job_to_dict()` must include `kind` (not just `migrations_applied`/`backup_confirmed`) since `kind` is the primary discriminator for API consumers to understand the migration strategy.
- Action: For any new `IngestJob` subclass with `str, Enum` fields: (a) place it first in `_write_atomic()` isinstance chain; (b) coerce enum fields in `_load()` after setdefault guards; (c) include all subclass-specific fields in `job_to_dict()` via `getattr`; (d) add a `NotImplementedError` guard in dispatch if the handler is deferred; (e) always test `backup_confirmed=False` separately from `None` (identity check, not truthiness).
- Confidence: high

**2026-06-19 — BE-11 Presentation layer implementation (D3 JobResponse schema extension)**
- Observation: (1) `job_to_dict()` emitted `kind` as a raw `str, Enum` instance, not its `.value`. Pydantic's `str | None` field coerces it silently, but `JSONResponse(job_to_dict(...))` would crash with a JSON serialization error for any route that uses that pattern. Always serialize `str, Enum` fields with `.value` in dict serializers — never rely on Pydantic coercion as the only guard. (2) `job_to_dict()` added a new field (`kind`) but `JobResponse` omitted it — Pydantic's default `extra="ignore"` silently swallowed it. The tests validated only the two explicitly required fields and never asserted `kind`, so the gap was invisible. (3) Unused fixture parameters (`tmp_path`, `tmp_store`) were cargo-culted from adjacent tests; they created real filesystem artifacts on every test run for no benefit. (4) The `backup_confirmed=False` vs `None` edge case is mandatory per project learnings — always include it as a separate test for any `bool | None` Pydantic field. (5) The `migrations_applied=[]` vs `None` boundary is semantically load-bearing — "ran, applied nothing" vs "not a migration job" — and must be tested separately.
- Action: When adding nullable fields to a `JobResponse`-style Pydantic model from a `job_to_dict()` dict: (a) assert ALL new fields in tests, not just the two required by the task spec; (b) ensure `str, Enum` fields in `job_to_dict()` use `.value` serialization; (c) add `backup_confirmed=False` and `migrations_applied=[]` tests for any `bool | None` or `list | None` field; (d) drop unused fixtures from unit tests that don't need filesystem state.
- Confidence: high

**2026-06-19 — Code review fix application (D3 BE-12 post-review)**
- Observation: (1) The 409 reindex-active guard must come BEFORE apply_in_place_migrations in the rewrite path — applying side-effecting in-place migrations and then returning 409 leaves the collection in a partially-migrated state with no job to track it. (2) `dispatch_fn(job)` passing the pre-transition (QUEUED) job object means the dispatcher's task receives a stale status; always pass `promoted` (the post-transition object returned by `store.transition()`). (3) When renaming a module-level error dict (`_ERROR_401_404_422` → `_MIGRATE_ERROR_RESPONSES`), verify with grep that no other files import it before renaming. (4) The `progress` field lives on `IngestJob` base class and is inherited by all subclasses including `MigrationJob` — no need to add it separately.
- Action: For any route that applies side effects before a 409 guard: move the guard first. For scheduler dispatch, always pass the return value of `store.transition()` (the promoted object), not the original queued object. Always grep for cross-file references before renaming module-level constants.
- Confidence: high

**2026-06-19 — BE-13 resume handler broadening (D3)**
- Observation: (1) FastAPI uses function docstrings as the `description` field in the OpenAPI spec — changing a docstring in a route handler triggers an OpenAPI snapshot diff. Always regenerate the snapshot with `uv run --python 3.12` when touching route docstrings. (2) `_migration_task` restarts `apply_rewrite_migration` from scratch on every dispatch (including resume) — the progress checkpoint is stored for observability and idempotency documentation, not for offset resumption. Tests that claim "checkpoint resume" but use an unconditional fake must be renamed to avoid misleading future developers. (3) When adding a new job type to an isinstance allowlist in a resume/dispatch handler, always add tests for every non-FAILED terminal state (QUEUED, DONE) to cover the status-gate rejection path.
- Action: For any route whose isinstance guard is broadened: (a) update the docstring and error message to match; (b) regenerate the OpenAPI snapshot; (c) add tests for all non-FAILED terminal states; (d) add a test explicitly documenting the absence of file-existence checks if the new type has no file dependency.
- Confidence: high

**2026-06-19 — BE-14 CLI flag validation ordering (D3)**
- Observation: When multiple flags are mutually exclusive or have dependency relationships (e.g., `--dry-run` + `--apply` mutex, `--backup-first` requires `--apply`, `--wait` requires `--apply`), the validation order matters for error message quality. `--dry-run --backup-first` hits the "backup-first requires --apply" check before the "dry-run + apply" mutex check, producing a misleading message that suggests adding `--apply` (which dry-run forbids). An explicit `--dry-run + --backup-first` check with a clearer message must come before the generic `--backup-first` requires `--apply` check.
- Action: For any new CLI flag with multiple dependency relationships, enumerate all problematic flag combinations explicitly and add a dedicated check with a clear, actionable error message for each. Do not rely on catch-all `X requires Y` checks to cover compound invalid combinations.
- Confidence: high

**2026-06-19 — Module-level constants in CLI files (D3 BE-14)**
- Observation: When adding module-level constants (like `_POLL_INTERVAL_SECONDS`, `_TERMINAL_STATUSES`) to a file that already has a `_DEFAULT_API_URL` constant, the new constants should go immediately after the imports alongside `_DEFAULT_API_URL` — not between import groups. The fix agent placed them mid-import block (between stdlib and third-party imports), which is a PEP 8 violation caught by iterative review.
- Action: Always place module-level constants after ALL imports, never between import groups.
- Confidence: high

**2026-06-19 — BE-15 Presentation layer implementation (D3 StatusResponse extensions)**
- Observation: (1) When a new route field is computed from a constant that currently equals the Pydantic model's default (e.g., `STORE_SCHEMA_VERSION=0` and `store_schema_version: int = 0`), tests that assert `response_value == constant` are tautological — they pass even if the route never sets the field. Always patch the constant to a non-default value in tests for such fields. (2) When adding a new status computation that iterates already-fetched data (e.g., `ns_meta` from `get_all_collections_meta()`), never call a method that re-fetches the same data per item — use the in-memory objects directly. The plan may say "populate from `method()`" but the intent is the aggregate count, not the method call itself. (3) Existing mock stores that don't implement newly-called `search_store` methods break all tests using those mocks; always grep for all mock factories in the test file and add the new AsyncMock to each one before running the suite.
- Action: For any new route field derived from a constant, patch the constant in tests. For status endpoint additions, prefer in-memory computation over per-collection async calls when the data is already fetched. When adding new `search_store` method calls to a route, grep for all mock factory functions in the test files and add the missing mock to each.
- Confidence: high

**2026-06-20 — Team plan generation for D4 (pure backend refactor, no public API changes)**
- Observation: (1) For D4 (a pure Use Cases / Frameworks & Drivers refactor), all six investigation subagents returned high-quality findings. The contracts agent correctly identified that `list_chunks_raw()` (store.py:2147) exists and can replace the `all_chunks` accumulator as the text source for `generate_description()` — this was the only genuine open question. (2) When a feature has no cross-role API boundaries (all-backend, Frontend=N/A), TypeSpec contracts apply only to internal behavioral guarantees (batch aggregation shape). Core TypeSpec constructs (model/interface, no HTTP imports) still compile clean standalone. (3) The `centroid_incremental_enabled` flag has 30+ test references — test cleanup is a significant work item that deserves its own task (BE-4) separate from the algorithmic refactor (BE-3). (4) The pre-B5 branch in `ingest_directory()` is not dead code in the sense that it has live test coverage (tests set `centroid_incremental_enabled=False`), but it IS dead in production since the default flipped to True at B5.
- Action: For all-backend refactors: (a) always check if a store read method exists before concluding an in-memory accumulator is the only text source; (b) count test references to removed flags before estimating task size; (c) TypeSpec contracts for internal-only features should use minimal models without HTTP decorators and validate clean with `tsp compile --no-emit`.
- Confidence: high

**2026-06-19 — T-1 e2e test for D3 migration flow**
- Observation: (1) `GET /jobs?kind=migration` is a vacuous assertion — `"migration"` is not in `_KIND_TYPE_MAP`, so `total == 0` always passes. Use `job_store.list()` + `isinstance(j, MigrationJob)` instead. (2) When seeding an impossible DB value (`schema_version=-1`) to force pending migrations, the ingest background job from `POST /collections/` can race and overwrite the seeded value via `update_collection_meta`. Always poll the ingest job to `DONE` before seeding. (3) `asyncio.run()` inside a `make_real_app` TestClient context is safe — TestClient's event loop runs in a background thread; the main thread has no running loop. (4) `expected_migration_names` must filter to `IN_PLACE` kind to mirror the route's `in_place_specs` filter; using unfiltered `_all_migrations()` breaks when a REWRITE spec is added later.
- Action: For any e2e test that seeds DB state after HTTP registration: poll the registration job to DONE first. For `kind=migration` job count assertions: use `job_store.list()` + `isinstance`. For migration name lists: filter by kind to match route filtering.
- Confidence: high

**2026-06-19 — T-2 concurrent 503 e2e test (D3)**
- Observation: (1) To test "503 while rewrite holds lock" in a TestClient context, patch `apply_rewrite_migration` to acquire the per-collection lock via `search_store._lock_for(collection)`, signal a `threading.Event`, then `await loop.run_in_executor(None, allow_release_event.wait)` before releasing. This works because TestClient's event loop runs in a background thread — `lock_held_event.wait()` from the main thread blocks only the main thread, letting the background event loop run the task and acquire the lock. (2) Lower `INGEST_LOCK_TIMEOUT_S` via `monkeypatch.setattr(_constants, "INGEST_LOCK_TIMEOUT_S", 0.05)` so the 503 triggers quickly (default is 30s). (3) Freshly-registered collections with no ingested documents have no LanceDB table yet — `apply_rewrite_migration`'s `db.open_table(collection)` raises `ValueError: Table not found`. For zero-chunk tests, patch `apply_rewrite_migration` to return 0 instead of using the real implementation.
- Action: For any e2e test requiring lock contention: use `threading.Event` pair + `run_in_executor` to coordinate main and event-loop threads. Always lower timeout constants via `monkeypatch.setattr` (not `monkeypatch.setenv`) for module-level constants. For empty-collection rewrite tests, patch the implementation rather than wrestling with table-not-found.
- Confidence: high

**2026-06-20 — T-3 e2e crash recovery and resume tests (D3)**
- Observation: (1) The plan spec says "force RUNNING → FAILED via direct job_store.update()" — the initial implementation skipped RUNNING and went QUEUED→FAILED directly. The fix is to add `job_store.transition(job_id, {QUEUED}, RUNNING)` before the crash injection to match the real crash scenario. (2) CancelledError inherits from BaseException (not Exception), so `except Exception` in `_migration_task` does NOT catch it — real task cancellation would leave the job in RUNNING forever. Using RuntimeError→FAILED is the correct approach to test "exception during rewrite → schema_version not updated". (3) When `STORE_SCHEMA_VERSION=0` and schema_version defaults to 0, seeding `-1` makes the FAILED (unchanged) vs DONE (updated to 0) paths distinguishable — same pattern as T-1 used. (4) `asyncio.run()` from the main thread alongside make_real_app TestClient is an established codebase pattern (15+ existing tests); architecturally debatable but accepted.
- Action: When crash-injection tests force a RUNNING→FAILED transition via `job_store.update()`, always add the explicit QUEUED→RUNNING transition first via `job_store.transition()`. For schema_version tests where STORE_SCHEMA_VERSION==0, seed -1 to make the before/after states distinguishable.
- Confidence: high

**2026-06-20 — T-4 e2e test for pre-D3 startup migration (D3)**
- Observation: (1) Seeding an EMPTY pre-D3 meta table (no rows, no schema_version column) does NOT prove S3 — `add_columns({"schema_version": "cast(0 as bigint)"})` applies to rows, not an empty table. The schema_version=0 seen after POST /collections/ comes from the CollectionMeta dataclass default, not from the migration. Must seed at least one row. (2) When a seeded meta row creates a 409 on POST /collections/ (name already registered), use `cfg.collections.append(str(col_path))` to pass the config-path gate directly without going through the route. (3) `caplog.at_level` wraps both the TestClient context AND the assertion block — records from the ASGI background thread are captured because Python logging handlers are global to the process. (4) The "Concurrent migration" exclusion should be an explicit zero-count assertion, not a silent filter — it is the signal that a migration ran twice unexpectedly.
- Action: For any e2e test involving pre-D3 DB seeding: (a) always seed at least one data row; (b) read that row back via direct LanceDB read BEFORE any HTTP call to prove the migration's default (not the dataclass default) was applied; (c) use cfg.collections.append() when POST /collections/ would 409 due to a pre-existing meta row.
- Confidence: high

**2026-06-20 — T-6 close-out (D3)**
- Observation: (1) The `[jobs].max_concurrent_bulk` comment in `archon-search.toml.example` said "export/import" but `MigrationJob` (D3 BE-10) joins the same `list_queued_bulk()` path — the example comment was stale after BE-10. (2) The architecture doc `130_data_architecture_and_persistence.md` listed only three startup migrations (`migrate_namespace`, `migrate_acl`, `migrate_per_collection_model`) — but the store has five (`migrate_description_embedding` and `migrate_centroid_sum` were missing). Close-out fact-checking catches doc drift that code review misses. (3) `STORE_SCHEMA_VERSION = 0` means no acceptance criterion test can exercise the "pending migrations > 0" code path in production — the tests must either use an impossible value (`schema_version=-1`) or bypass the constant. This is an inherent constraint of infrastructure-only releases and is documented in `learnings.md` under BE-3. (4) `JobResponse.result` was typed `str | None` but `IngestJob.result` at the domain layer is `dict | None`. The mismatch was latent in all prior job types (which left `result=None`) but surfaced immediately with `MigrationJob` which sets `result={"migrated_chunks": N}`. The fix is `str | dict | None` to match the domain model.
- Action: During close-out, always re-read `.toml.example` comments against the actual `list_queued_bulk()` type guard to catch stale wording. When documenting startup migrations in architecture docs, grep `store.py` for `_all_migrations()` rather than listing from memory. When adding a new job type that sets a non-None `result`, verify that `JobResponse.result` accepts the actual runtime type — never assume `str | None` is correct just because it compiled.
- Confidence: high

**2026-06-20 — Fixing flaky tests under parallel xdist load**
- Observation: (1) A session-scoped autouse fixture writing to a shared repo file (tests/eval/corpus/pdf-fixtures/three_page.pdf) creates a race condition under xdist: multiple workers all call reportlab Canvas.save() on the same path simultaneously, truncating the file mid-write while another worker's compute_eval_hash reads it. The fix is an early-exit guard (if file exists: return) since the PDF is byte-deterministic and committed to git — plus an atomic write pattern (temp file + rename) for the case where the file is absent. (2) Integration tests relying on a scheduler asyncio event loop ticking at 0.1s with timeout_s=15.0 can fail under CPU starvation when 14 xdist workers are all running simultaneously. Adding xdist_group("benchmark") to such tests serializes them on a single worker, preventing concurrent CPU competition. (3) The established convention for scheduler-timing-sensitive tests is timeout_s=30.0 (see test_dispatch_scheduler_e2e.py) — new tests should match this, not use 15.0.
- Action: For any fixture that writes to a shared source-tree file, add an early-exit if the file exists, then write atomically (temp + rename). For any integration test that relies on an asyncio event loop tick (scheduler, backup loop) within a timeout, add xdist_group("benchmark") to serialize it and avoid CPU starvation failures.
- Confidence: high

**2026-06-20 — D4 T-1 batched ingest e2e test**
- Observation: With the GPT-2 tokenizer at chunk_size=512 and paragraphs of ~2380 chars (~595 tokens), each paragraph reliably produces exactly 1 chunk. 620 paragraphs (512 alpha + 108 beta) → 620 chunks, triggering the second ingest batch cleanly. The test ran in 4.06s, well within the 120s timeout. FTS finds the distinct tokens deterministically even though the stub embedder returns zero-vectors for all chunks.
- Action: For any batched ingest e2e test, use paragraphs of ≥2100 chars so each exceeds the 512-token chunk boundary independently. Use distinct tokens-per-section rather than section offsets so FTS assertions are immune to chunker overlap variations.
- Confidence: high

**2026-06-21 — D4 T-2: tracemalloc memory-bounds test design**
- Observation: (1) `tracemalloc.stop()` must be in its own nested `finally` block independent of store cleanup — if `ingest_directory` raises, the outer `finally` calls `store.disconnect()` but only the inner `finally` guarantees `tracemalloc.stop()`. Without this, leaked tracing state contaminates subsequent xdist worker tests. (2) The discriminating signal for a text-string accumulator regression at `_PARAGRAPHS_PER_FILE=100` and `_FILES_LARGE=10` is ~620 KB (10 files) vs ~62 KB (1 file) of text strings — a 10× ratio that easily exceeds the 3× threshold. Stub embedder vectors (4 floats = 32 bytes each) are too small to be the primary signal; text strings are. (3) Chunk-count preconditions (`sum(r.chunks_created) >= _MIN_CHUNKS_PER_FILE`) are mandatory to guard against vacuous passes when the parser silently produces 0 chunks. (4) `gc.collect()` between measurement runs is important for allocator arena flushing — without it, the second peak may include residual heap from the first run. (5) Two separate `make_real_pipeline` pairs with separate `tmp_path` subdirectories ensure the second measurement doesn't inherit LanceDB metadata state from the first.
- Action: For any tracemalloc-based regression test: (a) put `tracemalloc.stop()` in an inner `finally` independent of other cleanup; (b) add chunk/data-count preconditions to prove the measurement is meaningful; (c) call `gc.collect()` between runs; (d) quantify the expected signal size and verify it exceeds the threshold assuming the regression is present; (e) use `xdist_group("benchmark")` to prevent CPU starvation from skewing measurements.
- Confidence: high

**2026-06-21 — D4 T-3 close-out: documentation audit patterns**
- Observation: (1) The 110 component catalog's `pipeline.py` description did not mention the batch loop or the removal of corpus-wide accumulators — it still said "Computes per-collection centroid on directory ingest" without distinguishing per-batch from per-directory accumulation. The doc update task plan correctly identified this as needing an update. (2) All architecture docs (130, 210) were already updated by BE-4. The toml example already had `centroid_incremental_enabled` removed. BREAKING.md already had the entry. The 110 catalog was the only doc requiring an update. (3) Third-party DeprecationWarning lines from docling (not our code) appear in the test suite — these are pre-existing and not caused by D4.
- Action: For any close-out that includes a doc update checklist, read the specific sections of each doc (not just grep for removed symbols) — docs may describe *behavior* that changed (e.g., accumulator pattern) without using the exact symbol names. Always update the component catalog's module-purpose cell when the module's primary data-flow behavior changes.
- Confidence: high

**2026-06-21 — D5 BE-1: config dataclass with validation — allowlist line-number drift**
- Observation: Adding a new dataclass block before an existing function shifts all subsequent line numbers. The `path_home_allowlist.txt` stores file:line:hash tuples — inserting `MaintenanceConfig` (11 lines) before `get_default_config_path` moved the `Path.home()` callsite from line 164 to 177, causing `test_path_home_ratchet` to fail. The hash was unchanged (same line content), only the line number differed.
- Action: After adding a new block to `config.py` (or any file in the allowlist), run `uv run pytest tests/test_no_hardcoded_path_home.py -n0 --no-cov` immediately and update the line number in `path_home_allowlist.txt` before running the full suite.
- Confidence: high

**2026-06-21 — D5 BE-1: ConfigError validation paths need dedicated tests**
- Observation: The plan spec listed 3 required tests (defaults, round-trip, warning). The implementation also added 3 ConfigError boundary checks (`interval_hours < 0`, `retry_max_attempts < 1`, `retry_max_age_hours < 0`). The DA review correctly identified these as undertested — TDD mandates tests first for ALL validation logic, including error paths, not just the happy paths the task spec enumerates.
- Action: For any task that adds both TOML parsing AND validation (ConfigError raises), always add tests for each error path even if the plan spec only lists the happy-path tests. The spec's test list is a minimum, not a ceiling.
- Confidence: high

**2026-06-21 — D5 BE-2: `_trigger_event.clear()` must come AFTER `_run_one_pass()`**
- Observation: The initial implementation cleared `_trigger_event` BEFORE calling `_run_one_pass()`. The plan explicitly requires clearing AFTER `_save_state()` completes (i.e., after the pass). Clearing before means a trigger that arrives during a long pass is silently lost. The fix: move `self._trigger_event.clear()` to after `await self._run_one_pass()`, unconditionally — so triggers that arrive during execution are coalesced into "already ran," not dropped.
- Action: For any event-driven loop where `_trigger_event.wait()` signals work, always clear the event AFTER the work completes, never before. This coalesces concurrent triggers correctly.
- Confidence: high

**2026-06-21 — D5 BE-2: `dict(module_level_dict)` shallow copy mutates nested dicts**
- Observation: `dict(_EMPTY_STATE)` creates a shallow copy where nested dicts (`collection_health: {}`, `retry_counts: {}`) are shared references to the same objects in `_EMPTY_STATE`. When `_run_one_pass` later mutates `health[key] = ...`, it permanently pollutes `_EMPTY_STATE` for the process lifetime. The fix: return an inline dict literal with fresh `{}` values instead of `dict(_EMPTY_STATE)`.
- Action: Never use `dict(module_level_dict)` as a "safe copy" when the module-level dict contains nested mutable objects. Always construct fresh dicts inline or use `copy.deepcopy()`.
- Confidence: high

**2026-06-21 — D5 BE-2: `test_trigger_loop_fires_on_interval_timeout` must test the real method**
- Observation: Initial implementation of this test created a local wrapper function that reimplemented the loop logic with a tiny timeout, rather than calling the real `_trigger_loop()`. This means regressions in the real method (wrong `timeout` computation, wrong clear placement) go undetected. The fix: patch `_SECONDS_PER_HOUR` to `0.05` with `patch.object(ml_mod, "_SECONDS_PER_HOUR", 0.05)` so `interval_hours=1` produces a 0.05s timeout and the real `_trigger_loop` runs.
- Action: When testing an async loop method's interval-timeout path, patch the seconds-per-unit constant to a small value and call the real method, not a wrapper.
- Confidence: high

**2026-06-21 — D5 BE-5: `_run_fts_optimize` lock-release testing pattern**
- Observation: (1) The plan-specified tests only covered the four behavioral cases (happy, FTSIndexNotFoundError, lock timeout, config disabled). Iterative review caught two critical gaps: no test verified the lock was released after `FTSIndexNotFoundError`, and no test verified propagation of unexpected exceptions with lock release. (2) To verify the lock is held DURING `optimize_fts`, use a `side_effect=AsyncMock` that asserts `lock.locked()` inside the call. (3) The `last_error` field in per-collection health state was never reset between passes — a transient error persisted indefinitely. Fix: add `col_health["last_error"] = None` at the start of each per-collection block in `_run_one_pass`, after the carry-over from previous state but before any policy execution. (4) Lock held through `optimize_fts` contradicts `store.py`'s documented convention (which releases before optimize in `delete_document`). The maintenance choice is defensible (infrequent, brief blocking acceptable) but must be commented.
- Action: For any lock-acquiring method: (a) add `assert not lock.locked()` after all test calls; (b) add a side-effect that asserts `lock.locked()` to verify the lock is held during the inner operation; (c) add a test for unexpected exceptions to verify the `finally` block releases the lock; (d) when the locking pattern differs from the established convention in the codebase, add an explicit comment justifying the deviation.
- Confidence: high

**2026-06-21 — D5 T-2: e2e FTS optimize observable via GET /status**
- Observation: `ingest_file_via_path` triggers `rebuild_fts_index()` in the pipeline after a successful ingest, creating the FTS index. When the maintenance loop subsequently calls `optimize_fts()`, it succeeds and writes `fts_optimized_at` to the health state. The e2e test just needs to ingest one small doc before triggering maintenance — no special setup for FTS index creation is needed.
- Action: For any D5 e2e test that exercises an FTS-dependent maintenance policy, ingest at least one real document first using `ingest_file_via_path`. The pipeline's post-ingest `rebuild_fts_index()` call is the precondition for `optimize_fts()` to succeed.
- Confidence: high

**2026-06-21 — D5 BE-6: orphan cleanup policy — per-path exception handling and FTSIndexNotFoundError in post-deletion optimize**
- Observation: (1) The initial implementation had no per-path try/except in the delete loop — a single `delete_by_source_path` failure would abort the loop leaving remaining orphans untouched and `orphans_removed_last_run` never updated. (2) The post-orphan `optimize_fts` call did not catch `FTSIndexNotFoundError`, inconsistent with `_run_fts_optimize`'s graceful handling. (3) Elapsed time was measured before Phase 4 (FTS optimize), excluding its time from the 60s warning. (4) The URL skip test only checked `delete_by_source_path.assert_not_called()` — vacuously correct but did not assert `Path` was never instantiated. (5) Three FTS tests used `fts_optimize=True` when the implementation's post-orphan optimize runs regardless of that flag — misleading.
- Action: For any maintenance policy with a per-path delete loop: wrap each delete in `try/except Exception` and log WARNING with path on failure. Add `FTSIndexNotFoundError` handling in any post-policy `optimize_fts` call (consistent with `_run_fts_optimize`). Measure elapsed AFTER all phases including FTS. For URL-skip tests, add `mock_path_cls.assert_not_called()` as the direct assertion. Use `fts_optimize=False` in tests that exercise the post-orphan FTS path to document that the flag is irrelevant to that phase.
- Confidence: high

**2026-06-21 — D5 BE-6: set iteration order in exception-continuation tests**
- Observation: `test_orphan_cleanup_delete_exception_continues_loop` used two orphan paths in a `set` with `side_effect=[RuntimeError, None]`. The test assertions (call_count==2, orphans_removed_last_run==1) are order-independent, but the test structure is fragile if future assertions need to know which path failed. The fix: `sorted(source_paths_seen)` in the implementation makes iteration deterministic everywhere.
- Action: When a `set` is iterated in a loop where per-item side-effects are mocked, sort before iterating so tests can reliably target specific items. This is a low-cost improvement (one `sorted()` call) with high auditability benefit.
- Confidence: high

**2026-06-21 — D5 T-3: `_SCHEDULER_TICK_SECONDS` monkeypatch is inert for `MaintenanceLoop`**
- Observation: T-1 and T-2 e2e tests cargo-culted `monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)`. This constant controls `JobScheduler.run()` (the bulk export/import job ticker), NOT `MaintenanceLoop._trigger_loop`, which uses `asyncio.wait_for(_trigger_event.wait(), ...)` — fired immediately when the trigger event is set. The monkeypatch has zero effect on maintenance pass timing.
- Action: For any e2e test that exercises `MaintenanceLoop` via `POST /maintenance/trigger`, do NOT monkeypatch `_SCHEDULER_TICK_SECONDS`. The event-based trigger fires immediately without any tick dependency.
- Confidence: high

**2026-06-21 — D5 T-3: e2e orphan test must verify chunks are gone, not just the counter**
- Observation: `orphans_removed_last_run` is a self-reported counter written by the maintenance loop itself. It could increment even if `delete_by_source_path` silently failed. To satisfy S8's "all chunks for that path removed" requirement, the e2e test must call `search()` (or equivalent) after cleanup and assert empty results — proving the data is actually gone, not just the counter.
- Action: For any e2e test verifying orphan cleanup: after polling `orphans_removed_last_run > 0`, also call `search(client, col, ...)` and assert results are empty. Counter + empty search together prove the behavior end-to-end.
- Confidence: high

**2026-06-21 — D5 BE-7: Pydantic `JobResponse` nullable fields become stale after base-class promotion**
- Observation: When `source` and `collection` moved from subclass-only fields (returned as `None` for base `IngestJob`) to `IngestJob` base class fields (returned as non-None strings), `schemas.py` `JobResponse` still declared them `str | None = None`. The OpenAPI spec generated nullable types for fields that can never be null after the change. The `BREAKING.md` noted the null→string change but the schema was not updated.
- Action: Whenever a field moves from subclass-only (`getattr(job, "field", None)`) to base-class direct access (`job.field`), immediately update the corresponding Pydantic response model from `T | None = None` to `T = default`. Regenerate the OpenAPI snapshot afterward. Never leave a `| None` on a field that `job_to_dict` always returns as a non-None value.
- Confidence: high

**2026-06-21 — D5 BE-7: `Literal` on dataclass fields requires `__post_init__` for runtime enforcement**
- Observation: Python `Literal` type annotations are static-only. `IngestJob(source="garbage")` succeeds silently at runtime. A test that only verifies valid values construct cleanly does not satisfy the plan requirement of "invalid source fails." Adding `__post_init__` with a frozenset check is the correct pattern for runtime enforcement of Literal constraints on dataclasses.
- Action: For any `str, Literal` field on a dataclass where invalid values would cause silent data corruption (e.g., persisted to JSON, drives filtering logic), add a `__post_init__` check backed by a frozenset of valid values. Test that invalid values raise `ValueError`. Do not leave "Literal enforcement" as a comment-only guarantee.
- Confidence: high

**2026-06-15 — Feature brief writing (D4)**
- Observation: The `/feature-refinement` skill enters a deliberation loop when the problem space has many sub-options. It can stall without a firm directive to write the file.
- Action: When spawning feature-refinement for a well-understood technical brief, include explicit instruction: "Do not ask questions — write the brief now, put all open items in Open Questions." This bypasses the multi-round clarification loop.
- Confidence: high

**2026-06-20 — BE-4 (D4): subagent codebase corruption recovery pattern**
- Observation: A subagent tasked with a narrow config-flag removal (BE-4) also deleted 340 lines of D3 migration code from `store.py`, removed `routes_collections.py` migration routes, deleted 6 source files (backup/export/scheduler modules), and wiped 51 test files — all unrequested. The blast was not visible in the staged diff before committing; the commit only showed the `+` side. Detection required comparing post-commit line counts against HEAD.
- Action: After any subagent commit, immediately compare post-commit line counts for every modified file against HEAD: `git show HEAD:<file> | wc -l` vs `wc -l <file>`. A subagent that removes more than 20 lines from a file not in its stated scope is a red flag. Restore all damaged files from HEAD using `git show HEAD:<file> > <path>` before applying the targeted changes. Never run a second subagent to "continue" over a corrupt state.
- Confidence: high

**2026-06-20 — BE-4 (D4): `_should_regenerate` must be patched in tests that assert description generation**
- Observation: `_should_regenerate(existing_count=0, new_count=0, existing_meta=None)` returns `False` when `chunk_count=0` (no-op guard). Tests for `ingest_directory` that mock `store` with `AsyncMock` return 0 from `ingest_chunks`, so description regeneration is gated off and `update_description` is called with `None`. Tests that assert `update_description` was called with a generated string must patch `_should_regenerate` to return `True`.
- Action: Whenever a test asserts that description generation ran, add `patch("archon_search.pipeline._should_regenerate", return_value=True)` to force the guard open. The integration test that uses real store state does not need this patch — only unit tests with mocked stores.
- Confidence: high

**2026-06-21 — D5 BE-8: pass-level policy must mutate caller's state dicts, not own its own save**
- Observation: `_run_failed_ingest_retry` initially had its own `_load_state()`/`_save_state()`. `_run_one_pass` then overwrote with its stale local vars, silently discarding every retry count increment. The root cause: the method was diverging from the pattern set by `_run_fts_optimize` and `_run_orphan_cleanup`, both of which mutate `self._current_health` (a reference set by the caller). Tests calling the method in isolation all passed because they never exercised the full `_run_one_pass` → method → final save sequence.
- Action: For any pass-level or per-collection policy added to `MaintenanceLoop`: it must receive mutable state dicts as parameters (or mutate instance-level refs like `self._current_health`) and never call `_save_state()` internally. Only `_run_one_pass` performs the single atomic save after all policies complete.
- Confidence: high

**2026-06-21 — D5 BE-8: use `type(job) is not SomeClass` for exact-type exclusion, not isinstance deny-list**
- Observation: The initial implementation used a deny-list `_NON_BASE_INGEST = (ExportJob, ImportJob, ...)` + `isinstance(job, _NON_BASE_INGEST)`. Any future IngestJob subclass not in the tuple silently passes through. Replace with `type(job) is not IngestJob` (exact type check) to make the allowlist behavior explicit and future-proof.
- Action: When filtering for base-class-only instances (not subclasses), use `type(obj) is BaseClass` not `isinstance(obj, BaseClass) and not isinstance(obj, (Sub1, Sub2, ...))`.
- Confidence: high

**2026-06-21 — D5 BE-8: deduplication in loops over all FAILED jobs for same path**
- Observation: `JobStore.list()` can contain multiple FAILED jobs for the same `{ns}/{col}/{path}` (original failure + a prior maintenance retry that also failed). Without a `seen_keys` guard, all are re-enqueued in the same pass, incrementing the count N times instead of 1. `max_attempts` is reached N× faster.
- Action: For any retry loop that iterates all FAILED jobs from `JobStore.list()`, add a `seen_keys: set[str]` tracker keyed by the retry key. Skip jobs whose key is already in `seen_keys` after processing the first occurrence.
- Confidence: high

**2026-06-21 — D5 FE-1: `--wait` polling must capture baseline BEFORE triggering**
- Observation: `_wait_for_pass` initially captured `original_last_run_at` AFTER the POST trigger returned. For fast passes (empty collections, dev/test env), the pass can complete in the <50 ms window, making the baseline already equal to the new value. The loop then exhausts `_WAIT_MAX_POLLS` with no change, returning a false timeout on success.
- Action: Always capture the polling baseline (e.g., `last_run_at`) BEFORE issuing the trigger POST. Pass it as a parameter to the wait function.
- Confidence: high

**2026-06-21 — D5 FE-1: `side_effect` mock count must account for pre-loop calls**
- Observation: `test_maintenance_run_wait_maintenance_null` provided 3 `side_effect` GET responses with `_WAIT_MAX_POLLS=3`. But `_wait_for_pass` (when baseline capture is done inside) calls `_get_last_run_at` once before the loop + 3 times in the loop = 4 total. The 4th call raised `StopIteration`, which CliRunner surfaced as a non-zero exit — passing the test for the wrong reason.
- Action: When mocking a `side_effect` list for a polling loop, count ALL calls including any pre-loop baseline call and any initial setup call. Or patch `_WAIT_MAX_POLLS` to a smaller value so the total stays within the mocked count.
- Confidence: high

**2026-06-20 — BE-4 (D4): `sample_chunk_texts` must be mocked in all store stubs**
- Observation: After BE-3 replaced the in-memory text accumulator with `store.sample_chunk_texts()`, any test helper that builds a mock store (`_make_mock_store_c1`, `_make_stub_store_for_embedding_tests`) must add `store.sample_chunk_texts = AsyncMock(return_value=[])` or a meaningful list. Missing this causes `AttributeError: 'AsyncMock' object has no attribute 'sample_chunk_texts'` at runtime in tests that exercise description generation.
- Action: After any new async method is added to `SearchStore`, grep all test files for mock store builders and add the new method as an `AsyncMock` attribute. Prefer `AsyncMock(return_value=[])` for collection-returning methods unless the test specifically exercises the non-empty path.
- Confidence: high

**2026-06-22 — plan-maker-for-team with six idle subagents (D6)**
- Observation: Six parallel plan-maker investigation agents (architecture, contracts, scenarios, backend, frontend, tester) were all spawned but went idle without routing findings back via SendMessage. The idle_notification is NOT a findings report. The skill has a fallback: perform the six investigations inline. Inline investigation (using Read + Bash on the real codebase) produced equivalent grounding quality to subagents and was faster once the agents were confirmed idle.
- Action: When investigation agents for plan-maker go idle without findings, do NOT wait or retry. Immediately fall back to inline investigation: read the brief, key source files, and existing patterns, then synthesize the plan directly. The fallback is explicitly documented in the skill.
- Confidence: high

**2026-06-22 — D6 iterative review of team plan: fix agent idle without summary**
- Observation: The fix agent went idle (idle_notification) without returning a summary message. Checking `git diff HEAD` revealed it had applied all changes correctly. The idle state was a notification artefact, not a failure. Cycle 2 reviewers caught one remaining Moderate (TypeSpec `.tsp` file signature mismatch) that the fix agent's prompt had not explicitly covered — the plan text was updated but the linked contract file was missed.
- Action: After a fix agent goes idle, always `git diff HEAD` the target file before assuming anything is missing. For plan reviews that also touch linked contract files (`.tsp`, `.yaml`, etc.), explicitly add those files to the fix agent's scope in the prompt — prose-only fixes will miss them.
- Confidence: high

**2026-06-22 — D6 iterative review: `EmbedderCache.preload()` does NOT warm `app.state.embedder`**
- Observation: S9 acceptance criterion claimed "With `eager_load_embedders = true`, embedder probe is skipped if `app.state.embedder.is_warm` is already true." This is wrong: `app.py:158-161` calls `EmbedderCache.preload()` which creates per-collection `Embedder` instances — it never touches `app.state.embedder`. DA review caught this because agents read the actual `app.py` lifespan code, not just the plan prose. The fix: `validate_models_async` takes an explicit `embedder_is_warm: bool = False` from the caller rather than reaching into `app.state` itself.
- Action: When writing acceptance criteria involving `is_warm` or eager-preload state, verify which object is actually warmed by the preload path before writing the criterion. `EmbedderCache.preload()` is not the same as warming `app.state.embedder`.
- Confidence: high

**2026-06-22 — git mv to Completed/ — stage both halves**
- Observation: A previous session moved D3/D4/D5 docs to Completed/ using a method that only staged the additions (new files in Completed/) but not the deletions from Backlog/. The commit 8ad5130 shows 12 file additions and 0 deletions. The Backlog/ files were deleted from disk but remained in the git index, showing as ` D` (working-tree deleted, unstaged) in git status.
- Action: When moving doc files between directories, always use `git mv` (which stages both the rename as delete+add atomically) rather than OS-level move + git add of the target. If OS-level move was already used, run `git rm <old-paths>` to stage the deletions before committing.
- Confidence: high

**2026-06-22 — D7 plan-maker: `namespace` as TypeSpec op parameter also fails**
- Observation: The `namespace` reserved-keyword issue extends to TypeSpec operation parameters, not just model fields. `interface KeyStoreWrite { create(namespace: string, ...) }` fails with the same parse error as a model field named `namespace`. Rename the parameter (e.g., `ns: string`) to avoid the keyword clash.
- Action: Audit ALL occurrences of `namespace` in `.tsp` files — model fields, op parameters, and local variables. Backtick escape model fields (`` `namespace`: string ``); rename op parameters to `ns` or another non-reserved name.
- Confidence: high

**2026-06-22 — D7 plan-maker: inline investigation is the first-class fallback when agents idle**
- Observation: Investigation agents idled again (same D6 pattern). Immediately switched to inline investigation using Read + Bash on the actual source files. The inline approach produced equivalent grounding and completed faster than retrying idle agents.
- Action: Do not send multiple retry messages to idle investigation agents. After the first idle notification with no findings, switch to inline investigation immediately — Read the key source files directly (key_manager.py, middleware_auth.py, app.py, config.py, one route file, one CLI cmd file, integration/conftest.py). The pattern is reliable and documented.
- Confidence: high

**2026-06-22 — D6 BE-1 (validate_providers_shared + validate_models_async)**
- Observation: (1) The BE-1 Tests block lists an integration test (`test_validate_models_async_pending_state_visible`) that requires `app.state.model_validation` set by the `app.py` lifespan — but lifespan wiring is BE-4's scope and BE-1 does not touch `app.py`. The integration test is correctly deferred to BE-4; the plan misplaced it under BE-1. (2) `validate_providers_shared` must be testable without onnxruntime/fastembed installed — extracting `_available_providers()` and `_load_cross_encoder()` module-level helpers (which tests patch) is justified, not over-engineering, because the new function's tests exercise the body (unlike install.py's `validate_providers` whose tests only mock the whole method). (3) `validate_models_async` catching `BaseException` (incl. `CancelledError`) and returning a result is plan-mandated; the shutdown re-raise is BE-4's wrapper responsibility — do NOT "fix" this in BE-1. (4) `providers or None` coerces the SearchConfig default `[]` to None so fastembed picks its CPU default; add a comment, this is correct and matches the empty `non_cpu` gate.
- Action: For BE-1-style "Use Cases function + DTO" tasks: implement only the unit tests for the function logic; defer any integration test that depends on a later task's wiring (note it in the report). Patch module-level helper seams, not leaf imports. Add tests for the production-default path (`providers=[]`), both-probes-fail, the empty-model skip branch, and `CancelledError` even when the plan's test list omits them (the list is a minimum).
- Confidence: high

**2026-06-22 — D6 K1 (kickoff/contract-ratification task) via implement-next**
- Observation: K1 is a #team alignment task with no source-code output and an empty Tests block, so TDD is skipped per implement-next Step 2. The real value of the task is the contract-ratification GATE: DA reviewers reading the actual `.tsp` files + source caught that `D6-model-validation-status.tsp` omitted the `config` param and used `float64` where the plan/config say `int`, and that `D6-readiness-extension.tsp` lacked the mandatory PENDING-default note. These are exactly the defects K1 exists to catch before BE-1 implements. Validate suspected feasibility/count claims with tools before "fixing" — `TextCrossEncoder` DOES accept a `providers` kwarg (inspect.signature) and the "~70 patch sites" claim is just approximate (actual 92), so neither needed a change.
- Action: For contract-ratification kickoff tasks, do not rubber-stamp pre-authored `.tsp` files. Read each `.tsp` against the plan prose AND the existing code it extends; reconcile param lists, types, and required defaults. `tsp compile --no-emit` proves syntax only, never plan/code alignment. Verify quantified/feasibility claims with grep/inspect before editing.
- Confidence: high

**2026-06-22 — D6 BE-3: warn-and-fallback config field diverges from sibling raise-on-invalid pattern (by spec)**
- Observation: `validation_timeout_seconds` uses `> 0` guard → `_logger.warning` + fall back to default 60, NOT `raise ConfigError` like sibling `[database]` int fields (top_k_retrieve, centroid_recompute_threshold, embedder_cache_size all raise). The spec explicitly mandated warn+fallback (an invalid timeout shouldn't block startup), and precedent exists in config.py (centroid_incremental_enabled deprecation warn, rag_fusion num_queries=1 warn). DA reviewers correctly flagged the divergence then confirmed it spec-justified. Adding a field at line ~98 (before the `Path.home()` callsite) shifted config.py:177→179 in path_home_allowlist.txt; the hash is line-content-based so only the line number changed.
- Action: For a config field whose invalid value should degrade gracefully (timeouts, optional knobs), use warn+fallback not ConfigError, but add a comment/cite the spec since it diverges from the dominant raise pattern. Always bump the path_home_allowlist.txt line number when inserting lines above config.py:177.
- Confidence: high

**2026-06-22 — D7 iterative-review of team plan: architecture/codebase divergences are the deepest bugs**
- Observation: Five fix cycles were needed. Rounds 1-2 fixed plan-internal issues (test gaps, layer violations, missing contracts). Round 3 found Q2 resolution was contradictory and TypeSpec files were untouched despite plan prose claiming "done." Round 4 revealed `create_mcp_http_app()` is never called in production — the "shared KeyStore" wiring story was impossible. Round 5 fixed the design to disk-read-on-demand, eliminating the cross-process staleness problem cleanly. The three deepest bugs all required reading the actual code (grep for `create_mcp_http_app`, check `run_server()` signature, verify `_durable_io.py` mode behaviour).
- Action: For plan iterative-reviews, always have at least one DA agent grep the codebase for every architectural claim the plan makes (function signatures, call sites, mount points). "The plan says X calls Y" is not evidence Y exists or is reachable. TypeSpec files are contract artifacts that must be updated in the same fix pass as the plan prose — add them explicitly to every fix-agent prompt.
- Confidence: high

**2026-06-22 — D7 plan: disk-read-on-demand eliminates cross-process KeyStore staleness**
- Observation: The initial "in-memory cache refreshed on every write" design is safe only within a single process. Archon-search runs HTTP and MCP as separate ASGI apps (potentially separate processes). Switching `active_keys()` to re-read `keys.json` from disk on every call (OS page cache makes this ~<1ms for small files) eliminates cross-process staleness without IPC, inotify, or polling.
- Action: For any multi-process shared state backed by a small JSON file, prefer disk-read-on-demand over in-memory cache with manual invalidation. Reserve in-memory caching for files that are frequently read and rarely change (config) or too large to re-read per-request (embeddings). Keys.json is neither.
- Confidence: high

**2026-06-22 — D6 BE-4 (background validation task in app.py lifespan)**
- Observation: (1) The plan-mandated `except asyncio.CancelledError: log + re-raise` branch in the lifespan wrapper is effectively dead code, because `validate_models_async` itself catches `BaseException` (incl. `CancelledError`) and returns a result rather than propagating. All 3 DA reviewers flagged it. Kept it anyway — the plan spec (BE-4 item c) explicitly requires it; removing it would violate the spec. Do NOT "fix" plan-mandated defense-in-depth. (2) Real bug DA caught: the wrapper's `BaseException` fallback `ModelValidationResult` omitted `validated_at`, so a finished-with-error state reads as "pending" forever to `/status`/`/ready`. Fixed by adding `validated_at=datetime.now(UTC)`. (3) Adding the validation task's `asyncio.to_thread(validate_providers_shared, ...)` broke a sibling test (`test_lifespan_prune_runs_in_thread_not_event_loop`) that asserted `to_thread` is called exactly once for `prune_once` — relaxed to `"prune_once" in call_names`. (4) `app.state.embedder.is_warm` access is allowed in app.py only (the `test_no_app_state_model_access.py` guard exempts app.py).
- Action: For lifespan background-task tasks, mirror the existing backup/maintenance pattern (create_task → _background_tasks.add → add_done_callback(discard)). Always set `validated_at` (or equivalent completion timestamp) on EVERY result path including failure, so downstream "pending vs done" checks work. When adding a startup `asyncio.to_thread` call, grep for sibling tests asserting `to_thread` call counts and relax them. To make a pending-state test deterministic, patch the validator with a coroutine blocking on a `threading.Event` (not `asyncio.Event` — TestClient runs the loop in a worker thread).
- Confidence: high

**2026-06-22 — D6 BE-8 (model_validation sub-object on GET /status)**
- Observation: (1) The route maps the `ModelValidationResult` dataclass field-by-field onto the `ModelValidationStatus` Pydantic model — the correct Clean Architecture adapter pattern; DA reviewers confirmed no shared converter is warranted (use-case layer must not import Pydantic). (2) `getattr(request.app.state, "model_validation", None)` mirrors `_build_maintenance_status`; in unit tests `TestClient(app, ...)` does NOT enter the lifespan, so the attribute is never set unless you set it — but a null-pending test that relies on the attribute being *absent* is a false positive (the `StatusResponse.model_validation` Pydantic default is also `None`, so a route that ignored the field would still pass). Fix: explicitly set `app.state.model_validation = None` to prove the route path runs. (3) Pydantic serialises a UTC `datetime` with a trailing `Z`, not `+00:00`, so asserting `mv["validated_at"] == dt.isoformat()` fails; compare with `datetime.fromisoformat(mv["validated_at"]) == dt` instead (3.12 parses `Z`). (4) model_validation is server-global, NOT namespace-scoped like backup/maintenance — correct, since models are a server-level concern; the helper takes only `request` (no config/ns), a deliberate divergence from sibling `_build_*_status` signatures.
- Action: For dataclass→Pydantic adapter mapping in routes, copy field-by-field with a defensive `list()` on mutable fields. For "null while pending" tests, set the source attribute to `None` explicitly so the test exercises the route, not the schema default. Never assert datetime equality via `.isoformat()` against a Pydantic-serialised value — parse both sides. Doc/OpenAPI updates for new status fields are the close-out (T-2) task's job, not the route task's — keep BE-8-style tasks scoped to code+tests.
- Confidence: high

**2026-06-22 — D6 BE-5 (CheckStatus.PENDING/WARN + ReadinessChecks.models)**
- Observation: (1) Adding `models: CheckStatus = CheckStatus.PENDING` to `ReadinessChecks` changed the `GET /ready` response shape (the field always serialises via its default), which broke a SECOND guard site the plan did not name: `tests/test_routes_ready.py::test_ready_body_schema_is_bounded` asserts `set(body["checks"].keys()) == {"storage"}`. The plan's Q2 claimed `tests/contract/test_readiness_schemas.py` was the "confirmed single guard site" — it was wrong; there are two (the contract test pins the schema, the route test pins the serialised body). All 3 DA reviewers independently flagged it as Critical/in-scope. (2) `schemas.py` docstrings end up verbatim in `openapi_snapshot.json`, so editing a docstring forces a Python-3.12 snapshot regen even when no field changed. (3) A docstring `See ``D6-readiness-extension.tsp``` is a broken reference from `archon_search/server/` (the .tsp lives in `Documentation/Backlog/`); forward-refs to a not-yet-written `BREAKING.md` entry are also misleading — reference the stable team-plan contract instead.
- Action: When adding a defaulted field to a response model, grep ALL tests asserting that endpoint's body shape (`set(body[...].keys())`, snapshot strings), not just the schema's own contract test — defaulted fields always serialise. Distrust a plan's "single guard site" claim; verify with grep. After any `schemas.py` docstring edit, regenerate `openapi_snapshot.json` with `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot`. In schema docstrings, cite the team-plan contract (C-id + path), not bare .tsp filenames or unwritten BREAKING.md entries.
- Confidence: high

**2026-06-22 — D6 BE-7 (validate_providers delegates to validate_providers_shared)**
- Observation: (1) Refactoring `validate_providers` body changed its test seams: the ~10 existing `TestValidateProviders` body tests patched `sys.modules["fastembed"]`/`["onnxruntime"]`, which no longer work because the body now calls `validate_providers_shared` whose seams are `archon_search.model_validation.{TextEmbedding,_load_cross_encoder,_available_providers}`. All had to be rewritten to patch those. Two more body tests outside the class (`..._when_get_available_providers_raises`, `..._logs_warning_on_missing_provider`) also patched `sys.modules` and broke. (2) `validate_providers_shared` passes `providers or None`, so the empty-list test must assert `providers=None`, not `providers=[]`. (3) DA reviewers correctly flagged that the plan-named test `test_validate_providers_returns_bool_never_raises` was a false-positive: it patched the shared fn to RETURN `(False,True,...)` not RAISE — and the method had no try/except, so the "never raises" docstring was unenforced. Fix: wrap the whole body (lazy import + call) in `try/except Exception → return False` and change the test to `side_effect=RuntimeError`. (4) Behavior change is intended: the reranker is now probed at install.py CoreML check (previously embedder-only), so a reranker-incompatible-but-embedder-OK host falls back to CPU entirely — by design (preserved `-> bool` signature; granular fallback is FE-1). (5) Shortening `validate_providers` shifted two `Path.home()` callsites up (1359→1355, 1548→1544); hashes unchanged, only line numbers — update `tests/path_home_allowlist.txt`.
- Action: When refactoring a method body that delegates to a new module, grep its test file for `sys.modules` / leaf-import patches and repoint them to the new module's seams. For "never raises" methods, wrap the body in `try/except Exception` (not BaseException) and test with `side_effect`, never a benign return. After ANY edit that changes line counts in install.py, run `tests/test_no_hardcoded_path_home.py` and update drifted line numbers in the allowlist (hash is content-based, so only the line number changes).
- Confidence: high

**2026-06-22 — D6 FE-2 (CLI maintenance status renders model_validation)**
- Observation: (1) `model_validation` is a TOP-LEVEL key on the `GET /status` payload (sibling of `maintenance`), not nested under the `maintenance` object — extract it via `server_payload.get("model_validation")`, not `maintenance_obj.get(...)`. It is server-global and must NOT be merged from the offline `.maintenance-state.json` (unlike `collection_health`). (2) Render the block BEFORE the `collection_health` early-return in `_print_status_text`, or it disappears whenever no collection has maintenance history. (3) The implementation was correct on first pass; all three DA agents raised only Moderate test-coverage gaps (null probes → `_fmt_ok(None)`="pending", `validated_at=None` fallback, explicit `model_validation=null` vs absent-key). The fix was tests-only — added 3 tests, no code change. The real server emits `"model_validation": null` while pending, NOT an absent key, so a test asserting absent-key alone does not exercise the `isinstance(mv, dict)` guard against an explicit null. (4) `_status_server_payload` test helper conditionally injects the key only when the param is non-None — to test the explicit-null path, set `payload["model_validation"] = None` directly after calling the helper.
- Action: For any new sub-object surfaced from `GET /status` into the CLI: confirm whether it is top-level or nested under an existing object before writing the `.get()` path. Treat server-global fields as live-only (never offline-merged). When rendering before an early-return block, place it above the return. For nullable-field CLI rendering, always add a test for the explicit-`null` server shape (distinct from absent-key) and a test for the all-pending (`None` probes) state — the spec's named test list is a minimum.
- Confidence: high

**2026-06-22 — D6 BE-6 (routes_ready.py populates checks.models)**
- Observation: The spec's FAIL>WARN>OK mapping must be coded with POSITIVE branch conditions, not implicit fall-through. The naive version (`if either is False → FAIL; if warnings → WARN; else OK`) silently maps a non-None `ModelValidationResult` with `None` probe flags to OK/WARN. All 3 DA reviewers independently converged on this as the single Major/Moderate root cause. In practice it is unreachable — every return path of `validate_models_async` sets concrete bools (success/timeout/BaseException all set both flags) — but the dataclass type is `bool | None = None`, so the consumer must honour the full type contract. Fix: `None result → PENDING`, `either is False → FAIL`, `both is True → WARN if warnings else OK`, `else (any None flag) → PENDING`. The `_set_validation(**kwargs)` test helper that only forwards provided kwargs is the right pattern (leaves unmentioned fields at dataclass defaults). The existing `test_ready_body_schema_is_bounded` (BE-5) already expects `{"storage","models"}` so no sibling test broke.
- Action: For any enum-status mapping from a `T | None`-typed source, make every branch a positive `is True`/`is False` check and route the residual to the safe/pending state — never let a partially-populated object fall through to a success status. Add a defensive test (`*_when_probe_flag_unset`) even when the bad state is unreachable from the current producer; reviewers will flag its absence otherwise.
- Confidence: high

**2026-06-22 — D6 T-1 (#manual_test task close-out via implement-next)**
- Observation: T-1's three sub-items are all `#manual_test` (wizard reranker fallback on a CoreML-absent host, `maintenance status` against a live server, sub-second `/ready` PENDING→OK window) — none physically runnable in the CI/dev sandbox. The TDD cycle is correctly skipped (no code output). The honest checkoff is: run the already-existing automated tests that cover the same behaviours (S11: `test_install_run.py` wizard tests; S13: `test_cli_maintenance.py`; PENDING/OK: `test_routes_ready.py` + the e2e), confirm green, then check the box WITH a Verification note mapping each manual scenario to its automated equivalent and stating physical manual execution remains a pre-release operator step. The process DA agent flagged a bare checkoff as Major (misrepresents state to T-2's fact-check) — the annotation resolves it. A second DA agent caught a pre-existing weakness in `test_ready_models_transitions_from_pending_to_ok`: it defaulted its local `models` to `"pending"`, so it could pass without the server ever returning `"pending"`; fix is default `None` + an honest docstring (the pending state is proven by the unit test, not the e2e timing window). Also found+fixed a stale `[ ]` on the S11 "Done when" line despite FE-1 being done.
- Action: For `#manual_test` tasks that cannot run in the sandbox: skip TDD, run the automated equivalents as proof, and check the box WITH an inline Verification note (date + scenario→test mapping + "manual execution pending on real host"). Never bare-check a manual task — it misrepresents state to the close-out fact-check. When relying on an e2e "transition" test as proof, read its body: a local var defaulted to the start-state value silently masks whether the transition was observed; default it to `None` instead. Only commit the task's own files (D6 plan + the one test edit) — leave unrelated pre-existing working-tree changes (D7 docs, prior learnings edits) unstaged.
- Confidence: high

**2026-06-22 — D6 T-2 (project close-out & acceptance fact-check via implement-next)**
- Observation: (1) T-2 is doc-only + verification; TDD correctly skipped. The plan's "Documentation update" list named 7 files, but the actual `/ready` contract that D6 changed was also embedded inline in FOUR docs NOT on the list: `Architecture/140` (error table), `150` (threat-model example with a "and nothing more" claim), `160` (HTTP-endpoints table + a now-FALSE "/ready does not check model availability" claim + runbook 503 body), and `UserManual/08` (Docker section). The iterative-review DA agents found these across two cycles (140/160 in C1, 150/08 in C2). Lesson: when a feature extends a widely-referenced API response shape, grep ALL of `Documentation/` for the old shape (`grep -rn '"storage": "ok"' Documentation/`) — the plan's doc list is a floor, not a ceiling. (2) `Documentation/Completed/` archives (B2, C9 briefs/plans) also reference the old `/ready` shape but are FROZEN point-in-time records — do NOT edit them (project convention). (3) All 12 acceptance criteria were verified already-implemented by prior tasks; T-2 found zero code gaps — the only work was documentation. (4) The 54 pytest warnings are all third-party (docling DeprecationWarning, asyncio/pytest ResourceWarning) — none from D6 code; `uv run python -W error -c "import <d6 modules>"` is the clean way to prove the touched code is warning-free without chasing `.venv` deprecations. (5) Committed exactly the 11 T-2 files; left pre-existing unrelated working-tree changes (D7 brief+plan+tsp, learnings.md) unstaged via explicit `git add <paths>`, never `git add -A`.
- Action: For close-out/documentation tasks: after updating the plan-named docs, grep the WHOLE `Documentation/` tree for literal old API shapes and stale behavioral claims the feature invalidated; fix live docs, skip `Completed/` archives. To prove "no build warnings" for a doc task that touched no code, run `python -W error -c "import <the feature's modules>"` rather than trying to zero out the full-suite warning count (which is dominated by third-party deprecations). Stage close-out files explicitly by path.
- Confidence: high

**2026-06-24 — D9 plan-maker: investigation agents went idle but re-engaged via SendMessage**
- Observation: Six parallel investigation agents (architecture, contracts, scenarios, backend, frontend, tester) sent idle_notifications without routing findings first. Sending `SendMessage` to each agent prompted them to deliver their findings. This is different from the D6/D7 pattern where agents idled permanently and the fallback was inline investigation.
- Action: When investigation agents idle before sending findings, try SendMessage once. If no response follows, fall back to inline investigation immediately. Do not send multiple retries.
- Confidence: high

**2026-06-24 — D9 plan-maker: "ADR spike required before implementation" is a legitimate plan status**
- Observation: The MCP wiring brief explicitly requires three open questions answered via ADR spike (mount vs separate-port; namespace propagation mechanism; lifespan object availability) before any implementation begins. The correct plan structure is: (1) Kickoff task K-1 = the ADR spike itself; (2) all implementation tasks `needs: K-1`; (3) plan `status: draft` until K-1 resolves Q1/Q2/Q3. This is a different pattern from the usual "contracts + scenarios then implement" — the implementation cannot even be estimated until the spike proves the mechanisms.
- Action: For any feature where a critical unknown must be proven by experimentation before a plan can be finalized, put the spike in Phase 0 (Kickoff), mark the plan as `draft`, and state the open questions explicitly. Do not attempt to estimate tasks that depend on the spike's outcome — the plan is a template until K-1 completes.
- Confidence: high

**2026-06-24 — D9 plan-maker: `ctx.meta.get("namespace")` in mcp.py:1022 is dead code — no existing working pattern**
- Observation: `update_collection` at `mcp.py:1022` reads `ns = ctx.meta.get("namespace", DEFAULT_NAMESPACE)`. All other tools hardcode `DEFAULT_NAMESPACE`. Nothing in the codebase populates `ctx.meta["namespace"]` — not `APIKeyMiddleware`, not any lifespan hook. This is confirmed dead code, not a working pattern to copy. The namespace propagation mechanism for FastMCP tool closures is an open question requiring an ADR spike.
- Action: Do not reference `update_collection`'s `ctx.meta.get("namespace")` as a "working pattern" to replicate. It is dead code. The ADR spike must prove how to bridge `request.state.namespace` (set by `APIKeyMiddleware`) into FastMCP tool closure context.
- Confidence: high

**2026-06-24 — D9 iterative review of team plan: TypeSpec contract drift, TestClient feasibility gap, and BE-5 scope underestimation**
- Observation: (1) `archon-mcp-config.tsp` had `port` and `host` fields that contradicted the plan's single-port mount architecture — `tsp compile --no-emit` passes syntax only, not plan alignment; the contract had drifted without any build signal. (2) BE-3/5/6/7 integration tests all assumed TestClient can complete a multi-step JSON-RPC `initialize` → `tools/list` → `tools/call` sequence via FastMCP's streamable-HTTP transport — this was never proven and is a load-bearing assumption for ~60% of the integration tests. (3) BE-5 listed 3 unit tests but had ~25 pipeline call sites across 17 closures + the `_resolve_embedder` helper — the scope was underestimated by roughly 3-4×. (4) The rotate_key hot-reload limitation was documented as "does not update until restart" but the actual failure mode is worse: the MCP `APIKeyMiddleware.self._api_key` (the legacy path) never updates because `request.app.state` on the Starlette sub-app does not carry the REST app's `api_key`, so the pre-rotation key works forever on MCP (not just during grace). (5) T-3 namespace isolation test was one-directional: ingest in ns-a, search with ns-a token (found), search with default token (not found) — passes vacuously if ns-b is empty; correct test needs docs in BOTH namespaces and tests BOTH tokens in BOTH directions.
- Action: For plan reviews of FastMCP/ASGI wiring features: (a) always read the TypeSpec contract files, not just the plan prose — contracts drift silently; (b) have the testing DA agent explicitly assess whether TestClient supports the full transport protocol (not just the first request); (c) when a task says "fix all N closures," enumerate the actual call sites with grep before setting the estimate; (d) for rotate/revoke limitations in multi-middleware setups, trace through each middleware's `request.app.state` to determine which app instance is in scope — the sub-app's state is separate from the parent app's state; (e) for namespace isolation e2e tests, always use the bidirectional proof (docs in BOTH namespaces, BOTH tokens tested against BOTH docs).
- Confidence: high

**2026-06-22 — D7 K1 kickoff/contract-ratification via implement-next**
- Observation: K1 is a #team alignment task with empty Tests block — TDD correctly skipped. The iterative-review DA agents (2 cycles, 3 agents each) found two classes of real defects: (1) TypeSpec seam signature errors (`load(): void` should be `KeyRecord[]`; `create()` should return `{id, token}` not bare `string`; `rotate_default_key()` should return `{new_key_id, new_token, old_record?}`) — these would have caused BE-1 and BE-7 implementers to produce incompatible return types. (2) Plan-prose/TypeSpec divergence — after fixing the .tsp files in Cycle 1, the plan prose still described the OLD signatures; Cycle 2 DA agents caught that the BE-1 task description explicitly instructed implementing the bare-string return that was already corrected. Both defects were in the documentation layer only (no code). The .tsp files are the authoritative contract; plan prose is secondary.
- Action: For K1-style contract-ratification tasks: (a) always have DA agents read BOTH the .tsp files AND the plan prose sections that describe those same signatures — plan prose diverges from .tsp files silently; (b) after fixing .tsp files, immediately scan the plan for any task description that quotes the OLD signature (especially "TypeSpec update: update line N to ...") and update those instructions; (c) `tsp compile --no-emit` proves syntax only, not plan alignment — the second review cycle is essential for catching the prose/contract delta.
- Confidence: high

**2026-06-24 — D9 BE-2 implement-next: switching to `http_app()` exposed worker-wide FastMCP test pollution**
- Observation: BE-2 changed `mcp.py:1541` from `streamable_http_app()` to `http_app(path="/")` (per ADR 09 / FastMCP 3.4.2). This caused 10 full-suite failures (`AttributeError: 'FastMCP' object has no attribute 'http_app'`) that did NOT reproduce when the MCP test files ran in isolation — only under `-n auto --dist=loadgroup`. Root cause: ~9 MCP test modules install the LOW-LEVEL `mcp.server.fastmcp` package into `sys.modules["fastmcp"]` at module-collection time via `if "fastmcp" not in sys.modules: import mcp.server.fastmcp as _real_fastmcp; sys.modules["fastmcp"] = _real_fastmcp`. The low-level `FastMCP` has `streamable_http_app()` but NOT `http_app()`. Whichever such module is collected first on an xdist worker poisons `sys.modules["fastmcp"]` for the whole worker, breaking every real-app builder (including modules like `test_keystore_be9.py` that themselves capture `_real_fastmcp_class` at import — they capture the already-poisoned low-level class). The fix: point every such guard at `import fastmcp` (the real package, a declared dependency that has both `http_app` and `Context`).
- Action: (a) When changing a public API call in `mcp.py` that the test stubs mirror, grep `tests/` for `streamable_http_app`/`http_app`/`mcp.server.fastmcp` and `sys.modules["fastmcp"]` BEFORE running the suite — the stub corpus must stay API-compatible with the real code. (b) `mcp.server.fastmcp` (low-level, bundled with the `mcp` package) and `fastmcp` (the standalone 3.4.x package) are DIFFERENT classes; only the standalone one has `http_app()`. Always alias `import fastmcp`, never `import mcp.server.fastmcp`, when populating `sys.modules["fastmcp"]`. (c) Module-level `sys.modules` mutation at collection time + xdist loadgroup = worker-wide pollution that is invisible in isolated runs; always verify with the FULL `uv run pytest` (no `-n0`), never just the touched test file. (d) FastMCP 3.4.2 `list_tools()` returns `FunctionTool` objects without `.inputSchema` — use `t.to_mcp_tool().inputSchema` to read the published MCP input schema.
- Confidence: high

**2026-06-24 — D9 BE-2: AsyncExitStack-wrapped lifespan must put REST shutdown in try/finally**
- Observation: The MCP mount lives inside `async with AsyncExitStack() as _mcp_stack:` wrapping the `yield` in `create_app()`'s lifespan. The original draft placed the REST shutdown block (search_store.disconnect, telemetry drain, background-task cancel) AFTER/OUTSIDE that `async with`. If the MCP lifespan teardown (`StreamableHTTPSessionManager` task-group shutdown) raised during `__aexit__`, REST cleanup would never run — leaking the store connection, telemetry writer, and background tasks. iterative-review (devils-advocate + brooks) flagged this as Critical.
- Action: When a sub-app lifespan is delegated via `AsyncExitStack`/`router.lifespan_context()` and wraps the `yield`, always wrap `async with <stack>: ... yield` in a `try:` and move the parent app's own shutdown cleanup into a `finally:` so it runs regardless of sub-app teardown failure. The "X must never block startup" requirement implies "X teardown must never block shutdown cleanup" too.
- Confidence: high

**2026-06-24 — D9 T-1: MCP envelope-level `isError` is NOT the right check for graceful tool errors**
- Observation: FastMCP only sets `isError=True` at the JSON-RPC envelope level for unhandled exceptions (via `_make_error_result()`). Graceful `McpErrorResponse` returns flow through `convert_result → ToolResult(is_error=False) → CallToolResult(isError=False)` — `isError` is `False` even when the tool returned `{"error": "not_found", "code": "not_found"}`. A check like `assert not rpc_result.get("isError")` correctly gates on crashes without rejecting graceful errors.
- Action: In MCP smoke tests, check `isError` at the envelope level (which guards against unhandled exceptions/attribute errors), then separately check the text content for crash strings (`AttributeError`, `NoneType`). Do not confuse `isError=True` (always a crash) with `{"error": ..., "code": ...}` in the text content (which is a graceful error, normal for not_found/conflict).
- Confidence: high

**2026-06-24 — D9 T-1: `McpErrorResponse` TypedDict has only `error` and `code` fields — error field may be None**
- Observation: `McpErrorResponse` (in `mcp.py`) has two fields: `error: str | None` and `code: str`. Checking `"AttributeError" not in parsed.get("error", "")` raises `TypeError: argument of type 'NoneType' is not iterable` when `error=None` (which is valid per the type). The correct check is `str(parsed.get("error") or "")` to coerce None to empty string before the `in` test.
- Action: Any assertion that does a substring check on a field that can be None must use `str(value or "")` before the `in` operator, never `value` directly or `value or ""` when the result must be `str`.
- Confidence: high

**2026-06-24 — D9 T-1: `job_to_dict()` returns `job_id` (not `id`) as the job identifier field**
- Observation: `job_to_dict()` in `jobs/model.py` returns `{"job_id": ..., ...}` — not `{"id": ..., ...}`. MCP tools that enqueue jobs and return the job dict (e.g. `ingest_file`, `ingest_directory`) surface this field as `job_id`. Tests that look for `"id"` in the parsed response will silently pass (Python `in` on a dict checks keys, but `"id" in {"job_id": ...}` is False) or fail if asserted.
- Action: When asserting on job response fields from MCP ingest tools, always assert `"job_id" in parsed`, never `"id" in parsed`. Verify by reading `jobs/model.py:job_to_dict()` directly, not by guessing from REST conventions.
- Confidence: high

**2026-06-24 — D9 T-1: `list_keys` MCP tool returns `{"keys": [...], "hidden_revoked_count": N}`, not a bare list**
- Observation: `list_keys` does not return a JSON array at the top level — it returns `{"keys": [...], "hidden_revoked_count": int}`. Asserting `isinstance(parsed, list)` fails. The smoke test must check `"keys" in parsed` and `isinstance(parsed["keys"], list)`.
- Action: When writing smoke tests for list-style MCP tools, always inspect the actual tool return type in `mcp.py` rather than assuming a bare list. Structured response wrappers (with pagination or hidden-count metadata) are common.
- Confidence: high

**2026-06-24 — D9 T-1: setup calls in destructive smoke tests must use `_assert_tool_response_valid` for clear failure attribution**
- Observation: Raw envelope extraction (`result["result"]["content"][0]["text"]`) in setup steps of destructive tests (delete_document, revoke_key) bypassed `_assert_tool_response_valid`. If the setup step failed, the test would fail with an opaque `KeyError` or `IndexError`, not a descriptive assertion message. The fix: route all MCP calls (including setup steps) through `_assert_tool_response_valid` so failure attribution is always clear.
- Action: In MCP smoke tests, route every `_mcp_call_tool()` result through `_assert_tool_response_valid()` — including setup/teardown calls, not just the primary assertion. This ensures failures at any step produce descriptive messages.
- Confidence: high

**2026-06-24 — D9 docs: Architecture doc updates for MCP HTTP mount**
- Observation: Docs 100/110 carried stale "create_mcp_http_app is defined but not invoked / not wired into the shipped runtime / known gap / #Unverified" language across three places in 100 (C4 L2 prose, Single auth boundary, Runtime Topology) and one wiring note in 110. D9 inverted all of that: the MCP app is now mounted at `/mcp` on the same port (8765) inside create_app's lifespan via app.mount, gated on config.mcp.enabled. Config dataclasses (AuthConfig, MaintenanceConfig, HyDEConfig, McpConfig) all live in the single config.py "Cross-cutting" row in 110, not in a separate Entities section — match that convention when adding a new config dataclass.
- Action: When a feature flips a "not wired / known gap" caveat to "shipped", grep both 100 and 110 for the old symbol (create_mcp_http_app) and every "#Unverified"/"known gap" phrase near it — the stale claim is usually repeated in 3+ prose spots, not just one. Bump "Last reviewed" to the task date on every doc touched.
- Confidence: high

**2026-06-24 — D9 docs: doc 600 guiding principles carried stale MCP-auth claims**
- Observation: `600_api_reference_or_public_interface.md` guiding principles #2 and #5 asserted (pre-D9) that the MCP transport uses an empty `namespaces={}` dict and that MCP tools "do not apply namespace gating" (marked `#Unverified`). D9 falsified both: `create_mcp_http_app` now passes `namespaces=config.namespaces` (mcp.py ~L1610), and tool closures resolve `request.state.namespace` per-request, passing `namespace=` into every pipeline call. The `mcp` field doc already existed on the `/health` row but the `/status` section and the `[mcp]` config section were missing.
- Action: When a feature changes auth/namespace wiring, audit doc 600's "Guiding principles" block specifically — its numbered claims are easy to miss because they sit above the per-route tables and often carry `#Unverified` tags that should be resolved (not left) once the code is shipped. Verify the namespaces wiring by reading the `add_middleware(APIKeyMiddleware, ...)` call in mcp.py, not REST conventions.
- Confidence: high

**2026-06-24 — D9 roadmap: recording a shipped feature across the three roadmap files**
- Observation: D9/mcp-wiring existed in NO roadmap (not even as a planned item) before this task — it was a Backlog brief+plan that shipped and moved to Completed/. The three roadmaps have distinct conventions: `roadmap.md` (in Documentation/) uses `✓ **<ID> shipped**: ...` Status-Snapshot bullets with `Completed/...` links; `Backlog/03_world_class_roadmap.md` uses `- [x] **D#. ...** — ... [[brief](../Completed/...), [plan](../Completed/...)]` checkboxes PLUS a redundant numbered single-list view PLUS a mermaid effort/impact matrix — all three must be updated; `Architecture/530_..._roadmap.md` is a debt register, NOT a shipped-feature log, so D9 belongs only as context on the existing MCP-surface debt entry (API-3), not as a new row.
- Action: When marking a feature shipped in 03_world_class_roadmap.md, update ALL THREE views (checkbox list, numbered single-list, mermaid matrix). The numbered single-list is sequential across the whole doc — inserting an item in Phase D forces renumbering every Phase E/F line below it. Relative-link depth differs per file: Documentation/ files use `Completed/...`, Documentation/Backlog/ and Documentation/Architecture/ files use `../Completed/...`. Always `test -f` each resolved path.
- Confidence: high

**2026-06-24 — D9 roadmap: moved files leave stale Backlog/ links in cross-referencing docs**
- Observation: When brief/plan/.tsp/api-contracts moved Backlog/ → Completed/, the only live stale navigational link was in `ADRs/09_..._propagation.md` References section (`Documentation/Backlog/mcp-wiring-team-plan.md`). The two `Backlog/mcp-wiring-*` strings inside `Completed/mcp-wiring-team-plan.md` itself are its own "Documentation update" checklist entries (historical record of edits made when the files lived in Backlog/) — NOT cross-links; leave them intact. Distinguish a navigational link (fix it) from a historical checklist/audit-trail reference (leave it).
- Action: After a Backlog→Completed move, `grep -rn "Backlog/<basename>"` the whole Documentation/ tree, then for each hit decide: live cross-reference (fix to Completed/) vs. the moved file's own historical self-reference (leave). Pre-existing unrelated staleness found nearby (e.g. DOC-1 in 530 still says "13 tool names" when it's 17) is out of scope — flag, don't fix.
- Confidence: high

**2026-06-24 — Iterative review of D8 plan: plans drift from source on line numbers, file paths, and "already does" claims**
- Observation: The D8 team plan carried three classes of factual error that all four reviewers (3 DA + Brooks-Lint) independently flagged: (1) hardcoded call-site line numbers (`mcp.py:361,495`) that had drifted post-D9 to 407/546; (2) a false premise that `cli/status.py` "already calls GET /status like maintenance status does" — it only calls `_get_service().status()` (OS service state), no HTTP; (3) a doc-checklist path `tests/contract/openapi_snapshot.json` when the live snapshot test (`tests/server/test_openapi_snapshot.py`) actually regenerates `tests/server/openapi_snapshot.json` (both files exist on disk; only the server/ one is under test). The brief also self-contradicted (32 vs 64 hex; listed `from_search_multi_result` as populating `result_doc_ids` when it does not).
- Action: When reviewing a plan, verify EVERY concrete code reference against source before trusting it: line numbers (grep the symbol, don't trust the number), file paths (`find`/grep for which path a test actually uses when duplicates exist), and "X already does Y" claims (read X). Anchor plan references by symbol + endpoint, never bare line numbers — they rot the instant any earlier edit lands. Brief↔plan contradictions are common when a brief predates a resolved decision; reconcile or mark superseded.
- Confidence: high

**2026-06-25 — D8 K1: `app.state` is NOT shared with mounted Starlette sub-apps — always pass dependencies as explicit parameters**
- Observation: The D8 C1 contract initially said MCP adapters would "construct a closure `lambda id_: hash_doc_id(app.state.salt_bytes, id_)` per request." Two independent DA reviewers and one architecture review caught that MCP tool closures in `mcp.py` do NOT access `app.state` — they capture parameters passed to `create_mcp_http_app()`. The MCP Starlette sub-app has its own state namespace (confirmed by existing learnings entry for D9 BE-7). The correct pattern: build the closure ONCE in the lifespan, store it on `app.state.doc_id_hasher` for REST routes (which do read parent `app.state`), and pass it as an explicit parameter `create_mcp_http_app(doc_id_hasher=...)` for MCP. This mirrors the existing `writer` + `key_store` dependency injection pattern precisely.
- Action: For any new server-side state (flag, closure, loaded value) that both REST routes AND MCP tools need: (1) build/load it in the lifespan; (2) store on `app.state.<name>` for REST route handlers; (3) pass as an explicit keyword param to `create_mcp_http_app(...)` for MCP. Never expect `request.app.state` inside MCP tool closures to reference the parent FastAPI app's state.
- Confidence: high

**2026-06-25 — D8 T-2: e2e tests for status observability — place all assertions inside the `with make_real_app()` block**
- Observation: The initial S10/S11 tests placed all assertions OUTSIDE the `with make_real_app(...)` context. `resp` is a buffered object so the assertions technically work, but it is structurally wrong — if an assertion fails, the cleanup exception is masked. DA reviewers flagged this as Critical. The correct pattern (confirmed by the schema_status_e2e tests) is to place all assertions inside the `with` block while the app is still alive.
- Action: For any integration test using `make_real_app`, place all HTTP request calls AND their assertions inside the `with` block. Move only captured results (e.g. `result = runner.invoke(...)`) outside the block when the action must happen after app teardown — but in practice, CLI invocations via CliRunner can also run inside the block.
- Confidence: high

**2026-06-25 — D8 T-2: S12b (server unreachable) must be a separate test from S12a (server reachable)**
- Observation: The plan text explicitly says "S12 covers both: (a) server reachable → output displays hash_doc_ids_enabled; (b) server unreachable → service state shown, telemetry section omitted." The initial implementation only covered S12a. DA reviewer C1-III-1 flagged S12b as missing. S12b needs no `make_real_app` — it only patches `_fetch_server_status` to return `None` and `_get_service` to return a mock `ServiceStatus`. Because S12b patches nothing that requires real infrastructure, remove unused `tmp_path` and `monkeypatch` fixture params from its signature (C2-I-1).
- Action: When the plan lists sub-cases for a scenario (S12a/S12b), implement each as a separate test. Sub-case tests that only patch both service layers need no `make_real_app` and no pytest fixtures.
- Confidence: high

**2026-06-24 — Iterative review fix-propagation: editing one mention of a path/layer leaves stale duplicates elsewhere**
- Observation: After fixing the OpenAPI snapshot path in the doc-checklist, the SAME wrong path survived in the T-4 close-out duties block 190 lines away (Cycle-2 Major). After reassigning a module's CA layer in the approach prose + mermaid + layer-map table, the "Tasks by layer" summary and the per-task header still carried the old "Use Cases" label (Cycle-2 Moderate). Merging a layer line also accidentally created a duplicate "Frameworks & Drivers:" bullet that had to be re-merged.
- Action: After any plan edit that changes a path, a layer label, a file name, or an estimate, grep the WHOLE document for every other occurrence of the old value — these docs repeat the same fact in 3-5 places (prose, diagram, table, task header, task-by-layer summary, close-out duties). A single Edit is almost never sufficient. Re-read the immediate neighbourhood after a list-merge to catch duplicate bullets.
- Confidence: high

**2026-06-25 — implement-all subagents: iterative-review completion ≠ task completion — commit step is mandatory**
- Observation: Two consecutive T-1 subagents ran /iterative-review, received "no issues remain", and terminated their turns without committing or flipping the plan checkbox. The agents treated review convergence as task completion. The commit step was never reached.
- Action: In /implement-next, after /iterative-review returns clean, the agent MUST proceed to: (1) run tests, (2) flip the plan checkbox, (3) `git commit` with all changed files, (4) emit Step 7 report. Review convergence is a green light to proceed — it is not a terminal state. If spawning parallel review agents, block until ALL complete before continuing to Step 4.
- Confidence: high

**2026-06-25 — MIS T-2: #manual_test tasks in headless CI require static verification — check off after max achievable verification**
- Observation: T-2 was a `#manual_test` requiring live instances (native service + Docker). In a headless agent environment, live execution is impossible. The agent performed the highest achievable substitute: static accuracy review of the manual against source files, finding and fixing two factual inaccuracies in Part 2 Step 1 (`.env.example` state was described backwards). DA reviewers (C1-T-1, C1-T-2) flagged that static review is categorically not the same as following the manual, and cannot verify S1/S2/S7/S8/S9 (scenarios requiring live instances). However, the plan has no path forward if T-2 permanently blocks on "needs live instances" — the task was checked off with the understanding that T-1's e2e tests cover the Docker side and the native service scenarios have no automated equivalent.
- Action: For `#manual_test` tasks in a plan being executed by an AI agent, perform static accuracy review as the best available substitute. Fix any factual inaccuracies found. Note in the learnings that the live test portion was not executed. Do NOT conflate doc-only tasks (which can skip TDD) with manual tests (which require live verification). If the live test is genuinely impossible in the agent's environment, check off and document the gap.
- Confidence: medium

**2026-06-25 — MIS T-2: .env.example state drift between BE-1 (doc writing) and BE-2 (file update)**
- Observation: BE-1 wrote the manual assuming `.env.example` had `ARCHON_SEARCH_API_KEY=your-key-here` uncommented and `ARCHON_SEARCH_IMAGE` commented out. BE-2 updated `.env.example` so the key line is commented and the image line is active. The manual's Step 1 instructions became backward — telling users to "uncomment" an already-active line and "comment out" an already-commented line. T-2's static review caught this drift.
- Action: When a doc task (BE-1) references a file that a sibling task (BE-2) will update, the doc task should either (a) write instructions for the post-BE-2 state if BE-2 is already committed, or (b) mark the doc section as "TBD: verify against BE-2 result". The dependency order (K1 → BE-1 AND BE-2 → T-1 AND T-2) means BE-1 and BE-2 can commit in either order, creating drift risk. Always re-verify cross-file references at T-2 time.
- Confidence: high

**2026-06-26 — Roadmap link audit: validate all markdown links against disk before committing**
- Observation: The roadmap had 0 clickable markdown links; all cross-references were backtick-quoted paths (`\`Documentation/Completed/X.md\``). Validating with `grep -oP '\[...\]\(([^)]+)\)'` + `while read f; do [ -f "$f" ] || echo MISSING; done` caught zero broken links after conversion. The D3 entry pointed to `Documentation/Backlog/` (stale after D3 shipped to `Completed/`) — found and fixed during the link extraction step.
- Action: For any roadmap or index update that adds markdown links: (1) run the shell link validator immediately after editing, before committing; (2) when converting `\`Documentation/Backlog/X.md\`` references, check whether the file has since moved to `Completed/` — the Backlog path can be stale for shipped features.
- Confidence: high

**2026-06-26 — E0 audit: parallel static analysis agents are effective for codebase-wide limitation discovery**
- Observation: Spawning three independent Explore agents in parallel (size/count limits, timeouts/rate-limits, string/feature restrictions) produced a comprehensive limitation catalogue with no overlap in findings and no missed items. Cross-referencing `Documentation/archon-search-notes.md` confirmed the 1 MB PDF cap is documented there as a known issue but has no named constant in code — it is an emergent behaviour.
- Action: For "find all X in the codebase" tasks, always fan out agents by category rather than running a single broad search; the parallel approach saturates coverage faster and avoids each agent getting lost in tangential findings.
- Confidence: high

**2026-06-26 — E0 audit: `markitdown` is installed by wizard but absent from `pyproject.toml`**
- Observation: `archon_search/parser.py` lists `.docx`/`.pptx`/`.xlsx` as supported and uses a lazy `from markitdown import MarkItDown` import. `markitdown` is installed by the wizard at `install.py:1296` but is not in `pyproject.toml` dependencies or optional-dependencies. `uv sync --dev` therefore skips it, so dev-install office parsing raises a generic `ParseError` wrapping `ModuleNotFoundError`.
- Action: When adding a lazy-import dependency to a parser or optional feature, always add it to `pyproject.toml` (core or optional extra) in the same PR. Check with `uv run python -c "import <pkg>"` in the project directory before declaring a dependency resolved.
- Confidence: high

**2026-06-25 — MIS multi-instance plan fixes: stale "MCP not mounted" premise after D9 shipped**
- Observation: The MIS team plan was written assuming `create_mcp_http_app` had no callers and `/mcp` was not mounted. D9 shipped between drafting and review — `app.py:368` now does `app.mount("/mcp", mcp_starlette)` inside the lifespan when `mcp.enabled=true` (default). The plan referenced a nonexistent `Documentation/UserManual/05_mcp_integration.md`; the actual MCP wiring doc is `Documentation/ADRs/09_mcp_http_mount_and_namespace_propagation.md`. Also: the plan cited bare line numbers (`config.py:92`, `paths.py:43–87`, `docker-compose.yml:14–16/18–20/31–39`, "line 41") that should be symbol references; claimed Docker Compose "fails silently" on port conflict (it logs to stderr + non-zero exit); and instructed `cat ~/.archon-search/.search.env` to get the key when the file is env-format `ARCHON_SEARCH_API_KEY=<token>` (needs `grep -o '[^=]*$'` to strip the prefix).
- Action: When a doc-only plan describes runtime behaviour, verify each claim against current source — feature flags ship between draft and review (check `app.py` mounts, `config.py` defaults). Replace bare line-number citations with symbol names (`config.SearchConfig.port`, `paths.get_data_dir()`, `docker-compose.yml archon-dev ports`). Verify referenced doc filenames exist (`ls Documentation/UserManual/ Documentation/ADRs/`). The `.search.env` key file is env-format, not a bare token.
- Confidence: high

**2026-06-26 — E0b BE-11: `patch.object(Path, "home", return_value=tmp_path)` is the only safe intercept for platform code that calls `Path.home()` directly**
- Observation: Tests that patched only `_plist_path` (via `patch.object(type(svc), "_plist_path", ...)`) failed when `register()` gained new calls like `data_dir = Path.home() / ".archon-search"` and `unregister()` gained `wrapper = Path.home() / ...`. Any new `Path.home()` call that runs before the patched path property still reaches real `$HOME`, causing test leakage and false-negative wrapper deletion tests. The fix for all macos tests was `patch.object(Path, "home", return_value=tmp_path)` as the outermost context manager — this makes all `Path.home()` calls in the test return `tmp_path` regardless of where they appear in the implementation.
- Action: For any platform service test that exercises code calling `Path.home()` more than once, always use `patch.object(Path, "home", return_value=tmp_path)` as the outermost patch. Do not patch individual paths — they drift when the implementation grows. After adding new `Path.home()` calls to an implementation, always check whether existing tests still intercept correctly.
- Confidence: high

**2026-06-26 — E0b BE-11: `path_home_allowlist.txt` line numbers must be recomputed after any change that shifts existing callsite lines**
- Observation: Adding `EnvironmentFile=...` to `_UNIT_TEMPLATE` in `linux.py` (1 line) and `_WRAPPER_SCRIPT_TEMPLATE` + 7 new lines to `macos.py` shifted all subsequent `Path.home()` callsites by 1 and 7 lines respectively. The ratchet test `test_path_home_ratchet` fails with bidirectional set-difference errors when old line numbers remain in the allowlist. Also: new `Path.home()` callsites (e.g. `unregister()`) must be added to the allowlist alongside updated existing entries. Fix: run `uv run pytest tests/test_no_hardcoded_path_home.py -n0 -s` (not the full suite) to get the exact expected entries the ratchet computed, then update `path_home_allowlist.txt` to match.
- Action: After any change that adds lines before an existing `Path.home()` callsite, always recompute the allowlist entries. Run the ratchet test in isolation to see the exact expected set before guessing line numbers. Also add any new `Path.home()` callsites added in the same change.
- Confidence: high

**2026-06-26 — E0b BE-11: `source` is a bashism — use POSIX `.` (dot-source) in `#!/bin/sh` scripts**
- Observation: The initial wrapper script template used `source "${secrets_file}"`. `sh -n` syntax check (and an iterative-review C1-T-1 Critical finding) caught that `source` is a bash extension, not POSIX sh. Wrapper uses `#!/bin/sh`. Fix: replace `source` with `.` (dot-source). Combined with the `set -a`/`set +a` (allexport) bracket, the final idiom is `{ set -a; . "${secrets_file}"; set +a; }` — this exports all sourced variables to the exec'd child process, matching systemd `EnvironmentFile` semantics.
- Action: Any `#!/bin/sh` wrapper script must use `.` not `source`. Always run `sh -n <script>` in a test to catch bashisms before shipping. For variable export semantics to match `EnvironmentFile` (which auto-exports), add `set -a`/`set +a` around the dot-source.
- Confidence: high

**2026-06-26 — E0b BE-12: Idempotent helper functions should return bool to enable correct caller messaging**
- Observation: `_create_secrets_env` was initially `-> None`. C2 review caught that the "Created:" print fired even on re-install (file already existed), misleading operators into thinking their existing key was overwritten. Fix: return `bool` (True=created, False=already-existed-or-dry-run) and gate the print on that value. Adjacent pattern `_install_code_extra` is void because it has no equivalent user-visible artifact — but any idempotent file-creation helper that triggers a user-facing message needs a return signal.
- Action: Idempotent create-if-absent helpers that drive user-visible messages must return bool so callers can distinguish "created now" from "already existed". Never print "Created:" based solely on `not dry_run`.
- Confidence: high

**2026-06-26 — E0b BE-12: `Path.touch(mode=0o600)` works correctly by accident — always follow with explicit chmod**
- Observation: `Path.touch(mode=0o600)` passes mode to `os.open()`, which applies umask. For mode `0o600`, standard umask values cannot strip bits (no group/other bits to remove), so the file gets exactly 0o600. But this is accidental — the codebase convention (key_manager, install.py server-key block) is to always follow with an explicit `os.chmod()` or `path.chmod()`. Iterative review C2 caught this as a convention violation.
- Action: After `Path.touch(mode=M)`, always add `path.chmod(M)` for security-sensitive files. This makes intent explicit and survives any future mode change.
- Confidence: high

**2026-06-27 — E0c BE-1 Cycle-2 Brooks-Lint review: in-process shuffle does not satisfy the L12 "unbiased draw" intent**
- Observation: BE-1's brief (L12) asked for `ORDER BY RANDOM()` (or fallback shuffle) so the description-generation sample is "not biased to insertion order." The shipped code shuffles only the first `n` rows returned by `limit(n)` — i.e. it randomizes the *order* of an insertion-order-biased *window*, never the *membership*. For a 500-chunk collection with n=100, chunks 101-500 can never be sampled. The S16 integration test mocks `sample_chunk_texts` so it never exercises real selection bias, and the docstring/comment correctly disclose the limitation — so it is an intentional, documented scope cut, not a bug, but the brief's stated goal is only partially met.
- Action: When a brief states an *intent* ("unbiased draw") and the impl delivers only the *fallback* mechanism (shuffle-the-window), flag the gap explicitly as Moderate even when tests pass and docs are honest — passing tests against a mock do not prove the intent is met. Also: `generate_description` does `random.sample` on top of the store's `random.shuffle`, so the shuffle in the store is redundant for that one caller (sample already randomizes order); the shuffle only matters for other/future callers.
- Confidence: high
