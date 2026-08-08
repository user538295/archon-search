**Purpose**: Ingest documents and manage collections.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Ingestion and collections

## Principles

1. **A "collection" is a named index over one source path.** The name is derived from the path via `archon_search.sync.path_to_collection_name`; the same path always produces the same name. `path_to_collection_name` is collision-unaware by design — two distinct paths with the same `Path.name` (e.g. `/a/docs` and `/b/docs`) produce the same raw name and are disambiguated downstream by `SearchCollectionSync`.
2. **Two collection lists.** `[collections].collections` are normal collections; `[collections].pinned_collections` are always included in every search regardless of routing. Pinned-only collections cannot be removed without first unpinning (enforced in `cli/collection.py` and by `DELETE /collections/{name}`).
3. **Only `sync` is incremental.** `archon-search sync` consults the indexing-state store, skips already-indexed files, and resets stale `IN_PROGRESS` entries to `PENDING` for crash recovery (`sync.py:_reset_stale_in_progress`). By contrast, `archon-search ingest` submits an async `POST /ingest` job that re-processes every file under the path with no state-store consultation. Use `collection reindex` to force a full rebuild (clears state and drops the LanceDB table).
4. **The watcher is opt-in.** Set `[collections].watch = true` to keep the index in sync with on-disk changes via watchdog (`archon_search/watcher.py`, `sync.py`).
5. **Chunk-size changes trigger reindex.** If `chunk_size` differs from the value previously used for a collection and `auto_reindex_on_chunk_size_change = true` (default), affected collections rebuild on the next start.

> **Write commands require a running server.** All CLI write commands (`ingest`, `sync`, `collection add/remove/reindex/reindex-metadata/migrate`, `export`, `import`) are HTTP proxies — they submit jobs to a running `archon-search serve` and accept `--api-url` / `--api-key` instead of `--config`. On a refused connection they exit `1` with `archon-search serve is not running. Start it first with: archon-search serve` See [`40_running_the_server.md`](./40_running_the_server.md).

## Supported file types

`archon-search` routes each file to a format handler by extension (`archon_search/parser.py`). `markitdown` is a **core** dependency, so Office formats work on a fresh install with no extra steps.

| Extension(s) | Category | Handler |
|---|---|---|
| `.md` `.txt` `.py` `.js` `.ts` `.go` `.rs` `.java` `.sh` `.yaml` `.yml` `.json` `.toml` `.csv` `.tsv` | Plain text | built-in (`Path.read_text`) |
| `.html` `.htm` | HTML | trafilatura |
| `.pdf` | PDF | docling |
| `.docx` `.pptx` `.xlsx` | Office (Open XML) | markitdown |
| `.xls` | Legacy Excel | markitdown |
| `.rtf` | Rich Text | markitdown |
| `.epub` | E-book | markitdown |
| `.eml` | Email (RFC 822) | markitdown |
| `.msg` | Outlook message | markitdown |
| `.png` `.jpg` `.jpeg` `.tiff` `.tif` `.bmp` `.webp` | Images | docling (OCR) |
| *(any other extension)* | Fallback | built-in (`Path.read_text`) |

**Notes:**
- `.doc`, `.ppt`, and `.odt` are **not** in the Office handler — markitdown has no converter for these formats (`UnsupportedFormatException`). They fall through to the plain-text fallback and are read as raw UTF-8 with `errors="replace"`, producing garbled binary output. Convert them to their Open XML equivalents (`.docx`/`.pptx`) or `.pdf` before ingesting.
- `.gif` falls through to plain-text and produces garbage — OCR on a single animation frame would be misleading, so it is intentionally excluded from the image handler.
- `.svg` also uses the plain-text fallback, but yields readable SVG/XML markup (indexable, though it contains XML tags rather than prose).
- `.rtf` is handled by markitdown but may return raw RTF control codes rather than extracted prose.
- trafilatura is not bundled — install it separately (via the wizard or `uv pip install trafilatura`); HTML ingestion fails on a bare `uv sync --dev` without it.

## Large files and the size guard

There is no fixed size ceiling on ingestion. Large PDFs (research papers, 500-page manuals, ebooks) ingest successfully — docling materialises the document in memory, so RAM during conversion scales with document size. To protect memory-constrained hosts, set an explicit guard:

```toml
[ingest]
max_file_mb = 100   # default 0 = unlimited (no size guard)
```

The guard fires **before** parsing at two levels (`archon_search/config.py`, `pipeline.ingest_file()`, and the `POST /ingest` route):

- **Single-file `POST /ingest`** over the limit → HTTP **413** with an actionable message naming the file size, the configured limit, and how to fix it (`code="file_too_large"`); no job is created, no partial indexing occurs. The MCP `ingest_file` tool surfaces the same `file_too_large` error.
- **Directory ingest / batch** → oversized files do not fail the whole job; each produces a per-file `IngestResult` with `code="file_too_large"` inside the job, and other files continue.

The CLI performs no local size check — it prints the server's 413 message and exits `1`.

**Memory under load (D4).** For large files and large corpora the pipeline flushes embed+write batches incrementally (`_INGEST_CHUNK_BATCH_SIZE = 512` chunks per batch) instead of accumulating all chunks and vectors before the first write. This keeps single-file and directory ingests within a bounded footprint (~2 MB per batch) so they complete on 1 GB containers regardless of corpus size. Batch size is an internal constant, not a config knob. Parse-time memory (docling/markitdown internals) is out of D4's scope — use the size guard above to bound it.

## CLI commands

### `archon-search ingest`

One-shot ingest of a **file or directory**. Submits an async `POST /ingest` job; the server processes every file under `--path`.

```bash
# Ingest a directory
archon-search ingest --path /Users/me/docs --collection docs

# Ingest a single file (collection name derived from the full filename)
archon-search ingest --path /Users/me/report.pdf     # → collection "report_pdf"

# Ingest and block until the job completes
archon-search ingest --path /Users/me/docs --collection docs --wait
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--path PATH` | **required** | File or directory to ingest (resolved to an absolute path). Omitting it exits `1`. |
| `--collection NAME` | `path_to_collection_name(path)` | Override the derived collection name. |
| `--wait` | off | Poll `GET /jobs/{id}` until terminal and print progress. Exits `1` on a non-DONE terminal state. |
| `--api-url URL` | `http://localhost:8765` | Base URL of the server. |
| `--api-key KEY` | env `ARCHON_SEARCH_API_KEY` or key file | Bearer token for server auth. |

Output (no `--wait`): `Ingest job submitted: <job_id>. Collection: '<name>'` plus a tracking hint. See [`100_jobs_and_async_operations.md`](./100_jobs_and_async_operations.md) for polling patterns.

### `archon-search sync`

Submits a server-side `POST /sync` job that re-syncs all configured collections incrementally.

```bash
archon-search sync [--wait] [--api-url URL] [--api-key KEY]
```

Output (no `--wait`): `Sync job submitted: <job_id>. Track progress with: archon-search jobs status <job_id>`. With `--wait`: adds `Sync complete.` on success or exits `1` on failure.

### `archon-search collection list`

Proxies `GET /collections/`. Prints one line per collection: `<name>  docs=<n>  chunks=<n>` (or `No collections found.`).

### `archon-search collection add <path>`

Registers the path as a collection and enqueues an ingest job. The server writes the path to `archon-search.toml` server-side (`_maybe_save_config`) — the CLI writes no TOML. The command returns immediately with a job ID (never freezes the terminal, even for large directories).

```bash
archon-search collection add /Users/me/docs
archon-search collection add /Users/me/docs --wait
```

- `--wait` — poll `GET /jobs/{id}` until terminal; prints `Collection '<name>' ingested successfully.` on DONE, exits `1` on FAILED. Ctrl-C during `--wait` cancels the local poll only; the server job keeps running.
- If the path/name is already registered the server returns **409** and the CLI exits `1`.
- If the path does not exist, the server still accepts the request (**202**) and creates a collection entry, but the background ingest job transitions to **FAILED** with `"path does not exist or is not a file/directory"`. The collection remains registered (with `docs=0`); use `collection remove` to clean it up.
- There is no "pin" flag — add pinned collections to `pinned_collections` in TOML manually. The request body accepts only `path` and optional `embedding_model` (name is always server-derived).

### `archon-search collection remove <name>`

Proxies `DELETE /collections/{name}`.

