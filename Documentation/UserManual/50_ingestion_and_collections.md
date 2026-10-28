**Purpose**: Ingest documents and manage collections.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-10-17 / **Next review**: 2027-07-29

# Ingestion and collections

## Principles

1. **A "collection" is a named index over one source path.** The name is derived from the path via `archon_search.sync.path_to_collection_name`; the same path always produces the same name. `path_to_collection_name` is collision-unaware by design — two distinct paths with the same `Path.name` (e.g. `/a/docs` and `/b/docs`) produce the same raw name and are disambiguated downstream by `SearchCollectionSync`.
2. **Two collection lists.** `[collections].collections` are normal collections; `[collections].pinned_collections` are always included in every search regardless of routing. Pinned-only collections cannot be removed without first unpinning (enforced in `cli/collection.py` and by `DELETE /collections/{name}`).
3. **Only `sync` is incremental.** `archon-search sync` consults the indexing-state store, skips already-indexed files, and resets stale `IN_PROGRESS` entries to `PENDING` for crash recovery (`sync.py:_reset_stale_in_progress`). By contrast, `archon-search ingest` submits an async `POST /ingest` job that re-processes every file under the path with no state-store consultation. Use `collection reindex` to force a full rebuild (clears state and drops the LanceDB table).
4. **The watcher is opt-in.** Set `[collections].watch = true` to keep the index in sync with on-disk changes via watchdog (`archon_search/watcher.py`, `sync.py`).
5. **Chunk-size changes trigger reindex.** If `chunk_size` differs from the value previously used for a collection and `auto_reindex_on_chunk_size_change = true` (default), affected collections rebuild on the next start.

> **Write commands require a running server.** All CLI write commands (`ingest`, `sync`, `collection add/remove/reindex/reindex-metadata/migrate`, `export`, `import`) are HTTP proxies — they submit jobs to a running `archon-search serve` and accept `--api-url` / `--api-key` instead of `--config`. On a refused connection they exit `1` with `archon-search serve is not running. Start it first with: archon-search serve` (`_SERVER_NOT_RUNNING_MSG`); if the server is reachable but still loading models or running its startup sync, the message is instead `archon-search is starting up. Please wait for it to finish loading models, then retry.` — see `cli/_helpers.py:_server_connect_fail_msg` and [`100_jobs_and_async_operations.md`](./100_jobs_and_async_operations.md) for the full probe logic. For a custom `--api-url`, a probe failure always returns the not-running message immediately (never consults the local service manager). See [`40_running_the_server.md`](./40_running_the_server.md).

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

