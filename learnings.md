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

**[2026-06-28] — E2a plan-maker-for-team: `namespace` is a reserved keyword in TypeSpec**
- Observation: Using `namespace: string` as a parameter name in a core-construct TypeSpec `.tsp` op causes "multiple-blockless-namespace" parse errors. The fix is to rename the parameter (e.g., `ns: string`).
- Action: Never use `namespace` as a parameter name in TypeSpec `.tsp` files. Use `ns` or `collection_ns` instead.
- Confidence: high

**[2026-06-28] — E2a plan-maker-for-team: tsp compile --emit outputs to @typespec/openapi3/openapi.yaml, not the source file name**
- Observation: Running `tsp compile file.tsp --emit @typespec/openapi3 --output-dir .` produces `@typespec/openapi3/openapi.yaml` regardless of the source filename. Sequential compilation of multiple files overwrites the same file. Solution: compile each file individually and `cp @typespec/openapi3/openapi.yaml <named>.openapi.yaml` immediately after each compile.
- Action: When compiling multiple HTTP seam TypeSpec files in one session, always copy the generated openapi.yaml to a named file (e.g., `cp @typespec/openapi3/openapi.yaml e2a-ingest.openapi.yaml`) right after each compile step, before running the next compile.
- Confidence: high

**[2026-06-28] — E2a plan-maker-for-team: `expired_chunk_count` belongs in MaintenanceStatusDetail (global), not CollectionHealthEntry (per-collection)**
- Observation: The brief explicitly states `expired_chunk_count` is "aggregated across all collections" at GET /status call time — not per-collection. Both architecture and scenarios agents initially considered per-collection placement. Resolved by reading the brief's Key Decisions section.
- Action: For any status field that the brief calls "aggregated across all collections," place it in the top-level status sub-object, not in the per-collection health entry. Always verify with the brief's Key Decisions section before choosing placement.
- Confidence: high

**[2026-06-28] — E2a plan-maker-for-team: scope_filter is a sibling parameter, not added to SearchFilters**
- Observation: `scope_filter` has wildcard post-filter logic (Python-side on top-k set after LanceDB retrieval) that is architecturally different from the `SearchFilters` predicate-builder path. Adding it to `SearchFilters` would mix two different evaluation strategies. The correct design is a sibling parameter to `hybrid_search_with_trace` and pipeline methods.
- Action: When a filter parameter has different evaluation semantics (pre-retrieval predicate vs. post-retrieval Python filter) from existing `SearchFilters` fields, pass it as a sibling parameter rather than extending the SearchFilters class. Document the distinction explicitly in the internal seam contract.
- Confidence: high

**[2026-06-28] — E0e T-2: language filter e2e is a smoke test — document it clearly in the docstring**
- Observation: The S8 language filter e2e test cannot distinguish "filter applied, no matches" from "filter silently ignored" because the language detector stub assigns `language=""` to all chunks. DA agents flagged this as Critical/Major. The correct resolution is to document the limitation in a NOTE block in the docstring and cross-reference the BE-4 unit test that proves filter forwarding — not to add stub machinery.
- Action: When a tester-role e2e test for a language filter has empty expected results due to a stub, always add a `NOTE:` paragraph explicitly calling it a smoke test and pointing to the unit test that covers filter-forwarding behavior. This is the established T-1 REST pattern.
- Confidence: high

**[2026-06-28] — E0e T-2: multi-collection filter e2e must assert results span BOTH collections**
- Observation: S9 (file_type filter) initially only checked that all results had `file_type=="py"` but never verified results came from both collections. The DA review flagged this as Major. It is cheap to fix: `{r.get("collection") for r in results}` + two membership assertions.
- Action: Any multi-collection filter test that asserts on result properties MUST also assert that results span all expected legs. One-sided fan-out (only one collection contributing) is indistinguishable without the `collection` field check.
- Confidence: high

**[2026-06-28] — E0e T-2: string multiplication `"text\n" * N` only repeats the last literal in implicit concatenation**
- Observation: `"line1\n" "line2\n" "line3\n" * 4` repeats only `"line3\n"` four times. This creates a truncated fixture that can produce wrong results if content volume matters. The fix is `("line1\n" "line2\n" "line3\n") * 4` (wrap in parentheses before `* N`).
- Action: Whenever using `* N` on a multi-line implicit string concatenation, always wrap the entire block in `()` first. Apply this check to all fixtures in a new test file before the first review cycle.
- Confidence: high

**[2026-06-28] — E0e BE-5: vacuous glob test — always assert non-empty before a for loop over results**
- Observation: The glob test in BE-5 initially had no `assert result.results` guard. If no results were returned (possible with constant stub embedder), the `for r in result.results:` loop would never execute, making the test pass vacuously while proving nothing about the filter.
- Action: Any test that iterates `for r in result.results:` and asserts properties of each result MUST first assert `result.results` is non-empty with a descriptive message. Missing this means the test cannot fail in its most important failure mode.
- Confidence: high

**[2026-06-28] — E0e BE-5: avoid duplicate test function names across integration test files**
- Observation: `test_search_many_filter_glob_real_pipeline` was already present in `test_e0e_be2_search_many_filters.py` (added during BE-2). When BE-5 added a test with the same name, it caused pytest ambiguity (`-k` filter selects both). Fixed by renaming the BE-5 variant.
- Action: Before naming a test function, grep the tests/ directory for the intended name to avoid silent duplicate function names across files.
- Confidence: high

**[2026-06-28] — E0e T-1: excluded_collections is list[dict], not list[str]**
- Observation: `assert col_b not in excluded_collections` where `excluded_collections` is `list[dict]` always returns True. String membership in list[dict] never matches.
- Action: Always assert `excluded == []` for zero-result leg assertions. Never use `name not in list_of_dicts`.

**[2026-06-29] — E1a T-2: RRF interleaving requires asymmetric doc sizes and disabled reranker for fanout tests**
- Observation: With uniform zero-vector stubs, RRF ranks documents by insertion order (vector rank) + BM25 FTS. Equal-sized docs (10 chunks each) create symmetric RRF scores (both ≈ 0.031), and the stub reranker's stable sort on 0.5 scores preserves the col1-first input order in fanout all_cands. col2 candidates never reach top_k_return=5.
- Action: For fanout e2e tests that must surface results from BOTH collections: (1) make each "target" doc very short (single chunk, ingested first for low vector rank), (2) make each "source" doc long (20+ reps), (3) set `reranker_model = ""` in TOML to disable the reranker and enable global RRF sort across all collections. Without all three changes, col2 candidates cannot reliably appear in top 5.
- Confidence: high

**[2026-06-29] — iterative-review on E1b plan: fix agent sub-prompt too large causes zero edits**
- Observation: The first fix agent (C1) was given a large multi-issue prompt covering 7 fixes. It completed partially (applied F1-F7) but was cut off. The second pass needed to apply remaining fixes directly via Edit in the main context rather than another sub-agent.
- Action: Keep fix agent prompts under ~5 targeted fixes. For 10+ fixes, split into two sequential agents or apply directly in main context using batched Edit calls.
- Confidence: high

**[2026-06-30] — iterative-review on E1c plan (plan doc review, not code): layer and guard pattern issues dominate**
- Observation: Plan doc reviews surface different dominant issues than code reviews. The two most expensive cycles were: (1) guard layer — plan said "no exceptions in pipeline" but `graph_communities_not_built` physically requires a pipeline-level check (graph store is only accessible through the pipeline); (2) layer assignment — plan put TraversalStep/GraphProvenance in `schemas.py` but all other explain Pydantic models are in `routes_explain.py`, and _diagnostics.py (Entities) importing from schemas.py (Interface Adapters) violates Clean Architecture. Both required checking the actual codebase to resolve.
- Action: When reviewing plan docs for a feature that adds new types, always verify: (a) the exact file where analogous existing types live — don't assume by layer name; (b) whether any proposed guard requires data that's only accessible deeper in the stack (if yes, it cannot be a route-layer guard). Read the existing guard patterns in the real code before specifying new guards in the plan.
- Confidence: high

**[2026-06-30] — iterative-review on E1c plan: hyde_applied vs rag_fusion_applied are separately owned**
- Observation: The plan conflated `hyde_applied` and `rag_fusion_applied` as both pipeline-owned. In reality `hyde_applied` is computed by the route handler (before calling the pipeline, via `resolve_hyde_vector()`), while `rag_fusion_applied` IS returned by the pipeline in `ExplainPipelineResult`. Specifying "pipeline enforces both False when graph_mode is set" was correct for rag_fusion but wrong for hyde_applied — the route handler must explicitly override it.
- Action: When writing invariants about response booleans on `/explain`, always check which layer computes each flag. Read `from_pipeline_result()` to see which flags are pipeline-owned params vs route-handler-computed params. State the owning layer explicitly in the plan invariant.
- Confidence: high

**[2026-06-29] — BE-7b fanout: test spec overrides scenario spec for S9 in multi-collection context**
- Observation: S9 (isolated nodes) in the team plan says "falls back silently to naive graph expansion." But the BE-7b test spec explicitly says "collection B has no community (isolated nodes); Collection B falls back to hybrid for that leg." Multiple review agents (architecture, correctness, Brooks-Lint) flagged the hybrid fallback as violating S9. They were wrong — the test spec is authoritative for the fanout context.
- Action: When scenario spec and task-level test spec conflict, the task-level test spec wins. Always read the `Tests` block of the specific task (not just the Scenarios section) to determine expected behavior.
- Confidence: high

**[2026-06-29] — BE-7b fanout: vacuous OR assertion is a Critical finding — always use separate asserts for multi-leg tests**
- Observation: `assert "chunk-a1" in chunk_ids or "chunk-b1" in chunk_ids` passes if only ONE of the two collections contributes results. This is unfalsifiable for the "both legs must appear" contract. Four out of four reviewers flagged it as Critical/Major.
- Action: In multi-collection fanout tests, ALWAYS use separate assert statements for each collection's expected results: `assert "chunk-a1" in chunk_ids` and `assert "chunk-b1" in chunk_ids`. Never combine with `or`.
- Confidence: high

**[2026-06-29] — E1b T-1 e2e: same db_path for GraphStore and SearchStore matches production CLI**
- Observation: Test (a) initially used separate paths (`graph_db_path` and `db_path`) with a fragile `GraphStore.__init__` patch. Reviews identified this as Major because the production CLI uses the same path for both stores — graph tables use `_archon_graph_` prefix to avoid collision. The patch silently discarded the path argument.
- Action: In CLI e2e tests, always seed `GraphStore` and `SearchStore` using the same `db_path` that `mock_cfg.db_path` will point to. Remove `GraphStore.__init__` patches — they are never needed when paths are aligned.
- Confidence: high

**[2026-06-29] — E1b T-1 e2e: module-level pytest.importorskip skips entire file, not just one test**
- Observation: `pytest.importorskip("leidenalg")` at module scope caused all three tests to be skipped (only leidenalg-gated test should skip). The fix is a `try/except ImportError` block setting `_LEIDENALG_AVAILABLE` and `@pytest.mark.skipif(not _LEIDENALG_AVAILABLE, ...)` on just that test.
- Action: Never use `pytest.importorskip` at module scope for optional dependencies. Use a module-level `try/except ImportError` flag + `@pytest.mark.skipif` on the individual test function.
- Confidence: high

**[2026-06-29] — iterative-review on E1b plan: JSONL output files too large to Read directly**
- Observation: DA agent output JSONL files were 400KB+. Attempting to Read them overflows context. Use `python3 -c "import json,sys; [print(m['content'][0]['text']) for l in open(sys.argv[1]) for m in [json.loads(l)] if m.get('role')=='assistant']" <file>` to extract only assistant text.
- Action: Never Read DA agent output JSONL files directly. Always use the python3 extraction one-liner to pull only assistant text before processing findings.
- Confidence: high

**[2026-06-29] — iterative-review on E1b plan: already-exists phantom — always grep before specifying "extract" tasks**
- Observation: The plan incorrectly stated "`_extract_ngrams` should be extracted from `GraphExpander._expand_sync()`". Reality: `tokenize_and_generate_ngrams(query, max_n)` is ALREADY a module-level free function at `graph_expander.py:81`. The plan invented a refactoring task for something already done. DA agents caught this in Cycle 3.
- Action: Before specifying any "extract X into a shared helper" task in a plan, grep the codebase for the function name to confirm it's actually inlined. Plans that reference phantom extractions waste implementation time and mislead reviewers.
- Confidence: high

**[2026-06-29] — iterative-review on E1b plan: ScoredSearchCandidate has no score field — scores live in SearchScoreBreakdown.rrf_score**
- Observation: The plan initially said "convert community chunks to ScoredSearchCandidate with initial score=1.0". ScoredSearchCandidate (_diagnostics.py:47) has NO `score` field — all scoring lives in `SearchScoreBreakdown` (a required nested field with `rrf_score: float` + 7 optional fields). Setting "score=1.0" would fail at construction time.
- Action: When specifying synthetic candidate construction for community/graph modes, always name the full `SearchScoreBreakdown(vector_rank=None, ..., rrf_score=1.0, reranker_score=None)` spec. Never say "score=1.0" without verifying the actual dataclass field.
- Confidence: high