```bash
archon-search collection remove docs
```

- Pinned-only collections → server **409**, CLI prints `"Cannot remove '<name>': collection is pinned-only. Un-pin it first."`
- Active write in progress → server **503**, CLI prints a retry-after-job message.
- The old `--dry-run`, `--force`, and `--config` flags were removed (see `BREAKING.md`).

### `archon-search collection info <name>`

Proxies `GET /collections/{name}`. Prints the `CollectionMeta` repr for one collection; exits `1` if unknown.

### `archon-search collection reindex <name>`

Proxies `POST /collections/{name}/reindex` — a full asynchronous rebuild (clears indexing state, drops and rebuilds the LanceDB table and FTS index).

```bash
archon-search collection reindex docs --wait
```

`--wait`, `--api-url`, `--api-key` behave as for `ingest`. Exits `1` with `collection not found` on a 404.

### `archon-search collection reindex-metadata <name>`

Proxies `POST /collections/{name}/reindex-metadata`. Backfills per-chunk metadata (`file_type`, `updated_at`, `ingested_by`) and optionally normalizes timestamps **in place** — it does **not** re-embed or re-chunk, so it is far cheaper than `reindex`.

```bash
archon-search collection reindex-metadata docs --dry-run --wait
archon-search collection reindex-metadata docs --wait
```

- `--dry-run` — report processed/updated/skipped counts without writing.
- `--normalize-timestamps` / `--no-normalize-timestamps` (default on) — rewrite `indexed_at`/`updated_at` to fixed-width UTC.
- A rebuild already in progress returns **409**. See [`55_chunk_metadata_and_enrichment.md`](./55_chunk_metadata_and_enrichment.md) for what these fields drive.

### `archon-search collection migrate <name>`

Proxies `POST /collections/{name}/migrate`. Without flags (or with `--dry-run`) it prints pending schema migrations and changes nothing. Use `--apply` for in-place migrations (synchronous) or `--apply --backup-first` for rewrite migrations (async job; add `--wait` to poll). This is the operator step required after certain upgrades — see [`../MigrationGuide/05_data_migration.md`](../MigrationGuide/05_data_migration.md).

### Export / import

`archon-search export <collection>` and `archon-search import <collection> <path>` move a collection between instances or snapshot it to disk. These are top-level commands (proxying `POST /collections/{name}/export` and `.../import`). See [`90_export_import.md`](./90_export_import.md) for formats, flags, and round-trip guarantees.

## REST equivalents

The same operations are available over HTTP for programmatic use (all require a `Bearer` token):

- `POST /collections/` (202) — add; `DELETE /collections/{name}` — remove (409 pinned-only, 404 unknown).
- `GET /collections/` / `GET /collections/{name}` — list / detail.
- `POST /collections/{name}/reindex` (202) / `.../reindex-metadata` (202) / `.../migrate` (202).
- `POST /ingest` (202), `POST /sync`, `GET /jobs/{id}`, `DELETE /jobs/{id}` — job lifecycle.

See `archon_search/server/routes_collections.py` and `routes_jobs.py`, and [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) (the live `GET /openapi.json` is authoritative) for full request/response shapes.

## MCP tools

The MCP surface (`POST /mcp`, streamable HTTP transport) exposes two ingest tools. Both are **synchronous** — they block until done and return the result directly, unlike REST which returns a job.

