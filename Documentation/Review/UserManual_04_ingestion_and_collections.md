# Review: UserManual/04_ingestion_and_collections.md

## Summary

The doc is mostly accurate but has several material inaccuracies, the most important being the claim that `archon-search ingest` is incremental (it is not — only `sync` is state-aware). The CLI flag table for `ingest` also misrepresents which flags exist on the other subcommands (they all accept `--config`, not just `ingest`). The `collection info` output description ("Prints the `CollectionMeta`") is technically correct but vague — it is `str(meta)` (the dataclass `__repr__`), not a custom formatted display. The cited source path for the reindex-on-crash logic (`archon_search/server/mcp.py:_needs_install_trigger`) is the wrong layer — the actual sync-time crash recovery lives in `archon_search/sync.py` (the `_needs_install_trigger` helper only governs whether the server triggers an install/sync at startup, not whether a collection is reindexed mid-run).

## Inaccuracies (numbered)

1. **Line 12 — "Ingestion is incremental. Re-running `ingest`/`sync` only processes changed files."**
   Only `sync` is state-aware. `archon-search ingest` (`archon_search/cli/ingest.py:35`) calls `pipeline.ingest_directory` directly, which iterates and re-ingests every file under the path with no `IndexingStateStore` consultation and no mtime check (`archon_search/pipeline.py:194-255`). Re-running `ingest` re-processes everything.

2. **Lines 28, 30-34 — "Flags (`archon_search/cli/ingest.py`)" with three rows.**
   The `ingest` command's `--path` default is `~/.archon-search/history/sessions`, but the table omits the *behavior* documented in source: when `--path` is omitted, the CLI emits `No --path given, using default: <path>` to stdout before running (`cli/ingest.py:20-21`). Minor — worth mentioning since it surfaces in real output.

3. **Line 36 — Output: `Ingest complete: <ok> ingested, <errors> errors.`**
   Accurate for `ingest`, but the doc does not mention that `ingest` does not print a per-file progress trail (sync does, via the state store). Not strictly wrong; flagging as ambiguous below.

4. **Line 40 — "Honors the indexing state store so it skips work for already-indexed files."**
   Correct for `sync`. But the same paragraph (line 46) says "Use it manually after editing the collection lists in TOML without going through `collection add`." This is correct in spirit, but `sync` also resets stale `IN_PROGRESS` state to `PENDING` (crash recovery, `sync.py:321-344`) which is a material behavior the doc omits.

5. **Line 54 — "Prints one line per collection: `<name>  docs=<n>  chunks=<n>`. Returns 'No collections found.' if empty. (`archon_search/cli/collection.py:list_cmd`.)"**
   Accurate. Verified at `cli/collection.py:35-38`.

6. **Line 58 — "Adds the path to `[collections].collections` in the TOML (if not already present) and ingests it immediately."**
   Accurate, with a caveat the doc omits: if the config file doesn't exist, `collection add` creates a new TOML document (`cli/collection.py:66-77`). Not an inaccuracy, just incomplete.

7. **Line 64 — "Pinned collections must be added manually to `pinned_collections` in TOML — the CLI does not have a 'pin' flag."**
   Verified: no `--pin` option in `cli/collection.py:49-98`.

8. **Lines 74-78 — `--dry-run` / `--force` flags and mutual exclusion.**
   Verified at `cli/collection.py:101-110`.

9. **Line 80 — "If the path is in `pinned_collections` but **not** in `collections`, removal is rejected with a message instructing you to unpin first."**
   Verified at `cli/collection.py:124-130`. Path comparison is by resolved absolute path, not raw string — worth mentioning but not wrong.

10. **Line 88 — "Prints the `CollectionMeta` for one collection. Returns exit code 1 if the collection is unknown."**
    Verified. The actual print is `click.echo(str(meta))` (`cli/collection.py:185`), i.e. the dataclass `__repr__`. The doc's phrasing ("Prints the `CollectionMeta`") is technically true but a user might expect a pretty-printed view; the output is `CollectionMeta(name=..., description=..., centroid=[...], ...)`. Minor.

11. **Line 96-102 — Reindex steps and "must already exist in `pinned_collections` or `collections` in the TOML".**
    Verified. `cli/collection.py:212-219` iterates `cfg.pinned_collections + cfg.collections`, matches by `path_to_collection_name`, and exits 1 if not found. The three numbered steps (clear state, drop table best-effort, re-ingest with `force_regenerate_description=True`) all match `cli/collection.py:223-235`.

