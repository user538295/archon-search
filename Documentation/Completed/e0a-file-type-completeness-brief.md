# Feature Brief: E0a — File-Type Completeness

## Problem
Users ingesting `.doc`, `.xls`, `.ppt`, `.odt`, `.rtf`, `.epub`, `.eml`, or `.msg` files get a cryptic `ParseError` wrapping a `ModuleNotFoundError`. Developers who follow the documented `uv sync --dev` path also get this error on `.docx`, `.pptx`, and `.xlsx` because `markitdown` is absent from `pyproject.toml`. The parser advertises Office support; the install doesn't deliver it.

## Goal
Every common document format ingests successfully on a fresh install with no extra steps. A user who runs `archon-search ingest document.doc` gets indexed content, not an error.

## Users & Context
- End users with existing document libraries (corporate, academic, personal) — most have a mix of legacy `.doc`/`.xls`/`.ppt` alongside modern formats.
- Developers setting up a dev environment via `uv sync --dev` who try to ingest an Office file and hit a silent import failure.
- Personal knowledge-base users ingesting ebooks (`.epub`) and email exports (`.eml`, `.msg`).

## Core Flow
1. User runs `archon-search ingest /path/to/file.doc` (or any newly-supported extension).
2. Parser detects the extension, routes to `_parse_office`.
3. `markitdown` converts the file to text (no import error — it's a declared core dep).
4. Text is chunked, embedded, and indexed normally.
5. Ingest result reports `file_type: .doc` and chunk count.

For `.tsv`: routed to `_parse_plain` (plain-text fallback already handles it correctly — just add it to `_PLAIN_EXTENSIONS` so it is explicit rather than falling through as "unknown").

## In Scope
- Add `markitdown` to `[project.dependencies]` in `pyproject.toml` (core, not optional).
- Add to `_OFFICE_EXTENSIONS` in `parser.py`: `.doc`, `.xls`, `.ppt`, `.odt`, `.rtf`, `.epub`, `.eml`, `.msg`.
- Add `.tsv` to `_PLAIN_EXTENSIONS` in `parser.py`.
- Replace the bare `except Exception` in `_parse_office` with an `ImportError` branch that surfaces a human-readable message ("install markitdown: `pip install markitdown`") before the generic `ParseError` fallback — defence against future dep-slip.
- Update `parser.py` module docstring to reflect new supported types.
- Update `Documentation/UserManual/` (ingest guide) to list all supported extensions.

## Out of Scope
- Audio transcription (`.mp3`, `.wav`) — requires a separate ML model (Whisper); separate brief.
- Image-only PDFs or scanned documents beyond current docling OCR coverage — no change to `_parse_pdf`.
- Animated `.gif`, `.svg` — already intentionally excluded; no change.

## Key Decisions
- **markitdown as core dep, not optional extra**: The parser already advertises Office support in its module docstring and `_OFFICE_EXTENSIONS`. Marking it optional while advertising it as a first-class feature is a broken contract. The install size cost (~1 MB) is negligible.
- **No new library for any format**: All newly-supported types are handled by `markitdown`, which is already the declared dep for Office. Zero new dependencies.
- **`.tsv` via plain-text, not CSV parser**: TSV is tab-separated text; the plain-text reader produces searchable content without any parsing overhead.

## Edge Cases & Constraints
- **markitdown conversion failure on malformed files**: Already handled — `_parse_office` wraps the `MarkItDown().convert()` call in `except Exception as exc: raise ParseError(path, exc)`. No change needed.
- **`.odt` with embedded images**: markitdown extracts text and ignores binary blobs; expected behaviour, no special handling needed.
- **`.eml` with HTML body**: markitdown strips HTML tags and returns plain text; acceptable for search indexing.
- **`.msg` (Outlook) on non-Windows systems**: markitdown uses `extract-msg` which is pure Python; works cross-platform.
- **Existing `.docx`/`.pptx`/`.xlsx` users**: Adding markitdown as a core dep is fully backward-compatible; behaviour is unchanged for existing supported types.
- **Wizard install**: The wizard already installs markitdown via pip (`install.py:1296`). Declaring it in `pyproject.toml` makes `uv sync` consistent with the wizard. No wizard change required.

## Open Questions
- None. All decisions are resolved by the investigation.

## Future Iterations
- Audio ingestion (`.mp3`, `.wav`, `.m4a`) via Whisper — requires opt-in model download, separate brief.
- Source-aware chunking for email (thread metadata, sender/recipient as filterable fields) — needs metadata schema extension.

## Recommendation
Ship this immediately. It is the highest ROI item in the entire E0 catalogue: ~10 lines of production code (8 extension strings + 1 dep declaration + 1 error message improvement), zero new dependencies, and it eliminates the single most surprising failure mode in the product — "I thought you supported Office files." The markitdown dep fix alone unblocks every developer who followed the documented setup path.