**[2026-06-29] — iterative-review on E1b plan: FTS BM25 IDF breaks symmetry only when one doc is much rarer than the other**
- Observation: When two docs have equal chunk counts (10 each), BM25 IDF for each term is log(1 + 10.5/10.5) ≈ log(2) ≈ 0.693 — the same for both. Expansion adds a term to the query but both docs still get similar RRF scores. Making the target doc very short (1 chunk vs 10) gives its unique term IDF = log(1 + 10.5/1.5) ≈ 2.08, making it rank #0 in FTS for the expanded query.
- Action: When testing that expansion surfaces a target doc, make that doc short (1 chunk) and the source doc long. This is the only reliable way to create large IDF asymmetry that guarantees target rises above baseline threshold.
- Confidence: high

**[2026-06-28] — E0e T-1: tester-role e2e tests for language filter must use language filter (not file_type)**
- Observation: S10 regression scenario specified `language: "en"` but the test was written with `file_type: ".md"`. The language filter is the one whose restriction was lifted by E0e; using a different filter doesn't guard the correct regression.
- Action: Match the exact filter type stated in the scenario. Language stub returns `language=""` so results are empty, but 200 still proves the path wasn't broken.
- Confidence: high

**[2026-06-28] — E0e T-1: coverage illusion from missing result assertion**
- Observation: An S2 test that asserts only `status=200` and `applied_filters echo` but never checks `data["results"]` passes whether the filter works or is silently ignored. Reviewers flagged this as a coverage illusion.
- Action: Always add `assert data["results"] == []` when the expected result is empty, with a comment explaining why. This eliminates ambiguity between "filter returns empty" and "filter silently dropped".

**[2026-06-29] — E1a BE-5: post-persist auxiliary writes must be wrapped in try/except**
- Observation: The graph write block (ensure_graph_tables, write_graph, edge_count) runs AFTER chunks are committed to LanceDB. Without try/except, any I/O error there raises instead of returning a graceful IngestResult — breaking the watcher's "always returns, never raises" contract. DA/Brooks reviewers flagged this as Critical.
- Action: Whenever wiring an auxiliary (non-primary) write step AFTER the main persist in ingest_file, always wrap the block in try/except, log a WARNING, and return status="ok" with a warning appended to IngestResult.warnings. Never let auxiliary failures propagate as exceptions after persist commits.
- Confidence: high

**[2026-06-29] — E1a BE-5: factory function must be updated when new optional dependencies are added to SearchPipeline.__init__**
- Observation: create_pipeline() is used by all CLI callers (ingest, sync, collection subcommands). Adding graph_extractor/graph_store/graph_config only to the direct SearchPipeline() call in app.py silently left CLI paths with graph.enabled=True but no extraction. Reviewers flagged as Critical.
- Action: Whenever adding new optional dependencies to SearchPipeline.__init__, check immediately whether create_pipeline() also needs updating. The factory is the canonical non-server construction path.
- Confidence: high

**[2026-06-29] — implement-all: subagents produce commit message draft instead of executing commit**
- Observation: Task 4.2 (T-5) subagent completed all implementation, ran review, passed tests, checked off the plan, but ended its turn by emitting a `git commit -F-` draft block for the user to run manually instead of committing. Recovery: parent agent detected missing commit via git log, verified the test passes, staged exact files, and committed manually.
- Action: Subagent prompt must include an explicit instruction: "After acceptance criteria A–F, invoke commit-message skill with `commit` argument (commit mode), NOT draft mode. The commit MUST land in git history before you end your turn." Recovery check must verify `git log --oneline -1` contains a new SHA, not just that the plan checkbox is ticked.
- Confidence: high

**[2026-06-29] — E1a BE-5: fatal_error path must have an explicit test — otherwise the early-return logic is invisible**
- Observation: The extraction fatal_error path (early return before persist) was the most critical error path in BE-5 but had zero test coverage in the initial implementation. DA review flagged it as Critical. The fix was a single additional unit test.
- Action: Every early-return in ingest_file (i.e., every non-happy-path that returns before chunks are written) must have at least one unit test verifying (a) status="error", (b) chunks_created=0, and (c) no downstream write methods called.
- Confidence: high

**[2026-07-02] — E1c T-2: S6 scenario says "(both tested)" — always add separate tests for each graph_mode value**
- Observation: S6 explicitly says "graph_mode='local' or 'global' (both tested)." The initial implementation only wrote a test for "local". Three DA reviewers independently flagged the missing "global" variant as Major. The plan task description only mentions "local" explicitly, but the S6 scenario definition (which T-2 "completes") mandates both.
- Action: When a scenario says "both tested" or "each mode tested," always add separate tests for each. The task spec's T-2 Tests block naming only one mode does not override the scenario definition; the scenario is authoritative for what "completes S6" means.
- Confidence: high

**[2026-07-02] — E1c T-2: type("Meta", ...) anonymous stub is fragile — use MagicMock for route-handler integration tests**
- Observation: Using `type("Meta", (), {"active_embedding_model": None})()` as a meta stub fails with AttributeError if the route handler ever reads any other attribute. Brooks-Lint and one DA agent flagged this as Moderate. `MagicMock(active_embedding_model=None)` tolerates incidental attribute reads while still returning None for the key field.
- Action: Use `MagicMock(spec_set=False, active_embedding_model=None)` or plain `MagicMock(active_embedding_model=None)` instead of anonymous types when stubbing objects for route-handler integration tests. This is safer and consistent with the existing BE-5 tests.
- Confidence: high

**[2026-07-02] — E2a BE-4 TDD: TOML `[collections]` section format required for `make_real_app(toml_content=...)`**
- Observation: Writing `toml_content = f'collections = ["{path}"]\n'` (top-level key) does NOT populate `cfg.collections`. The config loader reads `doc.get("collections", {})` treating the `[collections]` TOML section as a dict, then reads `collections["collections"]`. A top-level `collections = [...]` key returns a list, not a dict, so `"collections" in list` is False and the path is silently ignored.
- Action: When using `make_real_app(toml_content=...)` to register collection paths, always write `[collections]\ncollections = ["/path"]\n` (with the section header). A bare `collections = [...]` at the top level is silently ignored.
- Confidence: high

**[2026-07-02] — E2a BE-4 TDD: `_make_patch_app` in test_routes_collections.py returns 3-tuple, not 2-tuple**
- Observation: `_make_patch_app` returns `(client, mock_store, validate_patch_ctx)`. Using `client, _ = _make_patch_app(...)` raises `ValueError: too many values to unpack`. The third value (`validate_patch_ctx`) is the context manager for mocking `validate_embedding_model`.
- Action: Always unpack `client, mock_store, validate_patch_ctx = _make_patch_app(...)` when using this helper. When the context manager is not needed (e.g., the test patches it separately), use `_validate_patch_ctx` as the throwaway name.
- Confidence: high

**[2026-07-02] — E2a BE-5 MCP TDD: importlib.reload inside patch() context breaks the patch**
- Observation: Calling `importlib.reload(mcp_mod)` inside a `with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):` block causes the module to re-execute `from fastmcp import FastMCP`, which overwrites the patched value with the real FastMCP class. The `_FakeFastMCP` stub is silently discarded and the test fails with `AttributeError: 'FastMCP' object has no attribute 'tools'`.
- Action: Never call `importlib.reload()` inside a `patch()` context manager for MCP unit tests. Instead, import the module once (`from archon_search.server import mcp as mcp_mod`) inside the patch block — the patch already replaces the attribute on the existing module object; no reload is needed.
- Confidence: high

**[2026-07-03] — `dict(module_level_dict)` is a shallow copy — nested mutable values are shared**
- Observation: `_EMPTY_STATE` contained `"collection_health": {}` and `"retry_counts": {}` as nested dict literals. Returning `dict(_EMPTY_STATE)` from `_load_state()` created a new outer dict, but the SAME inner `{}` objects were shared across all callers. When `_run_one_pass` mutated `health[key] = col_health`, it was mutating the module-level `_EMPTY_STATE["collection_health"]` dict, causing test contamination across test runs in the same process.
- Action: When returning a defensive copy of a module-level constant dict that contains mutable values, use `copy.deepcopy(the_dict)` — never `dict(the_dict)`. This applies to any `_EMPTY_*` sentinel dicts with list or dict values.
- Confidence: high

**[2026-07-02] — E2a BE-5 MCP: scope length validation must use len(scope) not len(scope.encode("utf-8"))**
- Observation: The REST layer (`routes_jobs.py` line 80) validates scope length using `len(item)` (Python character count). The initial MCP implementation used `len(scope.encode("utf-8"))` (UTF-8 byte count). These diverge for multibyte characters: a 200-char CJK scope is 400 UTF-8 bytes and would pass REST validation but fail MCP validation.
- Action: Always use `len(scope)` (Python character count) for scope string length validation to match the REST layer. Never use `.encode("utf-8")` for this purpose in MCP tools.
- Confidence: high

**[2026-07-02] — E2a BE-5 MCP: validation tests for ingest_directory must be added separately — don't assume a shared helper means shared coverage**
- Observation: When the implementation was refactored to use `_validate_ttl_and_scopes()` shared helper, reviewers initially suggested the tests for `ingest_file` validation also covered `ingest_directory`. This is wrong: before extraction, two separate blocks existed and could have had different bugs; after extraction, the two call sites need separate tests only to verify the helper is actually called. But the plan's test list only specified `ingest_file` validation tests, leaving `ingest_directory` without validation coverage.
- Action: Always add validation tests for each MCP tool separately (even when sharing a helper), specifically: (1) zero/negative TTL test per tool, (2) overlong scope test per tool. This proves the helper is wired up correctly in each tool's code path.
- Confidence: high

**[2026-06-29] — E1a BE-5: spaCy model wheel version must match spaCy minor version range**
- Observation: Adding `en_core_web_sm-3.8.0` wheel URL with `spacy>=3.7,<4` allows spaCy 3.7.x which is incompatible with the 3.8.0 model (spaCy model versions are minor-version-locked). DA review caught this as Major. Fix: tighten to `spacy>=3.8,<3.9`.
- Action: When adding a pinned spaCy model wheel URL to optional extras, always match the spaCy version range to the model's minor version. Use `spacy>=X.Y,<X.(Y+1)` and `en_core_web_sm-X.Y.Z` with matching major.minor.
- Confidence: high

**[2026-07-03] — iterative-review on E2b plan doc: plan-to-brief divergence silently drops requirements**
- Observation: The E2b brief explicitly specified a `_MENTIONS_SCAN_CEILING` constant (OOM safety valve for large mention tables). The plan dropped it entirely. DA agents caught this as Critical — the implementer would build an unbounded scan. Also, the plan's `edge_count` description said "at least one surviving node" while the algorithm and test both required "BOTH endpoints survive" — a one-word inconsistency that would cause implementors to compute the wrong predicate.
- Action: When writing a plan from a brief, always search the brief for every capitalized constant name (e.g. `_MENTIONS_SCAN_CEILING`, `MAX_SOURCE_CHUNK_IDS`) and verify each one has a corresponding task or constant definition in the plan. Also: any quantifier word in a definition (at least one, both, all) must match exactly the quantifier in the algorithm and tests.
- Confidence: high

**[2026-07-03] — iterative-review on E2b plan doc: _truncate_graph signature contradicted its own algorithm**
- Observation: The fix agent added `surviving_entity_ids` to the `_truncate_graph` parameter list while the algorithm description said "step 1 produces it." This created two incompatible implementations: (A) caller pre-caps nodes and passes the set; (B) helper does all 4 steps including cap. The resolution was to drop the parameter (Interpretation B — helper owns all steps, set is internal). The "What changes" section already had the correct simpler signature; only the BE-6/BE-9 task body was wrong.
- Action: When a helper function's algorithm description says "step N produces X," X must NOT appear in the function's parameter list. If it does, one of the two is wrong — the algorithm is usually more authoritative. Always cross-check parameter lists against the step-by-step algorithm in the same section.
- Confidence: high

**[2026-07-02] — E2a BE-4: `page_count` not `total` for scan-bounded endpoints**
- Observation: `ExpiringChunksResponse` initially had `total: int = len(items)`. This is page count, not a cross-page total — `DocumentListResponse.total` IS a cross-page total. Using the same field name for two semantically different things is a silent lie. DA review flagged it as Major.
- Action: Any endpoint whose count field only reflects the current page's item count (not the global store total) must be named `page_count`, `returned_count`, or similar — never `total`. Use `total` only when the value represents the full collection count (paginated or not).
- Confidence: high

**[2026-07-02] — E2a BE-4: PATCH handler requires `model_dump(exclude_unset=True)` for absent-vs-null semantics**
- Observation: `PATCH /collections/{name}` must distinguish "field absent from payload" (no change) from "field explicitly set to null" (clear). Without `exclude_unset=True`, Pydantic sets all absent fields to their `default` (e.g. `None`), making absent and explicit-null indistinguishable.
- Action: Always use `payload = body.model_dump(exclude_unset=True)` in PATCH handlers, then gate each sub-behavior on `"field_name" in payload`. Never check `body.field_name is not None` alone.
- Confidence: high

**[2026-07-02] — E2a BE-4: range validator needed on PATCH field, not just ingest field**
- Observation: `IngestRequest.chunk_ttl_seconds` had a range validator added immediately. `PatchCollectionBody.default_ttl_seconds` did not — 0 or -1 would create immediately-expired chunks silently. DA review cycle caught the omission.
- Action: Whenever adding a TTL field to any request model, immediately add a range validator `[_TTL_MIN, _TTL_MAX]` in the same step. Check every model that touches the same semantic field — ingest, patch, and any future body — before the first review cycle.
- Confidence: high

