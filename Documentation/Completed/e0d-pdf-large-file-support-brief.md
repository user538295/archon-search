# Feature Brief: E0d — PDF Large-File Support

## Problem
PDF ingestion has an effective ~1 MB size limit. It is not a named constant — it emerges from the combination of the FastAPI/uvicorn default request-body budget and docling's full-document materialisation in memory. Users with real-world PDFs (research papers, financial reports, technical manuals, ebooks) hit a generic `ParseError` or a silent timeout with no actionable message. The product advertises PDF support; it does not deliver it for the most common PDF sizes.

## Goal
PDF files of any practical size ingest successfully. A user who ingests a 50-page financial report or a 500-page technical manual gets indexed content, not an error. If a file exceeds an operator-configured size guard, they get a clear, actionable error — not a silent timeout.

## Users & Context
- **Knowledge-base builders** ingesting research papers (2–20 MB), books (5–50 MB), corporate reports (1–30 MB).
- **Operators** on memory-constrained hosts (1–2 GB RAM containers) who need a hard file-size guard to protect the process.
- **Developers** testing ingestion pipelines who currently route around the limit with manual chunking.

## Core Flow

### Happy path — large PDF ingests successfully
1. User runs `archon-search ingest /path/to/200-page-manual.pdf`.
2. `pipeline.ingest_file()` checks file size via `os.path.getsize()` — no limit configured, so it proceeds.
3. docling `convert()` fully materialises the document internally (no page-by-page streaming API; memory reduction is D4's scope).
4. Ingest completes; CLI reports chunks indexed.

### File exceeds configurable size guard
1. Operator sets `[ingest].max_file_mb = 100` in `archon-search.toml`.
2. User attempts to ingest a 150 MB PDF.
3. Ingest returns HTTP 413 (or CLI equivalent) with: "File size 150 MB exceeds the configured limit of 100 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
4. Job is not created; no partial indexing occurs.

### No size guard set (default)
1. `[ingest].max_file_mb` defaults to `0` (disabled — no size guard).
2. All file sizes accepted. Memory during docling conversion is bounded by the document size (no streaming; see D4 for memory reduction).

## In Scope
- **`[ingest].max_file_mb` config field**: New `[ingest]` TOML section with `max_file_mb: int` (default `0` = no limit). Guard applied via a shared `_file_exceeds_limit(path, max_file_mb) -> bool` helper at two levels: (1) `pipeline.ingest_file()` for universal coverage; (2) `POST /ingest` route handler as a synchronous pre-check before job creation for HTTP 413.
- **413-equivalent error**: When the size guard triggers, return an `IngestError` with `code="file_too_large"` and a human-readable message that names both the file size and the configured limit and tells the user how to fix it.
- **CLI pre-parse large-file notice**: For files > 10 MB, print to stderr before calling `convert()`: "Parsing large file (X MB); this may take a while…". This replaces per-page progress reporting, which is infeasible with the installed docling (see Open Questions).
- **CLI single-file ingest mode**: `--path` routing to a file → `pipeline.ingest_file()` (collection name = `Path(path).stem`).
- **Update `Documentation/UserManual/`** to document the size guard config and remove the implicit 1 MB limitation.

## Out of Scope
- Splitting PDFs at the file level before ingestion — that is a user-side operation; the server handles any size up to the guard.
- Password-protected PDFs — docling either handles them or raises; no change in behaviour.
- Image-only PDFs (scanned documents) beyond current docling OCR support — no change to OCR behaviour.
- Streaming HTTP upload of PDFs via multipart — ingest operates on filesystem paths, not HTTP uploads; no change to the transport layer.
- Parallelising page conversion across multiple threads — out of scope; single-threaded docling conversion per file is acceptable.

## Key Decisions
- **Default `max_file_mb = 0` (no limit)**: The right default for a server tool is "accept what the operator's hardware can handle," not "silently cap at an arbitrary size." Operators on constrained hosts set the guard explicitly.
- **Dual-guard design — pipeline + route**: The guard lives at two levels. (1) `pipeline.ingest_file()` is the universal chokepoint — it returns an error `IngestResult` for REST job workers, MCP, CLI single-file, and watcher paths. (2) `POST /ingest` adds a synchronous pre-check before `job_store.create()` to return HTTP 413 immediately for single-file REST requests; the 413 is the semantically correct response and leaving no job in the store makes it predictable. A shared `_file_exceeds_limit()` helper ensures both sites use identical boundary semantics (strictly greater-than). Directory paths and `body.documents` payloads skip the route-level 413 check — oversized files in those paths surface as per-file error `IngestResult` inside the job.
- **docling conversion is unchanged**: docling `convert()` fully materialises each document. E0d does not modify `parser.py` — the size guard fires before `convert()` is called, preventing OOM for guarded sizes. Memory reduction for unguarded large PDFs is D4's scope.
- **`IngestError` with `code="file_too_large"`**: Consistent with the existing error taxonomy in `Architecture/140_error_handling_strategy.md`. Maps to HTTP 413 at the REST layer.

## Edge Cases & Constraints
- **`max_file_mb` check on symlinks**: Use `os.path.getsize()` which follows symlinks — correct behaviour (the actual file size is what matters). Note: `ingest_directory()` skips symlinks before the guard is reached; symlink checking only applies to `ingest_file()` direct calls (REST single-file path, watcher events, CLI single-file mode).
- **Watcher-triggered ingest**: The file-size guard applies automatically via `pipeline.ingest_file()` — `sync.py` calls it directly for file-changed/created events. No watcher code changes needed.
- **Directory ingest with oversized files**: `ingest_directory()` continues processing other files; oversized files produce per-file `IngestResult(status="error", code="file_too_large")`. The batch job does not fail-fast.
- **Very large PDFs (> 1 GB)**: Accept without error when `max_file_mb=0`. Memory during conversion is bounded by the document size (docling materialises fully). Test with a 500-page, ~100 MB PDF as the acceptance benchmark. Memory relief for very large PDFs is D4's scope.

## Open Questions

_All resolved as of 2026-06-27._

- **docling streaming API** ✅ Resolved (docling 2.102.2, verified 2026-06-27): docling does **not** expose a per-page streaming API. `convert()` materialises each document fully internally; `page_range` controls which pages are exported, not parsed. Implementation strategy: **Option B** — our own `os.path.getsize()` pre-check before invoking `convert()`, giving full control over the error message. Memory reduction for very large PDFs remains a D4 concern (chunker streaming). `convert()` natively accepts `max_file_size` but we own the guard for message quality.

## Future Iterations
- **Per-collection `max_file_mb` override**: Allows a "large documents" collection to accept bigger files than the global guard.
- **Background conversion for very large PDFs**: Move docling conversion off the request thread into a background task for files > X MB, returning a job ID immediately.

## Recommendation
This is the original motivating bug: "we support PDF ingestion but there is a 1 MB limitation which is ridiculous." It must ship. The Open Question about docling's streaming API is the only real unknown — if docling materialises the full document internally, the memory fix lands in D4 rather than here, and E0d's contribution is the size guard and the clear error message. Either way, the user-facing improvement (no more silent failures on large PDFs, actionable error when a guard is set) ships with E0d. Coordinate the implementation strategy with D4 before writing the plan.
