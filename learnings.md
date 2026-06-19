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

**2026-06-17 — Team plan generation (D3) — TypeSpec + role mapping**
- Observation: (1) `namespace` is a reserved keyword in TypeSpec — using it as a model field name fails to compile; rename to e.g. `jobNamespace`. Core-construct `.tsp` files (model/enum/interface, no `@typespec/http` import) compile standalone with `tsp compile <file> --no-emit`. (2) This repo has no GUI: the `/plan-maker-for-team` Frontend role is always N/A — Presentation (FastAPI routes, Pydantic schemas, Click CLI) is server-side Python owned by Backend.
- Action: For archon-search team plans, mark Frontend N/A and fold Presentation into Backend; when authoring TypeSpec contracts, avoid reserved keywords (`namespace`, `interface`, `model`, etc.) as field names and validate each file before referencing it.
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

## Open Questions
- (Nothing recorded yet)

**2026-06-15 — Feature brief writing (D4)**
- Observation: The `/feature-refinement` skill enters a deliberation loop when the problem space has many sub-options. It can stall without a firm directive to write the file.
- Action: When spawning feature-refinement for a well-understood technical brief, include explicit instruction: "Do not ask questions — write the brief now, put all open items in Open Questions." This bypasses the multi-round clarification loop.
- Confidence: high
