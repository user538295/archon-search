**Purpose**: Ingest documents and manage collections.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Ingestion and collections

## Principles

1. **A "collection" is a named index over one source path.** The collection name is derived from the path via `archon_search.sync.path_to_collection_name`; the same path always produces the same name. Note: `path_to_collection_name` is collision-unaware by design — two distinct paths with the same `Path.name` (e.g. `/a/docs` and `/b/docs`) produce the same raw name and are disambiguated downstream by `SearchCollectionSync`.
2. **Two collection lists.** `[collections].collections` are normal collections; `[collections].pinned_collections` are always included in every search regardless of routing. Pinned-only collections cannot be removed without first unpinning (enforced for the CLI at `cli/collection.py:124-130` and for the REST `DELETE /collections/{name}` at `routes_collections.py:197-205`).
3. **Only `sync` is incremental.** `archon-search sync` consults the indexing state store and skips already-indexed files; it also resets stale `IN_PROGRESS` entries to `PENDING` for crash recovery (`sync.py:_reset_stale_in_progress`). In contrast, `archon-search ingest` calls `pipeline.ingest_directory` directly and re-processes every file under the path with no state-store consultation. Use `collection reindex` to force a full rebuild (clears state and drops the LanceDB table).
4. **The watcher is opt-in.** Set `[collections].watch = true` to keep the index in sync with on-disk changes via watchdog (`archon_search/watcher.py`, `sync.py`).
5. **Chunk-size changes trigger reindex.** If `chunk_size` differs from the value previously used for a collection and `auto_reindex_on_chunk_size_change = true` (default), affected collections rebuild on the next start.

## CLI commands

All `archon-search` ingestion commands accept `--config PATH` to point at a non-default TOML file.

### `archon-search ingest`

One-shot ingest of a directory. Re-processes every file under `--path` on each run; this command is **not** incremental.

```bash
archon-search ingest --path /Users/me/docs --collection docs
```

Flags (`archon_search/cli/ingest.py`):

| Flag | Default | Effect |
| --- | --- | --- |
| `--path PATH` | `~/.archon-search/history/sessions` | Directory to ingest. When omitted, the CLI prints `No --path given, using default: <path>` to stdout before running. |
| `--collection NAME` | Path basename | Override the collection name. |
| `--config PATH` | default config path | Alternative config file. |

Output: `Ingest complete: <ok> ingested, <errors> errors.` (no per-file progress trail — that is `sync`'s job).

### `archon-search sync`

Re-sync everything declared in config (`pinned_collections + collections`). Honors the indexing state store so it skips work for already-indexed files. On entry it also resets any stale `IN_PROGRESS` entries to `PENDING` so that work interrupted by a crash is retried on the next run (`sync.py:_reset_stale_in_progress`).

```bash
archon-search sync
```

This is conceptually the same operation the background service runs on startup via its install-trigger path. #Unverified — startup wiring in `server/app.py` was not traced end-to-end for this doc. Use `sync` manually after editing the collection lists in TOML without going through `collection add`.

### `archon-search collection list`

```bash
archon-search collection list
```

Prints one line per collection: `<name>  docs=<n>  chunks=<n>`. Returns "No collections found." if empty. (`archon_search/cli/collection.py:list_cmd`.)

### `archon-search collection add <path>`

Adds the path to `[collections].collections` in the TOML (if not already present) and ingests it immediately. If the config file does not yet exist, `collection add` creates a new TOML document at the default config location.

```bash
archon-search collection add /Users/me/docs
```

Pinned collections must be added manually to `pinned_collections` in TOML — the CLI does not have a "pin" flag.

### `archon-search collection remove <path>`

```bash
archon-search collection remove /Users/me/docs
archon-search collection remove /Users/me/docs --dry-run
archon-search collection remove /Users/me/docs --force
```

Flags:

- `--dry-run` — print what would happen, do not execute.
- `--force` — proceed even if the service is running.
- `--dry-run` and `--force` are mutually exclusive.

If the path is in `pinned_collections` but **not** in `collections`, removal is rejected with a message instructing you to unpin first. Path comparison uses resolved absolute paths (`Path(p).expanduser().resolve()`), not raw string equality.

### `archon-search collection info <name>`

```bash
archon-search collection info docs
```

Prints `str(meta)` for one collection — i.e. the dataclass `__repr__`, formatted like `CollectionMeta(name=..., description=..., centroid=[...], ...)` rather than a pretty-printed view. Returns exit code 1 if the collection is unknown.

### `archon-search collection reindex <name>`

```bash
archon-search collection reindex docs
```

Forces a full rebuild:

1. Clears the indexing state for the collection.
2. Drops the LanceDB table (best-effort; errors are swallowed).
3. Re-ingests the source path with `force_regenerate_description=True`, regenerating the auto-description (see `description_generator.py` for what "auto-description" means).

The collection must already exist in `pinned_collections` or `collections` in the TOML; otherwise the command exits 1.

## REST equivalents

The same operations are available over HTTP for programmatic use:

- `POST /collections/` — add a collection (returns 202 + `JobResponse`).
- `DELETE /collections/{name}` — remove (409 if pinned-only, 404 if unknown).
- `GET /collections/` / `GET /collections/{name}` — list / detail.
- `POST /collections/{name}/reindex` — start a reindex job.
- `POST /ingest`, `GET /jobs/{id}`, `DELETE /jobs/{id}` — generic ingest job lifecycle.

See `archon_search/server/routes_collections.py` and `routes_jobs.py` for full request/response shapes, and [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) for the consolidated reference.

## Watcher behavior

When `[collections].watch = true`, the server starts a watchdog observer (`archon_search/watcher.py`) on each collection's source directory. File create/modify/delete events trigger an incremental ingest. The watcher does **not** delete collections when their source directory is deleted; use `collection remove` for that.

## Reindex triggers

A collection is reindexed automatically when:

- `chunk_size` differs from the value last used for that collection **and** `auto_reindex_on_chunk_size_change = true`.
- The previous indexing run was marked `IN_PROGRESS` (interpreted as a crash mid-index) — `IN_PROGRESS` is reset to `PENDING` by `archon_search/sync.py:_reset_stale_in_progress` so the next sync re-runs it. Server startup separately gates whether to enqueue an install/sync job via `archon_search/server/mcp.py:_needs_install_trigger` (any status other than `DONE` re-triggers); the actual reindex work runs in `sync.py`, not `mcp.py`.
- `archon-search collection reindex <name>` is invoked explicitly.

## Related documents

- [`02_configuration.md`](./02_configuration.md) — `[collections]` and `[database].chunk_size`.
- [`05_searching.md`](./05_searching.md) — how the collections are queried.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — empty-results and reindex-stuck issues.