**[2026-07-02] — E2a BE-4: STORE_SCHEMA_VERSION bump propagates to 3 call sites beyond store.py**
- Observation: Bumping STORE_SCHEMA_VERSION from 0 to 1 caused 11 test failures. Beyond the test guard assertions (`assert STORE_SCHEMA_VERSION == 0`), two production call sites set `schema_version=0` as a hardcoded default: (1) `pipeline.py` uses `existing_meta.schema_version if existing_meta else 0`; (2) `routes_collections.py` creates stub meta with `CollectionMeta(...)` whose default `schema_version=0`. Both needed fixing: pipeline.py imports `STORE_SCHEMA_VERSION` and uses it as the else-branch; `add_collection` route passes `schema_version=STORE_SCHEMA_VERSION` to the stub. Additionally, integration test seed rows that build schemas from `_meta_schema()` must explicitly exclude any new non-null integer columns added (like `default_ttl_seconds`).
- Action: Before bumping STORE_SCHEMA_VERSION, grep for `schema_version=0` hardcoded defaults and `else 0` in schema_version assignment branches in production code. Every hardcoded `0` is a regression waiting to happen. Also, any seed row in integration tests that uses `_meta_schema()` must be audited to include all new nullable/int columns.
- Confidence: high

**[2026-07-02] — E2a BE-1: LanceDB `add_columns` accepts a PyArrow Field directly for nullable columns**
- Observation: The `migrate_acl` method calls `table.add_columns(acl_field)` where `acl_field = pa.field("acl", pa.list_(pa.utf8()), nullable=True)`. This is the correct pattern for adding nullable columns — passing a `pa.Field` object directly. For non-nullable columns with defaults, the dict form `{"col": "cast(0 as bigint)"}` is used. The two forms serve different purposes and must not be mixed.
- Action: For nullable new columns (null default), use `table.add_columns(pa.field(..., nullable=True))`. For non-nullable columns with SQL-expression defaults, use `table.add_columns({"col": "cast(0 as bigint)"})`. Never use the dict form for nullable columns.
- Confidence: high

**[2026-07-02] — E2a BE-1: LanceDB `.limit()` is a scan limit, not a sort-then-limit — pagination methods must fetch all then sort**
- Observation: `query_expiring_chunks` initially used `.limit(limit + 1)` before Python-side sort. LanceDB's `.limit()` returns the first N rows in storage order, not sort order. Sorting those N rows then paginating produces wrong results when there are more than `limit + 1` qualifying rows — rows that sort earlier but are stored later are silently missed.
- Action: For any store method that needs sorted cursor pagination with Python-side sort: (1) fetch ALL matching rows up to a hard ceiling constant (e.g., `_EXPIRING_SCAN_CEILING = 10_000`), (2) sort Python-side, (3) then apply cursor filter and paginate. Never apply `.limit()` before the sort unless LanceDB natively supports `ORDER BY` for the query type.
- Confidence: high

**[2026-06-29] — E1a FE-2: graph_mode enabled flag check needs spaCy stub to construct app in tests**
- Observation: `create_app()` raises `ConfigError` when `config.graph.enabled=True` but spaCy is not installed (the `archon-search[graph]` extras are absent in CI and local dev by default). Any test that constructs an app with `graph_enabled=True` must inject a stub `types.ModuleType("spacy")` into `sys.modules["spacy"]` around the `create_app()` call, then restore the original (or remove it) in a `finally` block.
- Action: Pattern for graph-enabled app in tests: inject spacy stub before `create_app`, restore in `finally`. The stub only needs to exist in `sys.modules` — no attributes needed for the startup check.
- Confidence: high

**[2026-06-29] — E1a FE-2: adding new response field breaks exact-dict snapshot tests**
- Observation: `test_search_response_schema_fields` in `test_routes_search_acl.py` did an exact `model_dump()` dict comparison. Adding `graph_expansion_applied: bool = False` to `SearchResponse` caused an `AssertionError: Left contains 1 more item`. Fix: add the new field to the expected dict.
- Action: After adding any field to a Pydantic response model, grep for exact `model_dump()` dict comparisons in the test suite and update them. These snapshot-style tests are intentional regression guards — update them, don't weaken them.
- Confidence: high

**[2026-06-29] — E1a BE-1: path_home_allowlist.txt line numbers shift when new code is added above the callsite**
- Observation: Adding 12 lines (GraphConfig dataclass) above `get_default_config_path()` in `config.py` shifted the `Path.home()` callsite from line 203 to line 215, causing `test_path_home_ratchet` to fail with "new unallowlisted callsite." The allowlist is line-number-sensitive.
- Action: After adding code to any file in `archon_search/` that contains allowlisted `Path.home()` callsites, always check and update `tests/path_home_allowlist.txt` line numbers before running the test suite. Run `grep -n "Path.home()" archon_search/config.py` to find the new line numbers.
- Confidence: high

**[2026-06-29] — E1a FE-3: assert_not_called() in search_with_context guard tests must target the correct pipeline method**
- Observation: `test_mcp_search_with_context_graph_mode_returns_error` called `pipeline.search.assert_not_called()` but the `search_with_context` tool invokes `pipeline.search_with_context()` — a different method. The assertion passed even before the guard because `pipeline.search` is never called on the `search_with_context` path at all. DA Cycle 2 flagged this as Major.
- Action: When testing an early-return guard in a tool function, always assert `not_called()` on the exact pipeline method that the guarded code path would have called — not a sibling method. For `search_with_context`, assert `pipeline.search_with_context.assert_not_called()`.
- Confidence: high

**[2026-06-29] — E1a FE-3: MCP error returns must use McpErrorResponse, not ad-hoc plain dicts**
- Observation: The first implementation returned `{"code": ..., "message": ...}` for the new graph_mode guards. Every other error in `mcp.py` (60+ occurrences) uses `McpErrorResponse(error=..., code=...)`. DA Cycle 1 flagged this inconsistency as Major. Any MCP client checking for `response.get("error")` to detect failures gets None from the new shape and treats the error as success.
- Action: All MCP tool early-return errors must use `McpErrorResponse(error=..., code=...)`. Never return a custom dict shape from a tool error path in `mcp.py`.
- Confidence: high

**[2026-06-28] — E1a iterative-review: TypeSpec contracts must be updated in lockstep with plan prose**
- Observation: After Cycle 1 changed the entity stable ID formula and added `entitySubtype`, the .tsp contract files still had the old formula and missing field. The plan said "fix before implementation" but the files weren't actually updated. Cycle 3 reviewers flagged all four .tsp discrepancies as Major issues.
- Action: When a plan change affects a contract seam, update the .tsp file in the SAME edit session as the plan prose. Never defer contract file fixes with a "fix before implementation" note — the files are the contract.
- Confidence: high

**[2026-07-03] — E2a BE-12: adding a field to a Pydantic schema breaks exact-field-set snapshot tests in the same test file**
- Observation: Adding `scopes: list[str] = []` to `DocumentInfoSchema` caused `test_document_info_schema_fields` in `test_mcp_schemas.py` to fail — it asserted an exact field set without the new `scopes` field. The test was an intentional regression guard.
- Action: After adding any field to a Pydantic schema in `mcp_schemas.py`, grep `tests/test_mcp_schemas.py` for the class name and check for exact field-set assertions. Update them in the same step. These tests are regression guards, not snapshots to delete.
- Confidence: high

**[2026-07-03] — E2a BE-12: MCP scope_filter validation must be a module-level helper, not inline code per tool**
- Observation: BE-12 required the same validation logic in three tools (search, search_with_context, explain). Inlining the 10-line validation in each tool would create three divergence points. Extracting `_validate_scope_filter()` as a module-level helper (analogous to `_validate_ttl_and_scopes`) keeps validation logic in one place and is consistent with the existing MCP validation pattern.
- Action: Any validation logic needed in more than one MCP tool must be extracted to a module-level `_validate_*` helper following the `_validate_ttl_and_scopes` pattern: accepts params, returns `McpErrorResponse | None`. Never inline the same validation logic in multiple tool closures.
- Confidence: high

**[2026-06-28] — E1a iterative-review: query-time expansion cannot call make_stable_entity_id (unknown entity_type)**
- Observation: The plan initially said GraphExpander calls `make_stable_entity_id` to look up nodes. But `make_stable_entity_id` requires `entity_type` which is unknown at query time. The correct approach is a `findNodesByName` lookup (by entity_name, case-insensitive) — name-based, not ID-based.
- Action: At query time, always use name-based lookup (findNodesByName / exact case-insensitive match) against graph node tables, not stable ID computation. Stable IDs are an ingest-time concern; query expansion is a runtime name-matching concern.
- Confidence: high

**[2026-06-28] — E1a iterative-review: edge creation in spaCy-only mode must be explicitly specified**
- Observation: The plan listed typed relationship enums (USES, IMPLEMENTS, etc.) and deferred LLM extraction, but never said how edges are created without LLM. Three review cycles passed before someone asked: who creates the edges? The answer (co-occurrence within same chunk → RELATED_TO) is obvious but must be written down or the feature ships with nodes-only and graph expansion is a no-op.
- Action: Any plan that defers relationship extraction must explicitly document the fallback edge-creation heuristic. "Edges deferred" means the feature is a no-op; that must be stated explicitly if intentional.
- Confidence: high
- Confidence: high

**[2026-06-29] — E1a T-2: fanout e2e tests must verify result collection provenance, not just content**
- Observation: The initial T-2 fanout test asserted `any("RS256" in t)` and `any("LRU" in t)` in merged results. DA review flagged that a global-expansion implementation (expanding once and broadcasting to all legs) would also pass, since both terms would still appear. The correct fix is asserting `result["collection"]` membership: `{r["collection"] for r in data["results"]}` must include both col1 and col2.
- Action: Any fanout e2e test asserting on per-collection content MUST also assert `{r["collection"] for r in results}` includes every expected collection. Content-only assertions cannot prove per-leg independence.
- Confidence: high

**[2026-06-29] — E1a T-2: negative baseline assertions must guard against empty results**
- Observation: `not any("RS256" in t for t in baseline_texts)` trivially passes when baseline_texts is empty (e.g., collection empty, ingest failed). The test would then pass even if the graph infrastructure was broken. Fix: add `assert len(baseline_texts) > 0` before the `not any(...)` check.
- Action: Always guard negative baseline assertions with `assert len(results) > 0` before the negative content check. An empty list makes `not any(...)` vacuously true.
- Confidence: high

**[2026-06-29] — E1a T-3: assertion ordering — presence check before value check**
- Observation: In `test_e2e_graph_mode_noop_empty_graph`, the initial version asserted `data.get("graph_expansion_applied") is False` before asserting `"graph_expansion_applied" in data`. If the field is absent, `.get()` returns `None`, `None is False` is `False`, and the first assertion fails with a confusing type mismatch message. The presence check is then unreachable dead code.
- Action: In any test checking both presence and value of a response field, always assert presence first (`assert "field" in data`), then use direct indexing (`data["field"]`) for the value check. The ordering matters for diagnostic clarity.