12. **Lines 108-112 — REST equivalents.**
    Verified against `server/routes_collections.py` and `server/routes_jobs.py`:
    - `POST /collections/` → 202 + JobResponse (`routes_collections.py:114`). Doc says "returns a 202 `IngestJob`" — the actual response model is `JobResponse`, not `IngestJob`. Minor terminology drift.
    - `DELETE /collections/{name}` → 404/409 (`routes_collections.py:171`). Doc says "409 if pinned-only, 404 if unknown" — verified at lines 180, 200.
    - `GET /collections/` / `GET /collections/{name}` — verified at lines 74, 234.
    - `POST /collections/{name}/reindex` — verified at line 299, status 202.
    - `POST /ingest`, `GET /jobs/{id}`, `DELETE /jobs/{id}` — verified at `routes_jobs.py:91, 108, 119`.

13. **Line 118 — "The watcher does not delete collections when their source directory is deleted; use `collection remove` for that."**
    Verified: `watcher.py` only triggers `_async_callback(collection_name)` (a sync) on filesystem events; there is no deletion path.

14. **Lines 122-126 — "Reindex triggers". The third bullet cites `archon_search/server/mcp.py:_needs_install_trigger`.**
    Wrong source citation. `_needs_install_trigger` at `mcp.py:255-277` controls whether the *server startup* enqueues an install/sync job; it doesn't itself reindex. The "previous run was `IN_PROGRESS` (crash mid-index)" behavior is implemented in `archon_search/sync.py` — specifically the `_reset_stale_in_progress` logic at `sync.py:321-344` which resets `IN_PROGRESS` → `PENDING` so the next sync re-runs them. `FAILED` collections also re-trigger via `_needs_install_trigger`'s "any status other than DONE" check, but the actual reindex work happens in `sync.py`, not `mcp.py`. Citation should be `archon_search/sync.py` (with a secondary pointer to `mcp.py:_needs_install_trigger` for the startup-time gating).

15. **Line 10 — "the same path always produces the same name."**
    Verified at `sync.py:26-42`, but the doc omits that `path_to_collection_name` is **collision-unaware by design** (the function's own docstring says so) — `SearchCollectionSync` applies collision resolution. Two distinct paths with identical `Path.name` (e.g. `/a/docs` and `/b/docs`) will produce the same raw name and get disambiguated downstream. The "same path → same name" claim is true but the inverse implication (different paths → different names) is false; a user reading this might be misled.

## Verified claims

- Line 11: two collection lists, pinned always included, removal blocked until unpin — verified (`cli/collection.py:124-130`, `cfg.pinned_collections` / `cfg.collections` at `config.py:46-47`).
- Line 13: watcher opt-in via `[collections].watch = true` — verified (`config.py:191-192`, `watcher.py`).
- Line 14: chunk-size change triggers reindex when `auto_reindex_on_chunk_size_change = true` (default `True`) — verified (`config.py:37`, `sync.py:402`).
- Line 18: all ingestion CLI commands accept `--config PATH` — verified (each command in `cli/ingest.py`, `cli/sync.py`, `cli/collection.py` declares `--config`).
- Lines 32-34: `ingest` flag table — verified literally.
- Lines 54, 88: `collection list` / `info` output formats — verified.
- Lines 96-102: reindex steps + 404 behavior — verified.
- Lines 108-113: REST endpoints exist with the documented status codes (modulo the `IngestJob` vs `JobResponse` naming nit in #12).
- Line 118: watcher does not delete collections — verified.

## Unverifiable / ambiguous

- Line 40: "This is the command the background service runs on startup." Plausible (the server does call sync-equivalent logic on startup via the install trigger), but the doc does not cite a source and I did not exhaustively trace `server/app.py` startup. Worth a precise pointer.
- Line 11: "Pinned-only collections cannot be removed without first unpinning." Verified for the CLI; not verified for the REST `DELETE /collections/{name}` path. The REST handler returns 409 for some pinned cases (`routes_collections.py:200`), but whether the semantics exactly match "pinned but not in collections" needs a closer read of lines 171-230.
- Line 36: ingest output format — the exact f-string matches, but no test was consulted to confirm no other lines are emitted under error conditions (e.g. the "No --path given" line above).
- "Auto-generated description" mentions in lines 102 and elsewhere — the existence of `force_regenerate_description=True` is verified, but the doc doesn't define what "auto-description" means here; readers must cross-reference `description_generator.py`.
