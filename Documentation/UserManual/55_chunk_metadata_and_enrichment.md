**Purpose**: Explain what metadata every chunk carries, where it comes from, how you see it, and how you use it.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Chunk metadata and enrichment

Every chunk archon-search stores carries metadata beyond its text. Some fields are
**core schema fields** populated on every ingest (file type, timestamps, who ingested it,
ACL). Others are **auto-enrichment fields** added only when the source type supports them
(markdown headings, PDF page numbers, code symbols). This guide covers what each field is,
where it comes from, how it surfaces in search results and MCP, and how to backfill it on
collections ingested before these fields existed.

## Core schema fields (every chunk)

Wired end-to-end at ingest (parser → chunker → store) and returned on the top level of every
search result. Verified against `archon_search/store.py:_schema()` and
`archon_search/server/routes_search.py:SearchResultSchema`.

| Field | Meaning | Source |
|---|---|---|
| `source_path` | Absolute path of the ingested file | `parser.py` |
| `file_type` | Lowercased file extension **without** the leading dot (`md`, `py`, `pdf`; `""` for no extension). No alias collapsing — `foo.md` → `md`, never `markdown`. | `Path(source).suffix` |
| `language` | Reserved at the storage layer; stays on the chunk record. Filterable, but not populated by ingest yet (deferred to language detection). | storage-only |
| `indexed_at` | When the chunk was written to the index (ISO 8601 UTC) | store write time |
| `updated_at` | Source file mtime as ISO 8601 UTC; falls back to `indexed_at` when `stat()` is unavailable (stdin, vanished file) | `path.stat().st_mtime` |
| `ingested_by` | Call site that ingested the chunk: `cli`, `http`, `watcher`, or `reindex`. The legacy value `archon-search-cli` is normalized to `cli` at the read boundary. | ingest entry point |
| `acl` | Allowed principals for the chunk (from frontmatter, a sidecar, or the collection default). See the [Security guide](../SecurityGuide/03_authorization_and_acl.md). | `acl.py` |
| `metadata` | Free-form `dict[str, str]` of custom key/values supplied at ingest **plus** all auto-enrichment fields below. Bounded by max fields / key length / value length. | caller + enrichers |

The auto-enrichment fields live **inside** the free-form `metadata` dict (their keys start with
an underscore), not as top-level result fields.

## Auto-enrichment fields (by source type)

Enrichment runs per chunk right after chunking (`pipeline.py`; `enricher.py`,
`code_enricher.py`). Which fields appear depends entirely on the source type — a chunk only
gets the fields its content supports.

### Markdown structural context (C3a)

Added to chunks from markdown files. Verified in `archon_search/enricher.py`.

| Metadata key | Meaning |
|---|---|
| `_heading` | Nearest enclosing heading text for the chunk (truncated). Empty string when no heading precedes the chunk. |
| `_section_path` | Breadcrumb of ancestor headings down to the chunk's section (e.g. `Guide > Install > macOS`). |

### PDF / image page numbers (C3b)

Added to chunks parsed from PDFs and OCR'd images. Verified in `archon_search/enricher.py`.

| Metadata key | Meaning |
|---|---|
| `_page_start` | Page number (1-based) where the chunk begins. |
| `_page_end` | Page number where the chunk ends — written **only** when it differs from `_page_start` (single-page chunks omit it). |

### Code symbol context (C3c)

Added to chunks from source files, via tree-sitter. Supported extensions:
`.py .ts .js .go .rs .java .sh .swift .cs` (`code_enricher.py:CODE_EXTENSIONS`). Requires the
tree-sitter grammars (the `archon-search[code]` extra); if a grammar is missing, the file is
still ingested but a WARNING is logged and a per-file note is added to `IngestResult.warnings`.

| Metadata key | Meaning |
|---|---|
| `_symbol_type` | Kind of the enclosing symbol (`function`, `class`, `module`, …). |
| `_symbol_subtype` | Language-qualified subtype, e.g. `python-function`, `rust-module`. |
| `_containing_function` | Name of the function the chunk sits in (empty if none). |
| `_containing_class` | Name of the enclosing class (empty if none). |
| `_module_path` | Import-style module path derived from the file's location relative to the collection root (e.g. `pkg.sub.mod`). |