**[2026-07-03] — BE-10: wildcard scope filter semantics — 'user:*' matches ALL scopes starting with 'user:', including 'user:bob'**
- Observation: `_apply_scope_wildcard_filter` with filter `"user:*"` (prefix `"user:"`) includes `"user:bob"`, `"user:alice"`, `"user:alice:thread"` — all start with `"user:"`. Three test bugs were written assuming "user:bob" would be excluded by "user:*". The wildcard pattern is a prefix match, not a glob exclusion for sibling prefixes.
- Action: When writing wildcard scope filter tests that must exclude a specific sub-scope (e.g., "user:bob"), use a more specific wildcard like `"user:alice*"` (prefix `"user:alice"`) which excludes "user:bob" (doesn't start with "user:alice"). Never write test assertions expecting `"user:*"` to exclude `"user:bob"`.
- Confidence: high

**[2026-07-03] — BE-10: new keyword arg on store/pipeline method signature requires updating ALL test stubs — use grep before running suite**
- Observation: Adding `scope_filter=None` to `hybrid_search_with_trace` call sites caused 49 test failures. All failures were test stubs with signatures like `async def _hybrid(col, vec, q, depth, filters=None)` that didn't accept the new kwarg. `replace_all` in Edit only caught exact-match patterns — stubs with different type annotations (`list[float]` vs `Any`) required separate fixes.
- Action: When adding a new keyword argument to any widely-stubbed method, run `grep -rn "def.*<method_name>" tests/` before running the suite. Fix all stubs before the first `uv run pytest` to avoid a large red run. Watch for type annotation variations that prevent `replace_all` from catching all instances.
- Confidence: high

**[2026-07-02] — E2a iterative-review (plan doc, 12 cycles): phantom function/class names recur across every cycle**
- Observation: Across 12 review cycles, phantom references recurred constantly: `format_utc_timestamp` (doesn't exist; real fn is `normalize_iso_utc` in `_types.py`), `CollectionUpdateRequest` (real name is `PatchCollectionBody` in `schemas.py`), `_run_migrations()` (real method is `_run_startup_migrations()` or `apply_in_place_migrations()`), `_build_status_response` (doesn't exist; logic is inline in `async def status()` at routes_status.py line 45). All required DA reviewers to grep the codebase to resolve.
- Action: When writing any plan that references existing functions, classes, or route handlers by name, grep the codebase first to verify the exact name. Never write a plan referencing a symbol you haven't verified.
- Confidence: high

**[2026-07-02] — E2a iterative-review: PatchCollectionBody.embedding_model being required blocks partial PATCH**
- Observation: `PatchCollectionBody` in schemas.py had `embedding_model: str` as a required field. The E2a plan needed to add `default_ttl_seconds` as an optional PATCH field, but any PATCH without `embedding_model` would return 422. Fix requires: (1) make `embedding_model: str | None = None`, (2) update the `validate_embedding_model_not_empty` validator to guard `if v is not None and not v`, (3) restructure the handler to `payload = body.model_dump(exclude_unset=True)` and gate ALL embedding model logic behind `'embedding_model' in payload`. This is a breaking API change requiring a BREAKING.md entry.
- Action: When adding a new optional field to an existing PATCH body that has required fields, always check whether existing required fields block partial-body requests. If so, document the breaking change (optional-ification) as a prerequisite task, not an afterthought.
- Confidence: high

**[2026-07-02] — E2a iterative-review: apply_in_place_migrations calls zero-arg global methods**
- Observation: `apply_in_place_migrations` dispatches via `getattr(self, spec.name); await method()` — zero arguments. New migration methods must be global (iterate ALL collections internally, guard per-table with schema inspection). This means `POST /collections/foo/migrate` silently migrates all collections, not just `foo`. The schema_version in meta is bumped only for the requested collection. This is the same behavior as v0 migrations and must be documented in the operator runbook.
- Action: When designing LanceDB schema migrations, always specify whether the migration method is global (zero-arg, iterates all) or per-collection (takes collection+ns args). Check the existing `apply_in_place_migrations` dispatch to avoid over-specifying a per-collection pattern that doesn't fit the framework.
- Confidence: high

**[2026-07-02] — E2a iterative-review: defensive assertions are dead code without caller forwarding**
- Observation: Adding `scope_filter: str | None = None` + `assert scope_filter is None` to graph-mode methods is useless without the callers forwarding `scope_filter=scope_filter`. Since the default is `None`, omitting the forward means the assertion always passes — it can never fire even if the 422 guard is removed later. This required an additional fix to enumerate all call sites that must forward the parameter.
- Action: Whenever adding a defensive `assert param is None` to a method, also enumerate every caller that must forward the parameter explicitly. Without forwarding, the assertion is dead code and provides zero safety net.
- Confidence: high

**[2026-07-02] — E2a iterative-review: LanceDB _do_ingest extra-key behavior needs schema guard**
- Observation: Adding `'expires_at': c.expires_at` and `'scopes': c.scopes` to the `_do_ingest` row dict unconditionally risks LanceDB auto-adding those columns on un-migrated tables (schema evolution on write), bypassing the migration framework and leaving `schema_version` inconsistent. The safe pattern: `has_ttl_cols = 'expires_at' in [f.name for f in table.schema]`; include the new keys only when `has_ttl_cols` is True.
- Action: When adding new schema columns via migration, always add a corresponding schema guard in the ingest row dict builder. Never include extra keys unconditionally — check the table schema first to avoid unintentional schema evolution on un-migrated tables.
- Confidence: high

**[2026-07-02] — E2a K1 iterative-review: TypeSpec contracts can compile clean and still contradict the plan**
- Observation: All 5 E2a TypeSpec contracts compiled without errors, yet reviewers found Critical issues: `expired_chunk_count: int32 | null` contradicted the plan's "always non-null (0 or more)" spec, and a doc comment described a per-chunk TTL override that is explicitly deferred to v2. TypeSpec compilation only validates syntax — it does not catch semantic disagreements with the authoritative plan.
- Action: After authoring TypeSpec contracts, always cross-check nullable vs non-nullable fields against the plan's acceptance criteria. Pay special attention to count/status fields — "always an integer" in the plan means non-nullable in the TypeSpec. Run this check before calling K1 complete.
- Confidence: high

**[2026-06-29] — E1a T-3: MCP test xdist_group("mcp") must be on ALL files with MCP tests — even when mixed with non-MCP tests**
- Observation: test_e1a_t3 had two REST-only tests and two MCP tests, all under `pytestmark = pytest.mark.integration`. Missing `xdist_group("mcp")` means the MCP tests could run in parallel with other MCP tests across files, causing session/port conflicts. The convention across 17+ MCP integration test files is to always include `xdist_group("mcp")`.
- Action: Any test file that contains even one MCP test must include `xdist_group("mcp")` in its `pytestmark`. Applies at module level even if only some tests in the file use MCP.
- Confidence: high

**[2026-07-03] — E2a BE-9: stale mock assertion breaks on new kwargs — always include all default kwargs**
- Observation: `test_lance_store_hybrid_search_with_trace_delegates_to_module_function` in `test_store_trace.py` had `mock_fn.assert_awaited_once_with(..., filters=None)`. After BE-9 added `scope_filter=None` to the forwarded call, the assertion failed because the actual call included `scope_filter=None` but the expected call did not. The test was asserting exact kwargs.
- Action: When a delegate method (thin instance-method wrapper) gains a new parameter with a default value, update its corresponding mock assertion to include that parameter. Grep for `assert_awaited_once_with` and `assert_called_once_with` whenever a method signature changes.
- Confidence: high

**[2026-07-03] — E2a BE-11: ExplainRequest extra="forbid" requires field declared on model, not handler-body only**
- Observation: `ExplainRequest` has `model_config = ConfigDict(extra="forbid")`. Adding `scope_filter` only as a handler-body check (without declaring `scope_filter: str | None = None` on the Pydantic model) causes Pydantic to reject any request that includes `scope_filter` with a 422 "Extra inputs are not permitted" before the handler runs at all.
- Action: Before adding any handler-body validation for a new field on `ExplainRequest`, always declare the field on the Pydantic model first. Pattern: declare field with `None` default on the model → validate syntax/semantics in handler body. Applies to any model with `extra="forbid"`.
- Confidence: high

**[2026-07-03] — E2a BE-9: defensive empty-string guard in predicate builders prevents nonsensical SQL**
- Observation: `build_where(filters, scope_filter="")` with only the `is not None` check would produce `list_has(scopes, '')` — a valid SQL expression but semantically meaningless. Upstream validation (BE-11) rejects empty strings, but the store layer should not depend on caller discipline for SQL safety.
- Action: In any predicate builder that accepts user-supplied string values, add a truthiness guard (`and value` or `if value:`) in addition to `is not None`. An empty string is not None but produces a nonsensical predicate for all list-search operations.
- Confidence: high

**[2026-06-29] — E1a T-3: positive-path MCP graph_mode test must assert graph_expansion_applied=True, not just isinstance(bool)**
- Observation: `test_e2e_mcp_search_graph_mode` initially only asserted `isinstance(parsed["graph_expansion_applied"], bool)`. This passes even if graph expansion is permanently broken (always returns False). S8 requires proving expansion actually ran.
- Action: Any MCP test for a feature that should produce `expansion_applied=True` must assert the value, not just the type. Set up conditions that guarantee expansion (entity in graph matches query token) and assert the exact bool value.
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

**[2026-07-03] — E2a T-3: exact-match vs wildcard test distinction requires a sub-scope in the exact-match corpus**
- Observation: S8 (exact `scope_filter="user:alice"`) and S9 (wildcard `scope_filter="user:alice*"`) share almost all assertions. Without a chunk scoped `"user:alice:thread-1"` (sub-scope) in the test corpus, the two tests are indistinguishable: exact match and wildcard both include "user:alice" and exclude "user:bob". The sub-scope chunk is what separates them — S8 must exclude it, S9 must include it.
- Action: When writing exact-vs-wildcard test pairs for scope_filter, always include a sub-scope chunk (e.g. `"user:alice:thread-1"`) in the corpus and add separate inclusion/exclusion assertions for it. Without it, S8 and S9 are the same test.
- Confidence: high

**[2026-07-03] — E2a T-4: `benchmark` marker is NOT excluded from the default pytest suite**
- Observation: The docstring in `test_e2a_t4_scope_wildcard_benchmark.py` (and `test_search_filtered_benchmark.py`) falsely claims the `benchmark` marker is excluded via `-m 'not benchmark'` in addopts. The actual addopts excludes `live_benchmark`, not `benchmark`. Tests marked `@pytest.mark.benchmark` run on every `uv run pytest` invocation.
- Action: Never write "Auto-excluded from the default suite" for a `benchmark`-marked test. Only `live_benchmark` is excluded. Use the docstring pattern: "Note: the `benchmark` marker is NOT excluded from the default suite." Carry `xdist_group("benchmark")` for CPU contention serialization.
- Confidence: high

**[2026-07-03] — E2a T-4: use `time.perf_counter()` not `time.process_time()` for sub-millisecond benchmarks**
- Observation: `time.process_time()` measures CPU time with coarse resolution; many sub-ms filter operations register as 0.000ms. For latency benchmarks (especially pure Python operations taking microseconds), `perf_counter()` gives wall-clock nanosecond resolution.
- Action: Use `time.perf_counter()` for all sub-ms timing in benchmark tests. Reserve `process_time()` for operations with significant I/O (like `hybrid_search_with_trace`) where the LanceDB wall-clock difference from CPU time doesn't matter.
- Confidence: high

**Exit code assertions need a unique string to pin the code path**
- Action: When two code paths share an exit code, add `assert "specific string" in result.stderr`. Exit code alone is insufficient.

**Assert directly on result.stderr, not combined stdout+stderr**
- Action: Never concatenate `result.output + result.stderr`. Assert on `result.stderr` directly. In Click 8.3.3, default `CliRunner()` already separates streams — never pass `mix_stderr=False`.

**Defensive `or 0` on optional fields needs two tests: absent key AND null value**
- Action: `get(key, default) or fallback` has two branches. Add one test for absent key and one for null value — they are distinct code paths.

**[2026-07-02] — E1c T-6: single-entity corpus triggers CommunityBuilder early-exit without leidenalg**
- Observation: `leidenalg` (the optional Leiden community detection library) is absent from the dev environment. CommunityBuilder.build() takes an early-exit at `len(nodes) < 2` that returns a single community without running Leiden. Designing the corpus with exactly one unique entity ("PaymentService") across all docs guarantees this path — tests exercise the full ingest → extraction → community write → explain → provenance chain without the optional dependency.
- Action: For e2e community tests in environments where leidenalg is not installed, design the corpus so all docs share exactly one unique entity. This triggers the `len(nodes) < 2` early-exit in CommunityBuilder.build() and yields a valid community that the explain endpoint can traverse.
- Confidence: high

**[2026-07-02] — E1c T-6: asyncio.run() is safe from the main test thread inside `with TestClient(app):`**
- Observation: Starlette's TestClient runs the ASGI app in a background thread with its own event loop. The main test thread has no running event loop. Therefore `asyncio.run(coroutine)` is safe from the main thread even while the TestClient context is open. This allows opening fresh GraphStore + SearchStore connections to the same db_path to run CommunityBuilder.build() synchronously between ingest and the HTTP explain call.
- Action: Use `asyncio.run(_build_communities_async(cfg.db_path, col, cfg))` from the main test thread to seed community data between ingest and explain assertions. Open fresh store connections pointing to the same tmp_path db; GraphStore opens tables per-call so communities written by the fresh connections are immediately visible to the app's pipeline.
- Confidence: high

**[2026-07-02] — E1c T-6: chunk_id is always set in community-mode TraversalStep**
- Observation: In `pipeline.py` (line ~1945), community-mode TraversalStep construction always includes `chunk_id=candidate.chunk_id`. This is not optional. Tests must assert `step.get("chunk_id")` as truthy — if it's absent, it signals a regression in community candidate selection.
- Action: Any community-mode provenance test must assert `chunk_id` (truthy), not just `community_id`. Missing chunk_id is a real regression, not a quirk.
- Confidence: high

**Place assertions inside `with patch(...)` when using synchronous TestClient**
- Action: Always put HTTP call AND assertions inside the `with patch(...)` block. Outside is fragile to async refactors.

**[2026-06-29] — E1b BE-9: module-level constant over closure-local for MCP validation tuples**
- Observation: `_VALID_GRAPH_MODES` was initially declared inside the `search` tool closure. The iterative review flagged this as Moderate — closures with inline validation tuples are invisible to tests that import the module and check validation logic independently, and they can't be referenced by siblings (e.g., `search_with_context` docstring or a future validation helper).
- Action: Hoist any MCP tool validation tuple (like `_VALID_GRAPH_MODES`) to module level immediately. Module-level constants are testable, greppable, and shared across tools without cross-closure coupling.
- Confidence: high

**[2026-06-29] — E1b BE-9: MCP test using OR-fallback pipeline call assertions gives false positives**
- Observation: `assert pipeline.search.call_args.kwargs["graph_mode"] == "global" or "global" in str(call_kwargs)` — the `str(call_kwargs)` branch passes even if `graph_mode` was not forwarded (e.g., the whole kwargs dict contains "global" in some other field). DA review flagged this as Moderate.
- Action: Never use `or str(something)` as an assertion fallback for call argument verification. Always use `assert pipeline.search.call_args.kwargs["graph_mode"] == expected_value` — single strict assertion, no fallback.
- Confidence: high

**[2026-06-29] — E1b BE-9: error message update requires sibling E1a test updates — always grep for assertion strings**
- Observation: When BE-9 changed the `search_with_context` error message from "deferred to E1c" to "not supported; use the search tool instead", two tests in `test_e1a_fe3_mcp_search_graph_mode.py` silently broke: one asserted `"deferred" in ...` and another asserted `local` returns `invalid_graph_mode`. Neither test was mentioned in the BE-9 task spec.
- Action: After changing any error message string in `mcp.py`, run `grep -r "deferred\|E1c\|the old string"` across `tests/` to find sibling test files that assert on the old text. Update them in the same commit. Never leave the test suite in a mixed state across MCP tool generations.
- Confidence: high

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

**[2026-07-02] — E1c T-1: single-file ingest with chunk_size=512 produces only 1 retrieved result even with top_k=1**
- Observation: A document repeated 6× (~780 chars) with chunk_size=512 produces 2 chunks, but the vector+FTS search with a stub embedder returns at most 1 candidate (near_misses=[]). The `assert near_misses` guard fails even though the doc theoretically has 2 chunks.
- Action: To guarantee near_misses is non-empty in an explain e2e test, ingest TWO separate files (each produces its own chunk), then use top_k=1. With 2 distinct docs in the index, the second doc's chunk reliably lands in near_misses. Repeating a single doc is not sufficient due to stub embedder score symmetry.
- Confidence: high

**[2026-07-02] — E1c T-1: _REQUIRED_TOP_LEVEL_FIELDS coverage illusion in backward-compat tests**
- Observation: A backward-compatibility smoke test that hand-curates a subset of response fields can silently miss regressions on excluded fields. All four reviewers flagged the initial 9-field set as Major; the full ExplainResponse has 16 fields (15 always-present + stage_timings_ms which is conditionally popped).
- Action: When writing a backward-compatibility test for a Pydantic response model, use `ExplainResponse.model_fields.keys()` to enumerate the complete field set, then explicitly document which fields are intentionally excluded and why (e.g., `stage_timings_ms` is popped from the dict when None). Hand-curating a partial set without documentation creates a coverage illusion.
- Confidence: high

**[2026-07-02] — E1c BE-5: guard ordering creates shadow test problem for S14**
- Observation: When two 422 guards are ordered sequentially (graph_not_enabled before graph_mode_with_collections), a test that sends graph_mode+collections with graph disabled will hit the first guard and never reach the second. The S14 test was a false positive until graph_enabled=True was added.
- Action: When testing a guard that comes AFTER another guard in the same handler, ensure the precondition for the earlier guard is satisfied (here: graph.enabled=True) so only the target guard fires. Assert the exact error message to confirm which guard fired.
- Confidence: high

**[2026-07-02] — E1c BE-5: source-inspection tests disguise unreachable handlers as covered**
- Observation: `inspect.getsource()` checks pass whether a handler is correct, wrong, or unreachable. An abandoned HTTP-test attempt that falls back to source inspection creates a false-positive that survives all future test runs.
- Action: Never use inspect.getsource() as a proxy for behavior tests. If a handler is currently unreachable via the public API (e.g., blocked by a prior guard), send collections WITHOUT the blocking param and mock the pipeline to raise the error. This reaches the handler via a valid (non-blocked) path.
- Confidence: high

**[2026-07-02] — E1c BE-5: new route guards break existing tests that send now-guarded params**
- Observation: Adding a pre-pipeline guard (graph_not_enabled) to a route breaks existing tests that send graph_mode with the default config (graph disabled). Those tests rely on getting through to a mocked pipeline.explain and now get 422 before reaching it.
- Action: When adding a guard to a route, check existing tests in the same route's test files for requests that use the now-guarded param. Update those tests to satisfy the guard's precondition (e.g., graph_enabled=True + spaCy stub).
- Confidence: high

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

**[2026-06-30] — E1b T-4: gated eval tests must call assert_thresholds(report) before targeted per-metric assertions**
- Observation: Two per-metric gated eval tests (`test_eval_gate_graph_local_mrr`, `test_eval_gate_graph_global_mrr`) passed `baseline_path=BASELINE_JSON` to `run_eval_suite()` but never called `assert_thresholds(report)`. This bypassed the staleness hash checks, calibration-only baseline rejection, and floor-drop waiver policy that `assert_thresholds` enforces. The tests gave false confidence — they would pass even when `assert_thresholds` would fail due to a stale baseline. DA and Brooks-Lint caught this as a Major issue in Cycle 1.
- Action: Any gated eval test that passes `baseline_path` to `run_eval_suite()` MUST call `assert_thresholds(report)` immediately after, before any targeted per-metric assertions. The per-metric check provides a clearer failure message; `assert_thresholds` enforces the full gate contract including staleness.
- Confidence: high

**[2026-06-30] — E1b T-4: _QUALITY_METRIC_FIELDS in test_eval_suite.py must be updated when new metrics are added**
- Observation: `_QUALITY_METRIC_FIELDS` in `tests/eval/test_eval_suite.py` (used by `test_eval_suite_is_deterministic_except_latency`) only had `graph_mrr`, not `graph_local_mrr` or `graph_global_mrr`. Adding new per-mode graph metrics without updating this tuple leaves the determinism test blind to regressions in those metrics.
- Action: After adding any new metric to `EvalMetrics`, immediately add the metric name to `_QUALITY_METRIC_FIELDS` in `test_eval_suite.py` so the determinism check covers it.
- Confidence: high

**[2026-06-28] — plan-maker-for-team: E1B GraphRAG Leiden + Local/Global Modes**
- Observation: When E1a is a hard prerequisite but not yet implemented, the team plan must explicitly state it as a prerequisite (not just allude to it), name the exact artefacts E1b assumes exist (GraphConfig, graph tables, entity resolver, graph_mode=naive route), and add an open question to confirm the entity resolver symbol before BE-7 starts.
- Action: For any feature with a hard prerequisite feature that is itself in-progress: name the prerequisite in a bold "Prerequisite:" block at the top; list each assumed artefact by symbol name; add a Q# asking to confirm resolver symbol once E1a lands.
- Confidence: high

**[2026-06-28] — plan-maker-for-team: E1B TypeSpec union vs enum for string literals**
- Observation: TypeSpec `enum GraphMode { naive: "naive", local: "local", global: "global" }` compiles, but `union GraphMode { naive: "naive", local: "local", global: "global" }` also compiles and generates a cleaner oneOf in OpenAPI 3. Use `union` for string literal sets in TypeSpec HTTP contracts.
- Action: In TypeSpec HTTP service contracts, use `union` (not `enum`) for string literal discriminators — e.g. `union GraphMode { naive: "naive", local: "local", global: "global" }`. Both compile; union produces cleaner OpenAPI output.
- Confidence: high

**Pre-seeding JobStore before `make_real_app` via the same file path**
- Action: Create `JobStore(path=tmp_path / "jobs.json")` before entering `make_real_app`, seed it — `make_real_app` reads the same file on init via `_load()`. No need to expose the store.

**`pytest.mark.integration` as bare expression is dead code**
- Action: Use `@pytest.mark.integration` as a decorator. A bare expression statement inside a function body does nothing. Verify with `uv run pytest -m integration <file> -n0 -v --no-cov`.

**Hint-line count assertions need full surrounding phrase, not just a digit**
- Action: Never assert `str(N) in result.output`. Assert the full specific substring (e.g., `"2 revoked key(s) hidden" in result.output`).

**[2026-06-28] — E1a plan-maker-for-team: TypeSpec HTTP seams can fall back to core-construct when npm install is blocked**
- Observation: `tsp compile --no-emit` succeeded for all 6 contracts, but `npm install @typespec/openapi3` was blocked by the auto-mode classifier. All HTTP/API seams were authored as core-construct `.tsp` files (no HTTP decorators, no openapi.yaml emitted). The plan noted the fallback explicitly; no value was lost for the planning purpose.
- Action: When npm install is blocked for TypeSpec emitters, write core-construct `.tsp` for all seam types and note the fallback in the plan's "How to read this file" block. Do not retry npm install; the fallback is sufficient for contract agreement purposes.
- Confidence: high

**[2026-06-28] — E1a plan-maker-for-team: context compaction mid-session does not lose investigation findings if summary is accurate**
- Observation: Context compacted between TypeSpec validation and plan file writing. All 6 subagent findings, all 6 `.tsp` files, and the full task/scenario/contract design were preserved in the summary. Plan file was written cleanly from the compacted context.
- Action: For plan-maker sessions that spawn 6 subagents + write multiple contract files, compaction mid-way is normal and safe. Trust the summary for design decisions; re-read `.tsp` files only if specific content is needed.
- Confidence: high

**[2026-06-29] — E1a BE-9: always read all sections of a large file before coding — the implementation may already be there**
- Observation: runner.py (1410 lines) and backends.py (168 lines) had all the graph_mrr code already in place (`StubGraphExpander`, `EVAL_GRAPH_ENTITY_MAP`, `_execute_graph_retrieval_query`, `graph_mrr=graph_mrr` in EvalMetrics). My initial read stopped at line 116 of backends.py (only 116 of 168 lines) and missed the entire StubGraphExpander class. The test file passed on first run in green.
- Action: When reading a key file for a task, always check `wc -l` first, then read in pages if needed. Never assume a file ends where your read window ends.
- Confidence: high

**[2026-06-29] — E1a BE-9: adding eval corpus documents shifts routing_mrr scores via centroid change**
- Observation: Adding 2 graph documents to documents.jsonl + corpus/ caused `routing_mrr_centroid/hybrid` to drop from 0.75 to 0.7361 (delta -0.0139). The graph queries were properly excluded from `retrieval_traces` (recall/ndcg unchanged), but the corpus centroid shift affected the routing strategy evaluation of non-graph collections. The fix is to update the thresholds.toml floors to the new baseline and regenerate baseline.json twice (once after corpus change, once after thresholds change).
- Action: After adding any new documents to the eval corpus, always check whether routing_mrr values in the baseline changed. If the delta is within max_floor_drop_without_waiver (0.05), update the floors and regenerate the baseline again.
- Confidence: high

**[2026-06-29] — E1a BE-9: graph collection must NOT be added to routing/collections.jsonl**
- Observation: Adding `{"name": "graph", ...}` to routing/collections.jsonl introduced a new routing centroid that shifted routing MRR scores. Graph-mode queries are retrieval-scope only (no routing queries), so the graph collection has no business being in the routing manifest.
- Action: Only add a collection to routing/collections.jsonl if there are routing-scope queries targeting it. Retrieval-only collections do not belong in the routing manifest.
- Confidence: high

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

**[2026-06-30] — E1b T-5 close-out: xdist stub-install without immediate restore leaks into all workers**
- Observation: `test_e1b_be9_mcp_search_graph_mode.py` installed `_StubFastMCP` into `sys.modules["fastmcp"]` at module level but had no module-level restore. xdist with `--dist=loadgroup` imports ALL test files in every worker during collection. Workers that would never run the BE-9 tests still had `FastMCP = _StubFastMCP` left in their process-global `sys.modules`, causing 60+ MCP test failures (`AttributeError: '_StubFastMCP' object has no attribute 'http_app'`). The fix: immediately restore the real `FastMCP` (and pop `archon_search.server.mcp` from `sys.modules`) right after the module-level stub installation. The module-scoped fixture then reinstalls the stub only when the module's own tests actually execute.
- Action: Any test file that installs a stub into `sys.modules` at module level MUST immediately restore the real class at module level too. Fixture teardown is not sufficient — fixtures only run when a test actually executes, not during xdist collection-phase imports.
- Confidence: high

**[2026-06-30] — E1b T-5 close-out: CLAUDE.md doc updates must verify method names against actual source**
- Observation: Initial CLAUDE.md updates for E1b contained 4 fabricated `GraphStore` method names (`ensure_community_table`, `get_communities`, `community_count`, `last_community_built_at`) and a wrong `CommunityBuilder` method signature (`build_communities(collection) -> int` instead of `build(collection) -> list[Community]`). The iterative-review architecture agent caught all 5 discrepancies by grep-verifying against source. Wrong method names in CLAUDE.md are downstream correctness hazards since it is loaded into every agent's context.
- Action: After writing any CLAUDE.md doc update that names specific methods, always grep the actual source file (`grep -n "def <name>"`) to verify each method exists with the stated signature before finishing the session.
- Confidence: high

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

**[2026-06-28] — E0e T-3 (close-out: REST-only feature in "equivalent surfaces" manual)**
- Observation: The user manual declared "REST and MCP are equivalent surfaces" on line 4, then introduced `applied_filters` (REST-only, MCP deferred) in the same doc. Iterative review C1-I-1 (Major) caught the contradiction. The fix was a single bullet "REST only — MCP `search` tool does not include `applied_filters`" in the subsection, plus a matching E0e note in the 600 doc's MCP search return column following the existing RAG Fusion narrower-schema pattern.
- Action: Whenever a close-out adds a REST-only response field to a user manual that declares REST/MCP parity, always add an explicit "REST only" note in the new field's docs AND in the 600 doc's MCP tool return column. The "equivalent surfaces" heuristic breaks for deferred MCP features.
- Confidence: high

**[2026-06-28] — E0e T-3 (close-out: pinning test already existed)**
- Observation: C1-T-1 (Moderate) recommended adding a test pinning `applied_filters` absence from `McpSearchResponse`. On investigation, `test_mcp_search_response_fields` in `tests/test_mcp_schemas.py` already asserts the exact set of 6 field names and would fail if `applied_filters` were added. No new test needed.
- Action: Before adding a pinning test for a schema field's absence, always grep `tests/` for the schema class name — an exact-field-set test is likely already in `test_mcp_schemas.py` or `test_routes_search.py`.
- Confidence: high

**[2026-06-28] — plan-maker-for-team: E1c Graph-Path Provenance in /explain**
- Observation: `ScoredSearchCandidate` lives in `_diagnostics.py` (not `_types.py`); `ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss` all live inside `routes_explain.py` (not `schemas.py`); `ExplainPipelineResult` is a dataclass defined at the top of `pipeline.py`. The MCP `explain` tool exists in `mcp.py` and must be updated alongside the REST route. `from_candidate()` (line 98 in routes_explain.py) is the exact conversion point where `graph_provenance` must be threaded from candidate to response.
- Action: For any explain-layer plan, read `routes_explain.py` for the schema definitions — do not assume they are in `schemas.py`. Always check `mcp.py` for a matching MCP tool when extending the REST explain endpoint.
- Confidence: high

**[2026-06-28] — plan-maker-for-team: E1c — api-contracts/ already had node_modules**
- Observation: The `api-contracts/` subfolder from prior E0/E1b sessions already had `node_modules` installed and the `@typespec/openapi3` emitter available. The E1c HTTP seam contract compiled and emitted `openapi.yaml` without any new npm install step.
- Action: Before attempting npm install for TypeSpec OpenAPI emitter, check whether `api-contracts/node_modules/` already exists. If it does, compile directly with `tsp compile ... --emit @typespec/openapi3`.
- Confidence: high

**[2026-06-29] — E1a K1 implement-next: TypeSpec contract drift between extractor and store seams**
- Observation: `e1a-graphextractor-contract.tsp` defined `EntityType` and `RelationshipType` enums and used them, but `e1a-graphstore-contract.tsp` used plain `string` for the same fields. The contracts had drifted silently between sessions. Iterative review caught it as Major in Cycle 1.
- Action: When two TypeSpec files define the same domain model (e.g., `GraphNode` appears in both extractor and store contracts), add a note in the first-created file naming the other as "must stay in sync." Consider which file is authoritative and note it. Always diff corresponding model fields across all sibling contracts when reviewing a contract change.
- Confidence: high

**[2026-06-29] — E1a K1 implement-next: K1 is a documentation task — TDD cycle is skipped, but sanity-test still runs**
- Observation: K1 produces only planning documents and TypeSpec contracts — no Python code. The implement-next Step 2 explicitly says to skip TDD for doc-only tasks. However, Step 4 (run tests) still applies as a sanity check that no code was broken. Running a small subset (`tests/test_config_defaults.py tests/test_no_fstring_sql.py`) in under 1 second confirmed no breakage.
- Action: For doc-only tasks, run a minimal smoke test (2-3 test files) rather than the full suite to satisfy Step 4 efficiently. The full suite is not needed when no Python code was touched.
- Confidence: high

**[2026-06-29] — E1a BE-2 implement-next: both ID functions must normalize ALL string inputs**
- Observation: `make_stable_entity_id` was written normalizing only `entity_name` (strip+lower), leaving `entity_type` raw. Similarly, `make_stable_edge_id` did not normalize `relationship_type`. Iterative review caught both as Major/Moderate: inconsistent normalization means the same semantic entity/edge produces different dedup keys depending on caller casing.
- Action: For any hash-based stable ID function that accepts multiple string parameters, apply `.strip().lower()` to every semantic parameter (not to SHA-256 hex digests which are already canonical). Symmetric normalization prevents split-entity bugs.
- Confidence: high

**[2026-06-29] — E1a BE-2 implement-next: test strings must match enum vocabulary**
- Observation: Initial tests for `make_stable_edge_id` used uppercase `"RELATED_TO"` and `"USES"` — strings not present in `RelationshipType` enum (whose values are lowercase). Tests passed but exercised a path no real caller follows, hiding the normalization gap.
- Action: In hash-function tests, always drive test strings from the actual enum `.value` (e.g. `RelationshipType.related_to.value`), not invented uppercase variants. This ensures the tests catch real normalization failures rather than masking them.
- Confidence: high

**[2026-06-29] — E1a BE-2 implement-next: contract comments must be updated when implementation changes formula**
- Observation: After fixing `make_stable_entity_id` to normalize `entity_type`, the TypeSpec contract files (`e1a-graphextractor-contract.tsp`, `e1a-graphstore-contract.tsp`) and team plan (line 161 + BE-2 task description) still showed the old formula `{entity_type}:{...}`. Cycle 2 review flagged this as Major contract drift.
- Action: Whenever the hash formula in an entities module changes (even defensively), grep all `.tsp`, `CLAUDE.md`, and plan files for the old formula string and update them in the same commit. Contracts are not just docs — future implementers read them first.
- Confidence: high

**[2026-06-29] — E1a BE-3 GraphStore: AsyncMock vs MagicMock for lancedb table methods**
- Observation: In unit tests mocking lancedb, using `AsyncMock()` for the table object causes `table.query()` and `table.merge_insert()` to return coroutines instead of synchronous builders. This breaks chains like `table.merge_insert("id").when_matched_update_all()` — the coroutine has no `.when_matched_update_all` attribute.
- Action: Mock lancedb table objects as `MagicMock()` (not `AsyncMock`), and set `mock_table.open_table = AsyncMock(return_value=mock_table)` on the db. Only leaf async calls (`execute`, `to_arrow`, `to_list`) need `AsyncMock`.
- Confidence: high

**[2026-06-29] — E1a BE-5 pipeline graph hook: GraphNode/GraphEdge require enum instances, not strings**
- Observation: `GraphNode.entity_type` is typed `EntityType` (enum) and `GraphEdge.relationship_type` is typed `RelationshipType` (enum). `GraphStore.write_graph` calls `.value` on these fields. Tests that pass `EntityType.concept.value` (a plain string `"concept"`) cause `AttributeError: 'str' object has no attribute 'value'`.
- Action: In tests and real callers, always pass enum instances (`EntityType.concept`, `RelationshipType.related_to`) to `GraphNode`/`GraphEdge`. Use `.value` only when computing stable IDs via `make_stable_entity_id`/`make_stable_edge_id`.
- Confidence: high

**[2026-06-29] — E1a BE-5 pipeline graph hook: sys.modules[name] = None simulates absent package in tests**
- Observation: Setting `sys.modules["spacy"] = None` (not `del`) causes `import spacy` to raise `ImportError` inside the code under test. However, the check in `_check_graph_deps` must explicitly handle the `None` sentinel — `import spacy` on a `None` module actually raises `ImportError` from Python's machinery automatically.
- Action: To simulate an absent optional package in a unit test, use `sys.modules["pkg"] = None` and restore the original value in a `finally` block. The code under test will receive `ImportError` on `import pkg`.
- Confidence: high

**[2026-06-29] — E1a BE-9 eval graph_mrr: graph-mode traces must be excluded by query_id, not id()**
- Observation: First implementation used `{id(t) for t in graph_traces}` to exclude graph traces from regular `retrieval_traces`. Review flagged this as fragile — object identity breaks if traces are ever copied or serialized. `query_id`-based exclusion is self-describing and refactoring-safe.
- Action: When excluding a subset of traces from a metrics computation, use `{q.query_id for q in corpus.queries if q.graph_mode is not None}` and filter by `t.query_id not in graph_query_ids`. Never rely on Python object identity (`id()`) for logic that survives refactoring.
- Confidence: high

**[2026-06-29] — E1a BE-9 eval graph_mrr: promote private functions to public when they become cross-module contract**
- Observation: `StubGraphExpander` in `backends.py` needed to import `_build_expanded_text` and `_tokenize_and_generate_ngrams` from `graph_expander.py`. These were private (`_` prefix). Review flagged this as coupling to an undocumented contract that could break silently.
- Action: When a test stub or eval backend needs to reuse production helpers, promote those helpers to public (drop the underscore). If they are pure functions with stable behavior, they are already part of the module's contract — the `_` prefix is just incorrect labeling.
- Confidence: high

**[2026-06-29] — E1a BE-9 eval graph_mrr: silent skip in graph result mapping hides fixture bugs**
- Observation: First implementation had `continue` when `path_to_fixture.get(rel)` returned `None` in `_execute_graph_retrieval_query`. The regular `_map_result` raises `ValueError` for unmapped paths. Silent skip means fixture drift (renamed file, missing doc_id) produces a `graph_mrr` of 0.0 with no diagnostic.
- Action: In eval result mapping, always raise `ValueError` on unmapped paths. The eval corpus is fully controlled — there are no "extra docs" that should be silently dropped. Silent data loss in eval metrics is worse than an exception.
- Confidence: high

**[2026-06-29] — E1a T-6 close-out: background agent wrote camelCase Python method names into architecture docs**
- Observation: A background agent updating `CLAUDE.md` and `110_component_catalog_and_layer_breakdown.md` invented camelCase method names (`ensureGraphTables`, `writeGraph`, `getNeighbours`, etc.) that do not exist in the snake_case Python code. The project convention and the code are both snake_case throughout.
- Action: After any background agent updates architecture docs (CLAUDE.md, 110 catalog), grep for camelCase patterns (`[a-z][A-Z]`) against the actual method names in the referenced module. Background agents are reliable for prose but prone to hallucinating camelCase when the source is Python.
- Confidence: high

**[2026-06-29] — E1a T-6 close-out: iterative-review caught `expansion_used` definition missing new term**
- Observation: The existing `expansion_used` documentation in `600_api_reference` and `05_searching.md` was stale — it described `hyde_applied OR rag_fusion_applied` but the actual code (routes_search.py:233) now includes `or result.graph_expansion_applied` as a third term. The background agent that added the E1a docs did not update this existing definition.
- Action: When adding a new expansion type (graph, future modes), explicitly search for every existing "expansion_used" definition across all docs and update it. It is a cross-cutting concern that will appear in multiple files.
- Confidence: high

**[2026-06-29] — E1a T-6 close-out: duplicate BREAKING.md entries created by multi-session feature work**
- Observation: The E1a feature spanned multiple sessions. An earlier session added a BREAKING.md entry (BE-1 task). The T-6 close-out session added another entry for the same feature. Both ended up in the file simultaneously.
- Action: Before adding a BREAKING.md entry for a feature, always grep for the feature prefix (e.g., `grep "E1a" BREAKING.md`) to check whether an entry already exists. If one exists, merge rather than append.
- Confidence: high

**[2026-06-29] — E1b BE-3a: asyncio.get_event_loop().run_until_complete() breaks in xdist parallel workers**
- Observation: Tests using `asyncio.get_event_loop().run_until_complete()` pass when run in isolation (`-n0`) but fail under xdist parallel execution (the default) because xdist workers have no current event loop.
- Action: Always use `@pytest.mark.asyncio` with `async def` for async unit tests. Never use `asyncio.get_event_loop().run_until_complete()` in test bodies — it is broken in xdist and triggers a DeprecationWarning in Python 3.12+.
- Confidence: high

**[2026-06-29] — E1b BE-4: CliRunner merges stderr into output by default**
- Observation: Click's `CliRunner` uses `mix_stderr=True` by default, so `click.echo(..., err=True)` output appears in `result.output`, not a separate stderr stream. Tests asserting on error messages must inspect `result.output`.
- Action: Always assert error messages on `result.output` in CliRunner tests; assert `exit_code == 1` (exact) for specific failure scenarios, not `!= 0`.
- Confidence: high

**[2026-06-29] — E1b BE-4: Community.built_at is non-nullable — never pass None**
- Observation: `Community.built_at` is typed `datetime` (non-optional). Creating `Community(built_at=None)` raises a runtime error. The iterative-review caught this as C1-T-1 Critical.
- Action: Always use a concrete `datetime` value (e.g. `datetime(2026, 1, 1, tzinfo=timezone.utc)`) in test fixtures; grep the dataclass definition before constructing it to check nullability.
- Confidence: high

**[2026-06-29] — E1b BE-4: Place store connect() calls inside the try block**
- Observation: If `connect()` is placed before the `try` block, a connection failure bypasses the except handler. Both `GraphStore.disconnect()` and `SearchStore.disconnect()` guard with `if self._db is not None`, so calling them in `finally` is always safe — but errors during `connect()` should still be caught by the handler.
- Action: Always place `await store.connect()` inside the try block so connection failures are caught and the finally cleanup runs unconditionally.
- Confidence: high

**[2026-06-29] — E1b BE-6: TypeSpec contract required fields must be reflected in JSONResponse bodies**
- Observation: `GraphCommunitiesNotBuiltError` TypeSpec contract has `required: [code, message]` but the initial implementation only returned `{"code": "..."}` with no `message`. The fix is to bind the exception (`except Foo as exc`) and include `str(exc)` as `message`.
- Action: When a TypeSpec contract error body lists `required: [code, message]`, always verify both fields are present in the JSONResponse dict. Bind the exception variable to extract the message string.
- Confidence: high

**[2026-06-29] — E1b BE-7a: importing a private `_SYMBOL` from another module is a cross-boundary coupling violation**
- Observation: The plan directed `from graph_expander import _MAX_NGRAM_SIZE` but underscore-prefixed constants are private by Python convention. Any rename or removal in `graph_expander.py` silently breaks `pipeline.py`. The fix is a one-line default parameter: `def tokenize_and_generate_ngrams(query, max_n=_MAX_NGRAM_SIZE)` so callers pass no argument.
- Action: Never import private (`_`-prefixed) symbols from another module. Instead, expose the value via a default parameter, public constant, or accessor.
- Confidence: high

**[2026-06-29] — E1b BE-7a: new code added during review cycle itself needs test coverage**
- Observation: Cycle 1 added glob filtering on community candidates (`fnmatch.fnmatchcase` in Step 7). Cycle 2 correctly flagged this as Moderate: new behavioral code with zero tests. Added `test_local_mode_glob_filter_excludes_community_chunks` in Cycle 2.
- Action: Whenever a fix agent adds production code (not just test cleanup), the next review cycle must check for test coverage of that new code. Review agents can miss this if not explicitly prompted.
- Confidence: high

**[2026-06-29] — E1b BE-7a: log messages should name ALL filters that can trigger a fallback**
- Observation: After adding glob filter before ACL in Step 7, the warning message said "filtered by ACL" even though glob could be the sole cause. Cycle 2 review caught this as Minor.
- Action: When a fallback condition is triggered by multiple filters (glob + ACL), the log message must name all of them: "filtered by glob/ACL". Update log messages when adding new filter paths.
- Confidence: high

**[2026-06-29] — E1b BE-8: adding Pydantic response fields requires OpenAPI snapshot regeneration**
- Observation: Adding `community_count` and `last_built_at` to `StatusCollectionEntry` broke `tests/server/test_openapi_snapshot.py`. The Brooks-Lint reviewer caught this as a build-breaking Major issue (C1-B-1). The fix is `uv run --python 3.12 pytest tests/server/test_openapi_snapshot.py --update-openapi-snapshot`.
- Action: Whenever a Pydantic model used as a response model gains or loses fields, immediately regenerate the OpenAPI snapshot in the same task. This is already in `learnings.md` but this confirms it's caught by automated CI — do not skip.
- Confidence: high

**[2026-06-29] — E1b BE-8: multi-collection status tests must use side_effect keyed on collection name**
- Observation: Initial integration tests used a single-return `AsyncMock(return_value=...)` for `get_community_stats`. This meant the mock returned identical stats regardless of which collection was passed — making the per-collection mapping invisible to tests. Brooks-Lint raised this as C1-B-2 (Moderate).
- Action: Any test for per-collection behavior (status, stats, etc.) MUST use `side_effect` keyed on the argument (e.g., `lambda name: stats_by_col[name]`) so that incorrect argument passing fails the test. A constant return value is a coverage illusion.
- Confidence: high

**[2026-06-29] — E1b BE-8: health-path async DB calls need try/except guards**
- Observation: `get_community_stats` was called without exception guard in the `GET /status` collection loop. A LanceDB error in one collection would 500 the entire health endpoint. The existing `GraphStore.get_community_stats` only handles `FileNotFoundError`/`ValueError` on `open_table`, not downstream query errors.
- Action: Any auxiliary DB call inside `GET /status` or similar health endpoints MUST be wrapped in `try/except Exception` with WARNING log + fallback to defaults. Never let a non-critical sub-system read take down the primary monitoring surface.
- Confidence: high

**[2026-06-30] — E1c K1 implement-next: TypeSpec→OpenAPI can silently produce wrong error body shape**
- Observation: The E1c C1 TypeSpec defined `GraphNotEnabledError.detail` as `detail: string` (plain string, matching the `/search` live pattern). But the generated OpenAPI YAML rendered it as `detail: {code, message}` — a structured object that contradicts both the TypeSpec and the plan. The discrepancy was caught by iterative review (all 4 agents flagged it independently). The compiled artifact and its source were in direct conflict.
- Action: After generating any OpenAPI YAML from TypeSpec, always diff the error model shapes against: (1) the TypeSpec source, (2) the plan's error contract description, and (3) the existing sibling implementation (grep for the error string in `routes_*.py`). Never assume the TypeSpec compiler rendered error bodies correctly — plain-string `detail` vs structured-object `detail` is a common TypeSpec compilation pitfall.
- Confidence: high

**[2026-06-30] — E1c K1 implement-next: TypeSpec nullable fields without `?` generate required:true in OpenAPI**
- Observation: `graphMode: GraphMode | null` in TypeSpec means "must be present, may be null." TypeSpec generates this as `required: [graphMode]` in OpenAPI. The plan says `graph_mode` defaults to `None` (i.e., it can be omitted entirely). Without `?` (optional marker), the contract incorrectly requires the field. This also applied to `collection`, `collections`, `topK`, `rerank`, `hyde`, `ragFusion`.
- Action: In TypeSpec HTTP service contracts, always use `fieldName?: Type | null` (with `?`) for optional-with-null-default fields. Distinguish TypeSpec's two nullable semantics: `F?: Type | null` = may be absent or null; `F: Type | null` = must be present but can be null. For request body fields with defaults, always use `?`.
- Confidence: high

**[2026-07-02] — E1c BE-3 implement-next: tautological mock assertions — always test override, not default**
- Observation: Initial test for `rag_fusion_applied=False` had the mock returning `rag_fusion_applied=False` already. The assertion then proved nothing — the override in the graph_mode block was never exercised. Review caught this (C1-B-5).
- Action: When writing tests that verify a field is overridden to a specific value, always construct the mock to return the OPPOSITE value first. "Override to False" tests must start with the mock at `True`; "override to True" tests must start at `False`.
- Confidence: high

**[2026-07-02] — E1c BE-3 implement-next: integration test must exercise the NEW code path, not the null case**
- Observation: First integration test used `graph_mode=None`, which bypasses the new `if graph_mode is not None` block entirely. The test passed but provided zero coverage of the new branch. Review (C1-B-2) caught the gap — the integration test was a coverage illusion.
- Action: When the task adds a new code branch gated on a parameter, the integration test MUST pass a non-null value for that parameter. Tests with the default/null value are still useful for regression, but they do not prove the new branch works.
- Confidence: high

**[2026-07-02] — E1c T-3: mixed-results e2e requires corpus >> candidate_depth to force null-provenance candidates**
- Observation: S7 (mixed provenance) requires doc1 chunks to exceed candidate_depth so the expanded search cannot retrieve ALL of them, leaving some doc1 chunks only reachable by the standard hybrid path (null provenance). Initial corpus (~7 chunks) was far below candidate_depth=45 — all chunks got provenance and the null-provenance assertion always failed. Fix: 30 reps × chunk_size=16 → ~60 chunks >> candidate_depth=20 (pinned via top_k_retrieve=3 in TOML).
- Action: For any mixed-provenance test, set chunk_size small (16 GPT-2 tokens) and repetitions high (≥3× safety margin over candidate_depth). Pin top_k_retrieve in the TOML to make candidate_depth = max(top_k_retrieve × 3, 20) deterministic. Pin top_k in the request to the theoretical max (2 searches × 40-candidate union = 80) so near_misses stays empty and provenance checks stay in results[].
- Confidence: high

**[2026-07-02] — E1c T-3: ExplainNearMiss has no graph_provenance field — never check near_misses for provenance**
- Observation: `ExplainNearMiss` schema has `extra="forbid"` and does NOT include `graph_provenance`. Any `nm.get("graph_provenance")` on a near_miss always returns None vacuously, making "null provenance in near_misses" a meaningless assertion that can never fail. Review (C1-I-1) caught this as a tautological check.
- Action: In /explain e2e tests, ALWAYS check provenance only on `results[]` (which uses `ExplainResult` with `graph_provenance` field). Add `assert not near_misses` when the test requires all candidates to land in results[] — never mix results and near_misses into a single provenance pool.
- Confidence: high

**[2026-07-02] — E1c T-3: merge-path test (S7) needs TraversalStep structure validation — S2/S12 alone are insufficient**
- Observation: S2 and S12 exercise the all-graph path (all chunks fit in expanded search). S7 is the only test that exercises the merge path. If the merge path corrupts provenance structure (e.g., `graph_provenance` set to non-null garbage dict), S2/S12 would not catch it since they test a different code path. Review (C2-I-22) identified this as Major.
- Action: Any test that exercises a new code path through the pipeline MUST validate the full output structure on that path, not just the happy-path outcome. Even if S2 already validates TraversalStep in a simpler scenario, S7's merge path must validate the same structure independently.
- Confidence: high

**[2026-07-02] — E1c BE-8: unit tests that stub the method under test provide zero internal coverage**
- Observation: All three initial "unit" tests for BE-8 stubbed `_explain_community_candidates` with `AsyncMock`, so they only validated the call-path wiring in `explain()` but left all 7 branch paths inside the helper itself uncovered. Review caught this as Critical-coverage-gap.
- Action: When adding a new helper method, always write at least one set of tests that exercise the REAL method (not a stub). Stub-based tests are valid for the caller's orchestration logic, but cannot substitute for direct tests of the method being implemented. Add both.
- Confidence: high

**[2026-07-02] — E1c BE-8: global-mode chunk cap: inner-loop break + hard slice are both needed**
- Observation: The global community-retrieval loop breaks when `len(chunk_ids) >= max_cands` but the break fires AFTER the inner loop completes for one community, potentially adding up to one full community's worth of chunks beyond the limit. Review caught this as Major (C2-I-1). The fix: add `chunk_ids = chunk_ids[:max_cands]` after the loop (hard slice) in addition to the early-exit break.
- Action: For any loop that collects items and breaks when a threshold is reached, always apply a hard post-loop slice as the correctness guarantee. The break is only an optimization. This pattern mirrors the sibling `_search_graph_mode` global path (line 963: `chunk_ids[:max_cands]`).
- Confidence: high

**[2026-07-02] — E1c T-5 close-out: all 11 acceptance criteria verified by reading source code directly**
- Observation: All 11 E1c acceptance criteria were verifiable by reading `routes_explain.py`, `pipeline.py`, `_diagnostics.py`, `mcp.py`, and `tests/server/openapi_snapshot.json`. The test suite (6171 tests) passed with 93% coverage. Criterion 8 (TraversalStep with all-null optional fields → Pydantic validation error) is correctly enforced at the `TraversalStepResponse` Pydantic layer, not the `TraversalStep` dataclass layer — this is by design (docstring at `_diagnostics.py:21-22` explicitly notes this).
- Action: At close-out, verify each criterion by reading the actual source at the cited file path/line rather than relying on the plan description. The agent doing acceptance verification should search for the exact guard or field in the implementation file, not infer from test names.
- Confidence: high

**[2026-07-02] — E2a BE-3: store._row_to_meta and update_collection_meta must both handle new meta columns**
- Observation: Adding `default_ttl_seconds` to `CollectionMeta` and `_meta_schema()` is not enough. Both `_row_to_meta` (deserialisation) and `update_collection_meta` (write dict) also need updating, or the field silently reads/writes as None. Tests fail with unexpected None even though the DB column exists.
- Action: When adding a new column to `_meta_schema()`, always update `_row_to_meta` to read it and `update_collection_meta` to write it. Treat them as a matched pair — missing one is a silent data loss bug.
- Confidence: high

**[2026-07-02] — E2a BE-3: new `ingest_file` meta fetch breaks mock stores and assert_awaited_once_with assertions**
- Observation: Adding `get_collection_meta` calls to `ingest_file` (for TTL resolution) caused 7 failures in `tests/pipeline/test_pipeline_ingest.py`: 5 from mock stores (`CapturingStore`, `TrackingStore`, `StubStore`) that don't implement `get_collection_meta`, and 2 from tests asserting `assert_awaited_once_with` that now fail because `get_collection_meta` is called multiple times (once for TTL pre-resolution in `ingest_directory`, once per file in `ingest_file`, once at end of `ingest_directory` for description update).
- Action: When adding a new store method call to `ingest_file` or `ingest_directory`, add `async def get_collection_meta(self, *a, **kw): return None` to all inline mock stores in the affected test file. Change `assert_awaited_once_with` to `assert_any_call` in tests that only care about namespace forwarding, not exact call count.
- Confidence: high

**[2026-07-02] — E2a BE-3: test_old_schema_upsert_preserves_new_columns needs all migrations before update_collection_meta**
- Observation: `test_old_schema_upsert_preserves_new_columns` ran B5/C1/D3 migrations but not E2a's `migrate_default_ttl_seconds`. When `update_collection_meta` wrote `default_ttl_seconds` to the partially-migrated table, LanceDB raised `ValueError: Invalid input, field 'default_ttl_seconds' does not exist in table schema`.
- Action: When `update_collection_meta` writes a new column, any test that calls it on a manually-constructed table must also run the corresponding migration first. Add `await store.migrate_default_ttl_seconds()` to the migration chain in `test_old_schema_upsert_preserves_new_columns` whenever a new meta column is added via E2a-style migration.
- Confidence: high

**[2026-07-03] — E2a store.py: `_do_write_meta_unlocked` is the canonical write path, not just the constructors**
- Observation: `_do_write_meta_unlocked` builds a raw dict before calling `table.add()`. Even when all `CollectionMeta(...)` constructors correctly propagate `default_ttl_seconds`, the field is silently dropped if it is absent from that dict. The write path (`_do_write_meta_unlocked`) and the public write path (`update_collection_meta`) were diverged: the latter already included `"default_ttl_seconds"` in its dict, the former did not.
- Action: When adding a new column to `CollectionMeta`, verify it is included in BOTH write dicts: `update_collection_meta`'s `table.add([{...}])` AND `_do_write_meta_unlocked`'s `table.add([{...}])`. Missing it from `_do_write_meta_unlocked` silently wipes the field on every ingest-triggered centroid update. Also fix `pipeline.recompute_collection_meta`'s two `CollectionMeta(...)` constructors — they bypass the store's unlocked writer and call `update_collection_meta` directly, but still need `default_ttl_seconds=existing_meta.default_ttl_seconds if existing_meta else None`.
- Confidence: high

**[2026-07-03] — E2a T-1 e2e: asyncio.run() on server's store object is unsafe — prime meta via HTTP ingest instead**
- Observation: `asyncio.run(store.update_collection_meta(...))` from a synchronous test body (inside `make_real_app` TestClient context) acquires `asyncio.Lock` objects in a new, temporary event loop. After `asyncio.run()` exits, those locks remain in `store._collection_locks[col]` tied to the dead loop. When the TestClient's thread acquires the same lock in its own event loop, the behaviour is undefined (may silently corrupt state). The established safe alternative — documented in `test_migrate_dry_run_in_place_e2e.py` — is to use a fresh `lancedb.connect_async(cfg.db_path)` connection for any direct DB operations, which shares no locks with the server.
- Action: Never call async store methods via `asyncio.run()` inside a `make_real_app` TestClient context. To prime collection meta, do a real HTTP ingest (creates the meta row safely through the server stack). For schema inspection, open a fresh `lancedb.connect_async(cfg.db_path)` connection inside the `asyncio.run()` block — never pass `store._db` or call `store._require_connected()`.
- Confidence: high

**[2026-07-03] — E2a BE-6: adding a field to a dataclass shifts `Path.home()` line numbers in config.py**
- Observation: Adding `prune_expired_chunks: bool = True` to `MaintenanceConfig` shifted an existing `Path.home()` callsite in `config.py` from line 231 to 232. The `test_no_hardcoded_path_home.py` ratchet test stores `path:line:sha256` tuples and failed immediately. Fix is to update `tests/path_home_allowlist.txt` with the new line number.
- Action: Whenever adding a field to any dataclass in `config.py`, run `uv run pytest tests/test_no_hardcoded_path_home.py -n0` immediately after. If it fails, update the line number in `tests/path_home_allowlist.txt` — the sha256 stays the same (same code, just shifted). Also update `tests/test_config_defaults.py` snapshot if it exists for that dataclass.
- Confidence: high

**[2026-07-03] — E2a BE-6: `prune_expired_chunks` SELECT must be bounded by `_EXPIRING_SCAN_CEILING`**
- Observation: Initial implementation used an unbounded `table.query().where(pred).select(["doc_id"]).to_list()`. DA review flagged this as Moderate — on large collections with many expired rows this materializes all ids into memory. The sibling method `query_expiring_chunks` already caps at `_EXPIRING_SCAN_CEILING`. Fix: add `.limit(_EXPIRING_SCAN_CEILING)` to the SELECT and note in the docstring that returned doc_ids may be a subset (DELETE still removes ALL expired rows via predicate).
- Action: Any store SELECT that feeds logging data should cap at `_EXPIRING_SCAN_CEILING` if the result is never paginated. If the SELECT result is fully consumed into memory before the DELETE, the cap is required.
- Confidence: high

**[2026-07-03] — E2a BE-6: `prune_expired_chunks` must deduplicate returned doc_ids**
- Observation: `[r["doc_id"] for r in raw_rows]` returns one entry per chunk row. A document with 5 expired chunks produces `["abc", "abc", "abc", "abc", "abc"]`. The maintenance loop caller logs "pruned N documents" — duplicates inflate the count and produce misleading log output. Fix: `list(dict.fromkeys(r["doc_id"] for r in raw_rows))` to deduplicate while preserving insertion order.
- Action: Any prune/delete method that returns doc_ids for logging must deduplicate at the document level. Test with multiple chunks sharing the same `doc_id` to verify.
- Confidence: high

**[2026-07-03] — E2a T-2: `lancedb.connect_async().close()` is not a coroutine for local connections**
- Observation: `await db.close()` raises `TypeError: object NoneType can't be used in 'await' expression` because `AsyncConnection.close()` for a local (file-backed) LanceDB connection returns `None`, not a coroutine. C2-DA3 flagged this during review; the code attempted `await db.close()` in `_count_chunks_fresh` and `_get_doc_ids_fresh`. Fix: `db.close()` (no await).
- Action: When calling `db.close()` on a `lancedb.connect_async()` local connection, do NOT use `await`. The method is synchronous-returning for local backends. Only remote/cloud backends return an awaitable. Use `db.close()` in a `finally` block as a no-op cleanup for symmetry.
- Confidence: high

**[2026-07-03] — E2a T-2: fresh `SearchStore(db_path)` is safe inside `asyncio.run()` for seeding LanceDB in integration tests**
- Observation: The documented learnings.md pattern — "never call async store methods via asyncio.run() using the server's own store object" — does NOT prohibit using a fresh `SearchStore(db_path)` inside `asyncio.run()`. Opening a brand-new `SearchStore` instance in the main test thread is safe: it creates its own connection and asyncio locks tied to the temporary event loop. After `asyncio.run()` returns and calls `fresh_store.disconnect()`, that event loop and all its state are gone. The critical violation to avoid is reusing `client.app.state.search_store` — that object's connection is bound to the ASGI server's background event loop.
- Action: For seeding LanceDB data in integration tests: `asyncio.run(_seed_chunks(cfg.db_path, col, ...))` with a fresh `SearchStore(db_path)` created inside that async function is correct. Do not pass the server's store object across event-loop boundaries.
- Confidence: high

**[2026-07-02] — E1c T-5 close-out: UserManual graph_mode error table had two stale E1c placeholder entries**
- Observation: The UserManual `05_searching.md` error table had `graph_mode on POST /explain → 422 (extra field rejected)` (pre-E1c state) and `MCP search_with_context + graph_mode → Error dict (deferred to E1c)`. Both needed updating: the first because E1c now accepts graph_mode on /explain; the second because "deferred to E1c" is no longer accurate now that E1c has shipped.
- Action: At close-out, grep the UserManual for the feature tag (e.g., `grep "E1c\|deferred"`) and also explicitly check error tables for entries that describe the state BEFORE the feature was built. Update both the placeholder text and the now-shipped feature entries.
- Confidence: high

**[2026-07-03] — E2a T-5 close-out: all documentation was pre-updated by prior tasks**
- Observation: Every documentation target listed for T-5 (130, 160, 600 architecture docs; CLAUDE.md; BREAKING.md; toml.example; UserManual/12_ttl_and_scoping.md) was already fully updated by the prior T-1–T-4 implementation tasks. T-5 fact-checked all acceptance criteria (STORE_SCHEMA_VERSION=1, migrate_expires_at_and_scopes, prune_expired_chunks, scope_filter, ExpiringChunksResponse, expired_chunk_count, MCP tool updates, has_ttl_cols guard), regenerated the OpenAPI snapshot, and ran the full suite (6360 passed, 93% coverage).
- Action: At E2a-style feature close-outs, run the fact-checks first before planning documentation work — prior tasks often already completed the doc updates. The snapshot regeneration and full suite run are always needed regardless.
- Confidence: high

**[2026-07-03] — E2b brief revision: LanceDB merge_insert cannot increment counters — persist incidence, derive counts**
- Observation: The original E2b brief specced `chunk_count`/`weight` accumulation "via `merge_insert()` upsert". `merge_insert("id").when_matched_update_all()` (the only upsert in the codebase, `graph_store.py write_graph`) REPLACES matched rows — arithmetic increments are not expressible, and read-modify-write counters double-count on re-ingest because a document's prior contribution is unknown once summed. The fix: persist a `(entity_id, chunk_id, doc_id)` mentions incidence table with doc-scoped delete-then-add (chunk_ids are deterministic `{doc_id}-{idx:06d}`, pipeline.py:522), and derive all counts/weights at read time.
- Action: Never spec an accumulating counter column on a LanceDB merge_insert-upserted table. Persist the incidence rows (keyed so a document's contribution is deletable) and derive aggregates at read. Applies to any future per-entity/per-edge statistics.
- Confidence: high

**[2026-07-03] — Graph tables are write-only: no delete/TTL/maintenance path touches `_archon_graph_*`**
- Observation: `delete_document`, `delete_by_source_path`, `prune_expired_chunks`, and all four maintenance-loop policies operate only on chunk tables — graph nodes/edges/communities are never reconciled, so re-ingest/delete/TTL-expiry leave orphaned graph rows forever (verified store.py:2037-2420, jobs/maintenance_loop.py). Community `representative_chunk_ids` pointing at dead chunks are only skipped at read time via `get_chunks_by_ids`.
- Action: Any brief/plan touching graph inspection, metrics, or retrieval must state the staleness interaction explicitly and check `delete_document` + maintenance-loop for graph-table access first. Graph GC is roadmapped as E2d — do not silently assume graph rows track chunk lifecycle.
- Confidence: high

**[2026-07-03] — Eval graph gates prove only the fallback path — never cite them as community-retrieval evidence**
- Observation: `CommunityStoreStub` in `eval/backends.py` returns intentionally fake chunk IDs, so `get_chunks_by_ids` yields `[]` and local/global eval queries fall back to standard hybrid search. The gated `graph_local_mrr = graph_global_mrr = 1.0` floors in thresholds.toml are calibrated on that fallback (the calibration comment admits it); `graph_mrr` is report-only.
- Action: When claiming eval coverage for graph modes, check whether the store stub actually resolves chunk IDs. The current gates prove the non-raising fallback contract only — real community-retrieval quality gating is E2e.
- Confidence: high

**[2026-07-03] — CLAUDE.md E2a claim about GraphCollectionStats is wrong**
- Observation: CLAUDE.md states `GraphCollectionStats` gained `community_count` and `last_built_at` in E2a. Verified against `schemas.py:312-321`: `GraphCollectionStats` has only `collection, node_count, edge_count`; the two fields exist only on `StatusCollectionEntry` (`schemas.py:107-109`).
- Action: Do not trust the CLAUDE.md E2a bullet for `GraphCollectionStats` fields — grep `schemas.py` instead. Fix the CLAUDE.md line whenever an E2-series close-out next touches that section.
- Confidence: high

**[2026-07-03] — Roadmap mermaid chart labels drift from list numbering — re-sync when touching items**
- Observation: The "Remaining" quadrantChart in 03_world_class_roadmap.md still used pre-renumbering labels ("E7 GraphRAG", "E8 TTL+scoping+entity graph", "E1 Streaming results") that no longer matched the Phase E list (E1=GraphRAG, E3=streaming, E7=connectors, E8=admin UI). Cross-references inside G4/G16 ("requires E7 active") pointed at the old GraphRAG number too.
- Action: When adding or renaming roadmap items, always grep the file for the item family in BOTH mermaid charts and in other items' cross-references (`grep -n "E7\|E8" 03_world_class_roadmap.md`) and re-sync in the same edit session — the doc mandates chart/list sync.
- Confidence: high

**[2026-07-03] — E2b plan-maker: FastAPI literal-vs-parameterized route ordering is a planning-level concern**
- Observation: `GET /graph/cross-collection` and `GET /graph/{collection}` on the same prefix will cause FastAPI to match "cross-collection" as the `{collection}` path parameter if the literal route is declared second. This must be captured as an explicit implementation note (Q1) in the plan, not left as an implementation detail — the developer must know to declare the literal route first in `routes_graph.py`.
- Action: Any plan that adds both a fixed-path route (e.g. `/graph/cross-collection`) and a parameterized sibling (e.g. `/graph/{collection}`) under the same prefix must include a Q# or implementation note requiring literal-before-parameterized declaration order. This is a FastAPI registration constraint, not a runtime guard — it cannot be tested after the fact.
- Confidence: high

**[2026-07-03] — E2b plan-maker: always Read the actual file to confirm method existence before specifying "add X" tasks**
- Observation: The architecture investigation agent initially stated `get_all_nodes` and `get_all_edges` "do not exist in graph_store.py". Direct file read (`Read graph_store.py` with offset=600) confirmed both methods exist at lines 629–657. Specifying "add get_all_nodes" tasks based on agent reports without file verification would have wasted implementation time and confused the dev.
- Action: When an investigation agent says a method does not exist, always verify with `Read` (with offset into the relevant section) before writing tasks that add it. File read is authoritative over agent assertions about absence. This applies especially to methods in large files (>300 lines) where the agent's read window may not have reached the method's location.
- Confidence: high

**[2026-07-03] — Background research agents: idle notification ≠ delivered report; session-limited agents can self-recover inline**
- Observation: Named background agents (Agent tool, run_in_background) emitted idle notifications without their final reports reaching the main session; a SendMessage "deliver your report now, split into parts" retrieved full multi-part reports. Two agents whose own SUBAGENTS died to account session limits recovered by re-doing the research inline on their main thread. A respawned duplicate agent proceeded anyway after a stand-down message and delivered useful source-level verification — stand-downs are best-effort, budget accordingly.
- Action: After spawning background research agents: (1) on idle-without-report, SendMessage requesting delivery in parts rather than respawning immediately; (2) reserve respawn-with-identical-prompt for agents that confirm zero recoverable findings; (3) tell respawns "do NOT spawn subagents" when the failure was subagent session limits; (4) unauthenticated GitHub API calls across parallel agents exhaust the shared 60/hr IP quota — instruct agents to prefer raw.githubusercontent.com fetches and use the API sparingly.
- Confidence: high