| Tool | Parameters | Output |
| --- | --- | --- |
| `ingest_file` | `path: str` (required), `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error`, `warnings: list[str]`, `code: str \| null`. On unsafe `path`: `{error, code: "path_unsafe"}`; when a reindex holds the lock: `{error, code: "store_busy"}`; when file exceeds `max_file_mb`: `{status: "error", code: "file_too_large"}`. |
| `ingest_directory` | `path: str` (required), `glob_pattern: str = "**/*"`, `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `list[IngestResultSchema dict]`; progress reported via MCP progress notifications. |

For MCP client setup and the complete tool reference, see [`../DeveloperGuide/05_mcp_integration.md`](../DeveloperGuide/05_mcp_integration.md).

## Watcher behavior

When `[collections].watch = true`, the server starts a watchdog observer (`archon_search/watcher.py`) on each collection's source directory. Create/modify/delete events trigger an incremental ingest through `pipeline.ingest_file()` (so the `max_file_mb` guard applies automatically). The watcher does **not** delete a collection when its source directory is deleted — use `collection remove` for that.

## Reindex triggers

A collection is reindexed automatically when:

- `chunk_size` differs from the value last used **and** `auto_reindex_on_chunk_size_change = true`.
- The previous run was left `IN_PROGRESS` (crash mid-index) — `sync.py:_reset_stale_in_progress` resets it to `PENDING` so the next sync re-runs it. Server startup separately gates whether to enqueue an install/sync job (`server/mcp.py:_needs_install_trigger` — any status other than `DONE` re-triggers).
- `archon-search collection reindex <name>` is invoked explicitly, or a pending per-collection embedding-model change is committed (below).

## Per-collection embedding model

Every collection uses the global `[database].embedding_model` unless overridden — useful for domain-specific vocabularies or trialling a new model without rebuilding everything.

**At creation** — pass `embedding_model` in the `POST /collections/` body (unknown names → `422`):

```bash
curl -X POST http://localhost:8765/collections/ \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"path": "/Users/me/code", "embedding_model": "BAAI/bge-small-en-v1.5"}'
```

**On an existing collection** — `PATCH /collections/{name}` runs a safe state machine:

- Same model as active → clears any pending change; no reindex.
- Indexed data + different model → sets `pending_embedding_model` and `needs_reindex = true`. The current index keeps serving until you reindex.
- No indexed data yet → updates `active_embedding_model` directly; no reindex.

`GET /collections/{name}` reports `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, and `reindex_job_id`; `GET /collections/` and `GET /status` surface `needs_reindex` per collection. Commit the change with `POST /collections/{name}/reindex` — on success `active_embedding_model` is updated and `needs_reindex` cleared. The MCP `update_collection` tool exposes the same state machine.

**Embedder cache.** With mixed models the server keeps an LRU cache of loaded embedders (capacity `[database].embedder_cache_size`, default 3). Set `[database].eager_load_embedders = true` to pre-warm all known collection models at startup.

## FTS index maintenance

`archon-search` uses **incremental** FTS maintenance (`table.optimize()`) rather than a full rebuild on every ingest, so single-document updates into large collections complete in milliseconds.

- **Ingest / sync**: FTS updates automatically via `store.optimize_fts()` at the end of each batch or watcher cycle — no action needed.
- **Delete**: `delete_document` calls `optimize_fts` after removing chunks, so deletions leave no phantom FTS hits.
- **Manual repair**: if the FTS index becomes inconsistent (e.g. a crash mid-ingest), run `archon-search collection reindex <name>` for a full re-ingest + `rebuild_fts_index()`. Use sparingly on large collections.

**BM25 note**: after many incremental `optimize()` calls BM25 scores may drift slightly from a freshly rebuilt index; result-set membership is identical and ranking quality is unaffected. Run a periodic reindex if you need strict score reproducibility.

## Building community structure

Once entity extraction is populated (graph extraction at ingest with `[graph] enabled = true`), cluster entities into communities for graph-aware search:

```bash
archon-search graph build-communities <collection> [--namespace <ns>] [--wait]
```

This proxies `POST /graph/{collection}/rebuild-communities` (async Leiden job). It is never triggered automatically on ingest — re-run it after significant new ingest (a stale `last_built_at` in `GET /status` signals drift). Full details, config knobs, and prerequisites live in [`65_graph_search.md`](./65_graph_search.md).

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents and reading order.
- [`40_running_the_server.md`](./40_running_the_server.md) — start the server that write commands proxy to.
- [`55_chunk_metadata_and_enrichment.md`](./55_chunk_metadata_and_enrichment.md) — metadata fields and `reindex-metadata`.
- [`60_searching.md`](./60_searching.md) — how collections are queried.
- [`90_export_import.md`](./90_export_import.md) — export / import a collection.
- [`100_jobs_and_async_operations.md`](./100_jobs_and_async_operations.md) — tracking ingest / reindex jobs.
- [`../MigrationGuide/05_data_migration.md`](../MigrationGuide/05_data_migration.md) — `collection migrate` and schema upgrades.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — consolidated REST + MCP + CLI reference.