Ingestion has a default size ceiling of 100 MiB per file (`[ingest].max_file_mb`; before 2026-10-25 the default was unlimited). Large PDFs (research papers, 500-page manuals, ebooks) up to that size ingest successfully — docling materialises the document in memory, so RAM during conversion scales with document size. That conversion happens in a separate worker process (see [Parse memory](#parse-memory-pdfs-and-images) below), so it does not permanently inflate the server. Raise the guard for bigger files, lower it on memory-constrained hosts, or set `0` for no limit:

```toml
[ingest]
max_file_mb = 100   # default 100 MiB; 0 = unlimited (no size guard)
```

The guard fires **before** parsing at two levels (`archon_search/config.py`, `pipeline.ingest_file()`, and the `POST /ingest` route):

- **Single-file `POST /ingest`** over the limit → HTTP **413** with an actionable message naming the file size, the configured limit, and how to fix it (`code="file_too_large"`); no job is created, no partial indexing occurs. The MCP `ingest_file` tool surfaces the same `file_too_large` error.
- **Directory ingest / batch** → oversized files do not fail the whole job; each produces a per-file `IngestResult` with `code="file_too_large"` inside the job, and other files continue.

The CLI performs no local size check — it prints the server's 413 message and exits `1`.

**Other per-file limits.** A PDF or image conversion is bounded by `[ingest].parse_timeout_seconds` (300) plus `parse_timeout_seconds_per_mb` (10) per MiB of the file; on timeout that one document fails with a sanitized error and the parse worker is replaced. A zip-based Office file (docx, pptx, xlsx, epub) is refused before it is opened when its central directory lists more than `max_zip_entries` (10000) entries or more than `max_zip_uncompressed_mb` (512) MiB uncompressed. Only regular files are read: a symlink, FIFO or device is refused. On macOS docling runs on the CPU, so a large scanned PDF is slow (about 3.8 s per MiB measured on an Apple M-series Mac).

**What a directory ingest walks.** Directory ingest does not index hidden directories (names starting with `.`; it only opens credential ones such as `.ssh` to count their files as skipped), `node_modules`, `__pycache__`, `venv` or `site-packages`, and files in them are not indexed (this changed on 2026-10-25; point `path` at the file itself or at a directory outside them to index such files). A tree with more than `[ingest].max_discovered_files` (100000) candidate files is refused. One INFO line reports how many files were skipped because they sit in a location the server never reads.

**Concurrency.** At most `[ingest].max_concurrent_tasks` (4) ingest jobs run at once. A request over the limit gets `503` with code `ingest_busy` and a `Retry-After` header, and nothing is created or queued — retry after the header's delay. A request body over `[server].max_request_body_mb` (32) gets `413` with code `request_body_too_large` before authentication; send large corpora as several `POST /ingest` calls or by path.

**Memory under load (D4).** For large files and large corpora the pipeline flushes embed+write batches incrementally (`_INGEST_CHUNK_BATCH_SIZE = 512` chunks per batch) instead of accumulating all chunks and vectors before the first write. This keeps single-file and directory ingests within a bounded footprint (~2 MB per batch) so they complete on 1 GB containers regardless of corpus size. Batch size is an internal constant, not a config knob. Parse-time memory (docling/markitdown internals) is out of D4's scope — for docling formats it is bounded by the worker recycle described next, and otherwise by the size guard above.

### Parse memory (PDFs and images)

PDFs and images are converted — and images OCR'd — in a **separate worker process**, not in the server. The OCR stack (docling → RapidOCR → onnxruntime) retains native memory on every conversion that nothing in-process gives back, so the worker exits after a fixed number of files and a fresh one takes over; process exit is what returns that memory to the OS.

```toml
[ingest]
max_tasks_per_child = 25   # default; must be >= 1
```

Lower it to cap memory harder, raise it to pay the worker restart (~9 s) less often. Images (not PDFs — PDFs keep docling's own OCR resolution) are OCR'd at their native resolution rather than upscaled 3×, which cuts retained memory further; recognised text was unchanged on the text-bearing image tested, not a guarantee that resolution never affects OCR quality on arbitrary imagery. `max_file_mb` does **not** help here — it measures bytes on disk, and a 200 KB icon can cost hundreds of MB of RAM to OCR.

## Letting a team key ingest by path

A **path** ingest (`archon-search ingest PATH`, `POST /ingest` with `path`, `POST /collections/`, the MCP `ingest_file` / `ingest_directory` tools) reads a file **on the server host**, so the server limits where it may read from:

- **Always denied, for every key:** the data directory and the server's own state (keys, config, logs, database, telemetry, backups), `/proc`, `/sys`, `/dev`, and credential files and directories — `.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, `.env` and `.env.*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.pgpass`, private keys (`id_*`, `*.pem`, `*.p12`, `*.pfx`, `*.jks`, and `*.key` files that start with a PEM header), `/etc/passwd`, `/etc/shadow`, browser profiles and macOS Keychains. A symlink or hard link to a denied file is denied too.
- **Admin key (the default key):** may read any other path while `[ingest].allowed_roots` is empty (the default).
- **Non-admin keys (namespace keys):** may supply a path **only** when `[ingest].allowed_roots` lists at least one directory, and only inside those directories. With an empty list a non-admin key cannot ingest by path at all. Inline `documents` always work, and so does a single-key deployment (the default key is admin).

To let a team ingest from `/srv/docs/team-a` on a fresh install:

1. Set the roots in `~/.archon-search/archon-search.toml` (absolute paths) and restart the server (the config is read only at startup, see [`40_running_the_server.md`](./40_running_the_server.md#applying-config-changes)):

   ```toml
   [ingest]
   allowed_roots = ["/srv/docs/team-a"]
   ```

2. Create a non-admin key for the team's namespace with the default (admin) key. The CLI uses the key file automatically and prints the token exactly once:

   ```bash
   archon-search key create --namespace team-a --label "team a ingest"
   ```

   The REST equivalent is `POST /keys` with `{"namespace": "team-a"}` (`admin` defaults to `false`; only an admin key can set it to `true`). A `[namespaces]` token in the TOML is also non-admin; it must be at least 32 printable characters.

3. The team ingests with that token (`ARCHON_SEARCH_API_KEY=<token>` or `--api-key`). A path inside the roots works; anything else is refused with `400`:

   | Situation | REST (`POST /ingest`) | MCP tool |
   | --- | --- | --- |
   | `allowed_roots` empty | `400` `{"detail": "path is unsafe: path-based ingest by this API key needs the operator to set [ingest].allowed_roots; send the content as inline documents instead"}` | `{"error": "<same text>", "code": "path_unsafe"}` |
   | Path outside the roots | `400` `"path is unsafe: outside_allowed_roots"` | `{"error": "path is unsafe: the path is outside the directories this server is allowed to ingest from", "code": "path_unsafe"}` |
   | Denied location | `400` `"path is unsafe: denied_location"` | `{"error": "path is unsafe: this location is never ingested (server state, credentials, system files)", "code": "path_unsafe"}` |

   The same text and code apply to a file *inside* a root that is a denied location. Paths named in `[collections]` are operator-trusted: they skip `allowed_roots`, but the deny list still applies.

At startup the server logs one WARNING when non-admin keys exist and `allowed_roots` is empty (`non-admin API keys exist but [ingest].allowed_roots is empty: path-based ingest is refused for them (inline documents still work)`). Keys created later are not announced; the refusal itself names the setting.

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

- If the path does not exist, the server still accepts the request (**202**) and the background ingest job transitions to **FAILED** with `"path does not exist or is not a file/directory: <path>"`, `<path>` being the request's `path` as the server received it (the server's own `~` expansion and symlink resolution are not applied to the message) (other ingest failures report `"ingest failed; see the server log for details"`). The collection remains registered (with `docs=0`); use `collection remove` to clean it up.

### `archon-search sync`

Submits a server-side `POST /sync` job that re-syncs all configured collections incrementally.

```bash
archon-search sync [--wait] [--api-url URL] [--api-key KEY]
```

Output (no `--wait`): `Sync job submitted: <job_id>. Track progress with: archon-search jobs status <job_id>`. With `--wait`: adds `Sync complete.` on success or exits `1` on failure.

### `archon-search collection list`

Proxies `GET /collections/`. Prints one line per collection: `<name>  docs=<n>  chunks=<n>` (or `No collections found.`).

When any listed collection shows `docs=0`, the command prints a hint pointing to `archon-search jobs list` (then `archon-search jobs status <id>`) and the server log at `~/.archon-search/logs/archon-search.log` (or the equivalent path under `ARCHON_SEARCH_DATA_DIR`) so a failed ingest can be diagnosed. The path is best-effort: the CLI cannot read the server's config, so it will differ if the server sets a custom `[logging].log_file` or has file logging disabled.

### `archon-search collection add <path>`

Registers the path as a collection and enqueues an ingest job. The server writes the path to `archon-search.toml` server-side (`_maybe_save_config`) — the CLI writes no TOML. The command returns immediately with a job ID (never freezes the terminal, even for large directories).

```bash
archon-search collection add /Users/me/docs
archon-search collection add /Users/me/docs --wait
```

- `--wait` — poll `GET /jobs/{id}` until terminal; prints `Collection '<name>' ingested successfully.` on DONE, exits `1` on FAILED. Ctrl-C during `--wait` cancels the local poll only; the server job keeps running.
- If the path/name is already registered the server returns **409** and the CLI exits `1`.
- If the path does not exist, the server still accepts the request (**202**) and creates a collection entry, but the background ingest job transitions to **FAILED** with `"path does not exist or is not a file/directory: <path>"`, `<path>` being the request's `path` as the server received it (the server's own `~` expansion and symlink resolution are not applied to the message) (other ingest failures report `"ingest failed; see the server log for details"`). The collection remains registered (with `docs=0`); use `collection remove` to clean it up.
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

Proxies `POST /collections/{name}/migrate`. Without flags (or with `--dry-run`) it prints pending schema migrations and changes nothing. Use `--apply` for in-place migrations (synchronous; answers `404` if a concurrent `DELETE` removed the collection meanwhile, and `503` `store_busy` if another write holds the collection lock too long) or `--apply --backup-first` for rewrite migrations (async job; add `--wait` to poll). This is the operator step required after certain upgrades — see [`../MigrationGuide/05_data_migration.md`](../MigrationGuide/05_data_migration.md).

### Export / import

`archon-search export <collection>` and `archon-search import <collection> <path>` move a collection between instances or snapshot it to disk. These are top-level commands (proxying `POST /collections/{name}/export` and `.../import`). See [`90_export_import.md`](./90_export_import.md) for formats, flags, and round-trip guarantees.

## REST equivalents

The same operations are available over HTTP for programmatic use (all require a `Bearer` token):

- `POST /collections/` (202) — add (**admin key only**: it edits the shared collection list in `archon-search.toml`, so any other key gets `403`); `DELETE /collections/{name}` — remove (409 pinned-only, 404 unknown, 503 `store_busy` on a collection-lock timeout or while a job may still be writing to the collection — an ingest, reindex, import, migration, metadata reindex (a `--dry-run` one only reads, so it never blocks) or community rebuild; wait for it to finish — a job cancelled with `DELETE /jobs/{id}` blocks until it settles, so never cancel a metadata-reindex or community-rebuild job an older server left `PENDING`: it does not block, but once cancelled it blocks until the next server restart — then retry).
- `GET /collections/` / `GET /collections/{name}` — list / detail.
- `POST /collections/{name}/reindex` (202) / `.../reindex-metadata` (202) / `.../migrate` (202). Reindex and reindex-metadata answer `409` at once while a job of their kind already runs on the collection, `503` `store_busy` (with `Retry-After`; no job created) when another write holds the collection lock too long, and `404` also when a concurrent `DELETE` removed the collection. Reindex answers `503` `service not ready` when the server lacks its embedder cache, pipeline or search store — a defensive check, as startup sets them before the port opens — even for a collection that does not exist; reindex-metadata (and `graph build-communities`) answer it when the server has no search store. A `--dry-run` reindex-metadata gets the `409` while a non-dry-run one runs, and otherwise waits behind another dry-run.
- `POST /ingest` (202), `POST /sync` (admin key only), `GET /jobs/{id}`, `DELETE /jobs/{id}` — job lifecycle. A collection name is owned by the namespace that created it: ingesting into a name another namespace owns fails the job with `collection name is not available to this namespace` (MCP: `{"error": …, "code": "collection_unavailable"}`) — for the admin key too. When more than `[ingest].max_concurrent_tasks` ingest jobs run, `POST /ingest` answers `503` `ingest_busy` with `Retry-After`.

`POST /ingest` takes either `path` (a file or directory **on the server host**) or `documents` — a list of `{"text": str, "source_path": str}` items whose content is sent inline. `documents` is the mode to use from a remote client, which cannot place files on the server: `source_path` is the document's logical identity and need not exist on disk, and it seeds the `doc_id`, so re-posting the same `source_path` replaces that document's chunks. Both fields are required — an item missing either, or with the wrong type, is rejected with `422` before the job is created.

Two rules constrain `source_path`. It must be **absolute and free of `..` segments**, the same check `path` gets (`400`, `"documents[i].source_path is unsafe: <reason>"`), and it is stored in its resolved form. And it is treated strictly as a name, never as a file: no `.acl` sidecar is read beside it, so an inline document's permissions come from its front-matter `_acl` key or the collection default only. Inline documents are also exempt from the maintenance orphan sweep — they have no file on disk to go missing.

`max_file_mb` does apply, per document: a `text` whose UTF-8 length exceeds the limit is rejected with the same `413` a single oversized file gets, before the job is created.

See `archon_search/server/routes_collections.py` and `routes_jobs.py`, and [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) (the live `GET /openapi.json` is authoritative) for full request/response shapes.

## MCP tools

The MCP surface (`POST /mcp`, streamable HTTP transport) exposes two ingest tools. Both are **synchronous** — they block until done and return the result directly, unlike REST which returns a job. Because they create no job, they do not appear in `GET /jobs`; while one runs, `DELETE /collections/{name}` of its collection answers `503` `store_busy` (retry after it returns — an `ingest_directory` of a large directory can take minutes).

| Tool | Parameters | Output |
| --- | --- | --- |
| `ingest_file` | `path: str` (required), `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `IngestResultSchema dict` — fields: `doc_id`, `chunks_created`, `status`, `error`, `warnings: list[str]`, `code: str \| null`. On unsafe `path`: `{error, code: "path_unsafe"}`; when another write holds the lock past the timeout: `{error, code: "store_busy"}`; when file exceeds `max_file_mb`: `{status: "error", code: "file_too_large"}`. |
| `ingest_directory` | `path: str` (required), `glob_pattern: str = "**/*"`, `collection?: str`, `chunk_ttl_seconds?: int \| null`, `chunk_scopes?: list[str] \| null` | `list[IngestResultSchema dict]`; progress reported via MCP progress notifications. |

For MCP client setup and the complete tool reference, see [`../DeveloperGuide/05_mcp_integration.md`](../DeveloperGuide/05_mcp_integration.md).

## Watcher behavior

When `[collections].watch = true`, the server starts a watchdog observer (`archon_search/watcher.py`) on each collection's source directory. Create/modify/delete events trigger an incremental ingest through `pipeline.ingest_file()` (so the `max_file_mb` guard applies automatically). The watcher does **not** delete a collection when its source directory is deleted — use `collection remove` for that. While a watcher sync runs — or a startup sync or `POST /sync` is writing that collection — `DELETE /collections/{name}` (and `collection remove`) answers `503` `store_busy` — retry after it completes; once the collection is removed from the config, the watcher ignores later changes under its path instead of re-creating it (restart the server to stop its observer, and re-adding the path with `POST /collections/` resumes the sync).

Chunks written by the watcher carry `ingested_by = "watcher"`. The one exception is a **genuine** collection-wide reindex triggered from inside a watcher-driven sync — a chunk-size or embedding-model change against a collection that already has a recorded `indexed_chunk_size` — where the rewritten chunks carry `ingested_by = "reindex"` instead. A collection with no completed sync-state entry yet (for example a file created during the boot window, before the startup sync finishes) is **not** a reindex: those chunks stay `"watcher"`.

## Reindex triggers

A collection is reindexed automatically when:

- `chunk_size` differs from the value last used **and** `auto_reindex_on_chunk_size_change = true`.
- The previous run was left `IN_PROGRESS` (crash mid-index) — `sync.py:_reset_stale_in_progress` resets it to `PENDING` so the next sync re-runs it. Server startup separately gates whether to enqueue an install/sync job (`server/mcp.py:_needs_install_trigger` — any status other than `DONE` re-triggers).
- `archon-search collection reindex <name>` is invoked explicitly, or a pending per-collection embedding-model change is committed (below).

## Per-collection embedding model

Every collection uses the global `[database].embedding_model` unless overridden — useful for domain-specific vocabularies or trialling a new model without rebuilding everything.

**At creation** — pass `embedding_model` in the `POST /collections/` body (a name outside the [supported list](#supported-embedding-models) → `422` with `unsupported model '<name>'; supported: …`):

```bash
curl -X POST http://localhost:8765/collections/ \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"path": "/Users/me/code", "embedding_model": "BAAI/bge-small-en-v1.5"}'
```

**On an existing collection** — `PATCH /collections/{name}` runs a safe state machine:

- Same model as active → clears any pending change; no reindex.
- Indexed data + different model → sets `pending_embedding_model` and `needs_reindex = true`. The current index keeps serving until you reindex.
- No indexed data yet → updates `active_embedding_model` directly; no reindex.

The change runs under the collection lock. `PATCH` answers `404` also when a concurrent `DELETE` removed the collection meanwhile (its meta row is not re-created), and `503` `store_busy` (with `Retry-After`) when another write holds the lock too long; the MCP `update_collection` tool answers `not_found` / `store_busy` likewise.

`GET /collections/{name}` reports `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, and `reindex_job_id`; `GET /collections/` and `GET /status` surface `needs_reindex` per collection. Commit the change with `POST /collections/{name}/reindex` — on success `active_embedding_model` is updated and `needs_reindex` cleared. The MCP `update_collection` tool exposes the same state machine.

**Embedder cache.** With mixed models the server keeps an LRU cache of loaded embedders (capacity `[database].embedder_cache_size`, default 3). At startup the server always populates that cache with the default `[database].embedding_model` and warms the reranker cross-encoder. Set `[database].eager_load_embedders = true` to extend that to every per-collection `active_embedding_model` as well.

### Supported embedding models

`embedding_model` accepts only the 19 names below (the registry in `archon_search/model_specs.py`; the `422`/validation error above lists them too). The weights are PyTorch checkpoints from Hugging Face, downloaded on first use into `<data-dir>/models` at a pinned revision. The vector sizes are listed in the same order as the names.

| Family | Model names | Vector size |
| --- | --- | --- |
| BGE English v1.5 | `BAAI/bge-small-en-v1.5`, `BAAI/bge-base-en-v1.5`, `BAAI/bge-large-en-v1.5` | 384, 768, 1024 |
| BGE English v1 | `BAAI/bge-small-en`, `BAAI/bge-base-en` | 384, 768 |
| BGE Chinese | `BAAI/bge-small-zh-v1.5` | 512 |
| Multilingual | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`, `intfloat/multilingual-e5-large` | 384, 768, 1024 |
| Snowflake Arctic | `snowflake/snowflake-arctic-embed-xs`, `snowflake/snowflake-arctic-embed-s`, `snowflake/snowflake-arctic-embed-m`, `snowflake/snowflake-arctic-embed-l` | 384, 384, 768, 1024 |
| GTE | `thenlper/gte-base`, `thenlper/gte-large` | 768, 1024 |
| Other | `mixedbread-ai/mxbai-embed-large-v1`, `sentence-transformers/all-MiniLM-L6-v2`, `ibm-granite/granite-embedding-small-english-r2`, `Qwen/Qwen3-Embedding-0.6B` | 1024, 384, 384, 1024 |

The Snowflake names are lower-case (`snowflake/…`) although the Hugging Face repositories are `Snowflake/…`. No query prefix or instruction is added to any text: texts are embedded as given. Quantized variants of a model are not separate names. The earlier fastembed runtime accepted more names (41 embedding models) than this list: a collection or `[database].embedding_model` set to a name that is not listed fails with the error above. To move such a collection to a listed model, use the `PATCH` state machine above; a collection that already holds data needs a reindex.

Vectors an earlier release stored stay usable: the opt-in `runtime_parity` test lane checks every model against frozen outputs of the previous runtime (cosine at least 0.99994). Six models (`bge-small-en-v1.5`, `bge-base-en-v1.5`, `paraphrase-multilingual-MiniLM-L12-v2`, `bge-small-en`, `bge-base-en`, `bge-small-zh-v1.5`) differ slightly from the old vectors because fastembed's optimized graphs used the tanh GELU approximation, not because of quantization — see `tests/fixtures/runtime_parity/README.md`. Rerankers are listed under `reranker_model` in [`30_configuration.md`](./30_configuration.md#database).

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
