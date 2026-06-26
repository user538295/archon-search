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
2. Parser opens the file. docling converts page-by-page, yielding text incrementally.
3. Each yielded page batch is chunked and emitted to the store without holding the full document in memory.
4. Ingest completes; CLI reports total pages and chunks indexed.

### File exceeds configurable size guard
1. Operator sets `[ingest].max_file_mb = 100` in `archon-search.toml`.
2. User attempts to ingest a 150 MB PDF.
3. Ingest returns HTTP 413 (or CLI equivalent) with: "File size 150 MB exceeds the configured limit of 100 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."
4. Job is not created; no partial indexing occurs.

### No size guard set (default)
1. `[ingest].max_file_mb` defaults to `0` (disabled — no size guard).
2. All file sizes accepted; memory usage is bounded by the streaming page-batch approach.

## In Scope
- **Streaming / incremental docling conversion**: Modify `_parse_pdf` in `parser.py` to yield text page-by-page (or in configurable page-batch sizes) rather than converting the full document to a string before returning. Coordinate with D4 (`streaming-incremental-chunking`) which already targets the same ingest pipeline path.
- **`[ingest].max_file_mb` config field**: New field in `IngestConfig` (or a new `[ingest]` section). Default `0` = no limit. Applied at the start of `parse()` before docling is invoked. File size check is a single `os.path.getsize()` call.
- **413-equivalent error**: When the size guard triggers, return an `IngestError` with `code="file_too_large"` and a human-readable message that names both the file size and the configured limit and tells the user how to fix it.
- **CLI message for page progress**: For large files (> 10 MB or > 50 pages), `archon-search ingest` emits progress: "Parsing page 45/200…" so the user knows the process is alive.
- **Update `Documentation/UserManual/`** to document the size guard config and remove the implicit 1 MB limitation.

## Out of Scope
- Splitting PDFs at the file level before ingestion — that is a user-side operation; the server handles any size up to the guard.
- Password-protected PDFs — docling either handles them or raises; no change in behaviour.
- Image-only PDFs (scanned documents) beyond current docling OCR support — no change to OCR behaviour.
- Streaming HTTP upload of PDFs via multipart — ingest operates on filesystem paths, not HTTP uploads; no change to the transport layer.
- Parallelising page conversion across multiple threads — out of scope; single-threaded docling conversion per file is acceptable.

## Key Decisions
- **Default `max_file_mb = 0` (no limit)**: The right default for a server tool is "accept what the operator's hardware can handle," not "silently cap at an arbitrary size." Operators on constrained hosts set the guard explicitly.
- **Size guard at `parse()` entry, not at the route layer**: The check belongs at the parser boundary — it applies whether ingestion comes from REST, MCP, CLI, or the watcher. A route-layer check would miss the watcher and CLI paths.
- **Coordinate with D4, not replace it**: D4 (`streaming-incremental-chunking`) targets the embed+write pipeline; E0d targets the parse stage. They are complementary. E0d's streaming parse feeds naturally into D4's streaming chunk emission. If D4 is not yet shipped, E0d can ship with a "parse fully, then stream chunks" approach as an intermediate step — the key fix is removing the memory materialisation of the raw text string before chunking starts.
- **`IngestError` with `code="file_too_large"`**: Consistent with the existing error taxonomy in `Architecture/140_error_handling_strategy.md`. Maps to HTTP 413 at the REST layer.

## Edge Cases & Constraints
- **docling page-by-page API availability**: Verify that the installed `docling >= 2.80.0` version exposes a page-streaming or batch API. If not, the intermediate approach is: convert to markdown string (existing behaviour), then stream the string into the chunker in batches rather than holding chunked output in memory. The memory win is smaller but the fix is forward-compatible.
- **`max_file_mb` check on symlinks**: Use `os.path.getsize()` which follows symlinks — correct behaviour (the actual file size is what matters).
- **Watcher-triggered ingest**: The file-size guard must apply in `watcher.py`'s ingest dispatch path, not only in the REST/MCP handler. The guard lives in `pipeline.ingest_file()` to ensure universal application.
- **Progress reporting for async jobs**: For REST/MCP ingest (async job model), page-level progress updates write to `job.progress` field. CLI `--wait` polls and displays the progress field. No new protocol needed.
- **Very large PDFs (> 1 GB)**: The streaming approach keeps per-file memory proportional to a single page batch, not the file size. Test with a representative 500-page, 100 MB PDF as the acceptance benchmark.

## Open Questions
- **docling streaming API**: Does `docling >= 2.80.0` expose an incremental page converter, or does it materialise the full document internally regardless? If the latter, the memory benefit of streaming at the `_parse_pdf` layer is limited; the real fix is in D4's chunker streaming. Planning must verify this against the installed docling version before deciding implementation strategy.

## Future Iterations
- **Per-collection `max_file_mb` override**: Allows a "large documents" collection to accept bigger files than the global guard.
- **Background conversion for very large PDFs**: Move docling conversion off the request thread into a background task for files > X MB, returning a job ID immediately.

## Recommendation
This is the original motivating bug: "we support PDF ingestion but there is a 1 MB limitation which is ridiculous." It must ship. The Open Question about docling's streaming API is the only real unknown — if docling materialises the full document internally, the memory fix lands in D4 rather than here, and E0d's contribution is the size guard and the clear error message. Either way, the user-facing improvement (no more silent failures on large PDFs, actionable error when a guard is set) ships with E0d. Coordinate the implementation strategy with D4 before writing the plan.