These same code fields feed the code graph — see
[Code graph and impact](70_code_graph_and_impact.md).

## How you SEE metadata

### In `/search` results

Core fields are always on each result. The `metadata` dict (which holds the enrichment keys)
is returned **only when you ask for it** — set `filters.include_metadata = true`, otherwise the
server blanks `metadata` to keep payloads small (`routes_search.py`).

```bash
curl -s http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "collection": "docs",
        "query": "install on macOS",
        "filters": { "include_metadata": true }
      }'
```

Each result then carries `source_path`, `file_type`, `language`, `indexed_at`, `updated_at`,
`ingested_by`, `acl`, and a populated `metadata` dict:

```json
{
  "source_path": "/home/me/docs/install.md",
  "file_type": "md",
  "updated_at": "2026-07-20T11:03:22.000000Z",
  "ingested_by": "cli",
  "metadata": {
    "_heading": "macOS",
    "_section_path": "Guide > Install > macOS"
  }
}
```

`GET /openapi.json` is the authoritative field list — consult it rather than memorizing this
example.

### Via MCP

The MCP `search` and `search_with_context` tools return the same core fields (MCP wraps the
same pipeline). `list_documents`, `get_collection_meta`, and `get_collections_meta` expose
per-document / per-collection metadata for browsing a corpus without running a query. See
[MCP integration](../DeveloperGuide/05_mcp_integration.md).

## How you USE metadata

### Metadata and query filters at search

`POST /search` accepts a `filters` block that narrows results before ranking. Verified fields
(`archon_search/filters.py:SearchFilters`):

- `file_type` — exact stored extension, no dot (`"md"`, `"py"`).
- `language` — ISO language code.
- `source_path_prefix` / `source_path_glob` — path prefix or glob.
- `indexed_after` / `indexed_before` — date or datetime bounds on `indexed_at`.
- `include_metadata` — return the enrichment `metadata` dict (above).

Full filter syntax and worked examples live in [Searching](60_searching.md).

### Scope filters

`scope_filter` on `/search` is a separate axis from `filters` — it gates results by scope
labels (exact scopes are pushed into SQL; trailing-`*` wildcards are post-filtered). Scoping and
TTL are covered in [TTL and scoping](130_ttl_and_scoping.md).

## Backfilling metadata on existing collections

Collections ingested before these fields existed have empty `file_type`, missing `updated_at`,
and legacy `ingested_by`. Backfill is **opt-in** — nothing rewrites your data automatically.

Re-derive `file_type` from each `source_path`, refresh `updated_at` from the current file mtime
(preserving chunks whose source file has vanished), and rewrite legacy `ingested_by` to
`reindex`:

```bash
# CLI — proxies POST /collections/{name}/reindex-metadata (server must be running)
archon-search collection reindex-metadata docs --wait

# Preview counts without writing (--wait surfaces the counts; without it you get only a job id)
archon-search collection reindex-metadata docs --dry-run --wait
```

Flags (`archon_search/cli/collection.py`):

- `--dry-run` — report counts, write nothing.
- `--normalize-timestamps / --no-normalize-timestamps` — rewrite `indexed_at` / `updated_at` to
  fixed-width UTC (default on).
- `--wait` — poll until the job finishes.
- `--api-url` / `--api-key` — target server and credentials.

Or drive the endpoint directly:

```bash
curl -s -X POST \
  http://127.0.0.1:8765/collections/docs/reindex-metadata \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false, "normalize_timestamps": true}'
```

The route runs asynchronously as a `MetadataReindexJob` and returns `202` with a job ID; track
it via [Jobs and async operations](100_jobs_and_async_operations.md). A `409` means a metadata
reindex is already in progress for that collection.

Note: `reindex-metadata` backfills the **core** fields only. The structural / page / code
enrichment fields (`_heading`, `_page_start`, `_symbol_type`, …) are computed at chunk time — the
only way to add them to an already-ingested collection is to re-ingest.

## Related documents

- [Ingestion and collections](50_ingestion_and_collections.md)
- [Searching](60_searching.md)
- [Code graph and impact](70_code_graph_and_impact.md)
- [TTL and scoping](130_ttl_and_scoping.md)
- [UserManual index](00_index.md)
