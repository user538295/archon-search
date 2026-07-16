# Feature Brief: CLI Store Commands Start Instantly

## Problem
`archon-search collection list`, `collection info`, and `collection remove` are slow to start — taking 1–5 seconds on a warm machine and up to a minute on first install — because they load the full ML pipeline even though they only need to read the database.

## Goal
`collection list` and `collection info` respond in under 200ms on a warm machine. First-install experience no longer triggers a multi-second (or multi-minute) tokenizer download just to list collections. The Claude SDK is not imported on any CLI command that doesn't generate descriptions.

## Users & Context
Operators and developers running quick administrative commands — checking what collections exist, inspecting collection stats, removing a collection — without triggering an ingest. These commands should feel like database reads, not ML startup sequences.

## Core Flow
1. User runs `archon-search collection list` (or `info` or `remove`).
2. The CLI reads the config file.
3. The CLI opens a direct database connection — no ML pipeline, no tokenizer, no embedder.
4. Collection data is read and printed.
5. The command exits in under 200ms (warm).

## In Scope
- `collection list`: use `SearchStore` directly instead of `create_pipeline()`
- `collection info`: same
- `collection remove`: same (already only calls `store.drop_collection()`, but still pays the `create_pipeline` cost)
- `pipeline.py`: move `from archon_search.description_generator import ...` (line 25) inside the one function that calls it (`recompute_collection_meta`, around line 935), so the Claude SDK (`claude_agent_sdk`) is not imported on CLI startup

## Out of Scope
- `collection add`, `reindex`, `sync` — these legitimately need the pipeline (they embed documents)
- Lazy-initializing chunkers inside `SearchPipeline` — right long-term refactor but wider blast radius; deferred
- `config show` slowness — covered by a separate brief (bug-004-cli-startup-latency)

## Key Decisions
- **Direct `SearchStore` over a shared factory**: A small `_make_store(cfg)` helper (~10 lines) opens `SearchStore(cfg.db_path)` without constructing chunkers, embedder, or reranker. Store-only commands use this instead of `create_pipeline()`. This is the same pattern the existing `migrate` command uses for its internal store access.
- **Import locality over module-level convenience**: Moving the `description_generator` import inside `recompute_collection_meta` is a single-line relocation. The function is only called during ingest — no CLI command that skips ingest will pay this cost.

## Edge Cases & Constraints
- **`collection remove` still needs the embedder for centroid cleanup**: Verify whether `remove` calls any pipeline method that touches the embedder after `store.drop_collection()`. If it does, the `_make_store` path needs a fallback or the command stays on `create_pipeline`. (Needs code-path verification in planning.)
- **`lancedb` first-import cost (~900ms) is unavoidable**: The store itself triggers a lazy `import lancedb` on first use in a process. This is load-once and cannot be eliminated without shipping a precompiled binary. Document in the release notes: first-ever invocation on a machine will be slower.
- **`path_home_allowlist` ratchet test**: Moving the `description_generator` import does not change any `Path.home()` call site; the allowlist test is unaffected.
- **`test_config_defaults.py` and import-side-effect tests**: Moving the import inside a function means tests that previously relied on `description_generator` symbols being available at module load via `pipeline` will need to import them directly. Verify with a test run before merging.

## Open Questions
- Does `collection remove` call any embedder method after `drop_collection()` (e.g. centroid update, graph GC)? If yes, it cannot safely bypass `create_pipeline`. Check `archon_search/cli/collection.py:183–210` and the `pipeline.delete_collection` code path.
- The `_make_store(cfg)` helper needs `cfg.db_path`. Confirm `SearchConfig` exposes `db_path` directly or via `paths.get_data_dir()` (it does — `SearchStore(cfg.db_path)` is already used in integration tests).
- Should `_make_store` be in `archon_search/cli/_helpers.py` (already exists as shared CLI infrastructure) or inline in `collection.py`?

## Future Iterations
- Lazy-initialize `DocumentChunker` and `ASTChunker` inside `SearchPipeline.__init__` so all pipeline construction is fast regardless of caller — this would eliminate the chunker cost from `collection add` and `ingest` too.
- Cache the lancedb connection across CLI invocations (not feasible without a daemon, but worth noting for a future connected-CLI mode).

## References
- [[archon_search/cli/collection.py]] `[code-agent]` — `list_cmd` (line 45), `info` (line 211), `remove` (line 148) all call `create_pipeline(cfg)`
- [[archon_search/pipeline.py:25]] `[code-agent]` — top-level `from archon_search.description_generator import ...` triggers `claude_agent_sdk` import on every CLI command
- [[archon_search/pipeline.py:935]] `[code-agent]` — only call site for `generate_description` and `_should_regenerate`; safe to move import here
- [[archon_search/store.py:271]] `[code-agent]` — `SearchStore.__init__(db_path)` — direct construction, no ML deps
- [[archon_search/cli/collection.py:105]] `[code-agent]` — `collection add` already does a lazy `from archon_search.sync import ...` inside the function body; same pattern applies here

## Recommendation
Build this. The fix is small (one helper function, two import relocations, three call-site swaps) and the user-visible improvement is dramatic — a 5-second wait becomes under 200ms for the most common administrative commands. The lancedb cold-start cost (~900ms) is the remaining irreducible floor and should be documented, not fixed. Do this before any other CLI performance work; it's the highest ratio of impact to risk in the codebase right now.
