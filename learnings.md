# Learnings

## What Has Worked

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

**2026-06-21 — asyncio.Event.set() from test thread is consumed by the running event loop before the route handler checks it**
- Observation: In `test_trigger_while_busy_returns_202`, calling `maintenance_loop._trigger_event.set()` from the synchronous test thread caused the TestClient's background asyncio event loop to immediately wake up `_trigger_loop`, run `_run_one_pass`, and clear the event — all before the route handler ran. The test got `"triggered"` instead of `"already_triggered"` because by the time the route checked `is_set()`, the loop had already cleared it.
- Action: When testing an "event already set" branch in a route, replace the actual `asyncio.Event` with a `MagicMock` whose `is_set()` always returns `True`. Do not set the real event from the sync test thread when an async loop is consuming it in the background.
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
