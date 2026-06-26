**Purpose**: Catalogue every confirmed user-facing limitation and file-type gap found by a systematic codebase audit. This is the index document — each sub-item has its own actionable brief.
**Audience**: Maintainers planning E-series work.
**Status**: Backlog — refined into sub-briefs below
**Last reviewed**: 2026-06-26
**Next review**: 2026-09-30

# E0 — UX Limitations and File-Type Gap Audit

## Sub-briefs (refined, ready for `/plan-maker`)

| ID | Title | Scope | Brief |
|---|---|---|---|
| E0a | File-Type Completeness | markitdown as core dep + 8 missing extensions | [e0a-file-type-completeness-brief.md](e0a-file-type-completeness-brief.md) |
| E0b | Silent Failure Transparency | HyDE/RAG Fusion fallback, ANTHROPIC_API_KEY, FAILED_EXPIRED state, telemetry truncation, ACL warning, --wait timeout | [e0b-silent-failure-transparency-brief.md](e0b-silent-failure-transparency-brief.md) |
| E0c | API Surface Fixes | list_documents pagination, max_fanout tracks config, top_k operator cap, description sampling | [e0c-api-surface-fixes-brief.md](e0c-api-surface-fixes-brief.md) |
| E0d | PDF Large-File Support | Streaming PDF ingest, configurable max_file_mb guard, clear 413-style error | [e0d-pdf-large-file-support-brief.md](e0d-pdf-large-file-support-brief.md) |
| E0e | Multi-Collection Filters | Lift v1 restriction; per-leg filter injection in search_many(); MCP parity | [e0e-multi-collection-filters-brief.md](e0e-multi-collection-filters-brief.md) |
| — | Windows Service Management | Implement start/stop/register/unregister on Windows | Deferred — platform-specific sprint |

---

# E0 — UX Limitations and File-Type Gap Audit

This document is the output of a full static audit of the `archon-search` codebase. It identifies (a) hardcoded or structural limits that users hit unexpectedly and (b) file types supported by comparable tools (R2R) that archon-search does not handle. Nothing here is speculative — every item is traced to a source file and line number.

---

## Part 1 — User-Facing Limitations

Items are grouped by severity. "Severe" = user loses work or hits a wall with no workaround. "Moderate" = user is surprised but can work around it. "Minor" = polish.

### Severity: Severe

#### L1 — PDF ingest silently fails on files larger than ~1 MB

**Source**: `Documentation/archon-search-notes.md:7`; `archon_search/parser.py:113` (`_parse_pdf`)

The effective 1 MB limit on PDF ingestion is not a named constant — it is an emergent behaviour from the combination of the FastAPI/uvicorn default request-body budget and docling's document-converter memory characteristics. The user manual documents PDF support without qualification. Users with real-world PDFs (reports, papers, books) hit a ParseError or a silent timeout with no actionable message.

**Acceptance criteria for fix**: PDF files of any size ingest correctly; the parser streams or pages through docling's converter without holding the full document in memory; a clear `413`-style error is returned for files exceeding a configurable max (e.g. `[ingest].max_file_mb`, default uncapped) rather than a generic ParseError. See also `Documentation/Completed/D4-streaming-incremental-chunking-brief.md:87` which already flags a "file-size guard" as a follow-on item.

---

#### L2 — Multi-collection search silently rejects all filter parameters (v1 restriction)

**Source**: `archon_search/server/routes_search.py:89`

```python
raise HTTPException(status_code=400, detail="filters are not supported for multi-collection search in v1")
```

Any `POST /search` request that specifies both a `collections` list and any `filters` field returns HTTP 400. The OpenAPI schema does not prevent the combination; the user only discovers the restriction at runtime. For users who rely on `source_path_prefix`, `language`, or `file_type` filters to scope results, multi-collection search is effectively unusable.

**Acceptance criteria for fix**: Filters (at minimum `file_type`, `source_path_prefix`, `language`) are applied per-collection leg during fan-out, and the restriction is either lifted or prominently documented with a timeline commitment.

---

#### L3 — Windows service management raises NotImplementedError on every call

**Source**: `archon_search/platform/windows.py:11–23`

Every platform lifecycle method (`start`, `stop`, `restart`, `register`, `unregister`) raises:

```
NotImplementedError: Windows service management not yet supported — run archon-search start manually
```

There is no degraded mode. Windows users who run `archon-search start` get an uncaught exception with no recovery path. The README and install docs do not call out this limitation.

**Acceptance criteria for fix**: Either implement Windows service management (NSSM or Task Scheduler), or surface a clear, friendly error at `archon-search install` time on Windows that explains what to do instead, and document it in `Documentation/UserManual/`.

---

#### L4 — `list_documents` hard-capped at 1 000 with no pagination cursor

**Source**: `archon_search/store.py:2025`

```python
limit = min(limit, 1000)
```

There is no cursor, offset, or token-based pagination. A collection with 5 000 documents silently returns the first 1 000 with no indication that results were truncated. Users cannot page through a large collection programmatically.

**Acceptance criteria for fix**: `list_documents` gains a `cursor` / `offset` parameter or keyset pagination; the response includes a `next_cursor` / `has_more` field so clients know when results are complete. The 1 000 hard cap becomes a page-size maximum, not a total cap.

---

#### L5 — `markitdown` is not declared in `pyproject.toml`; Office file parsing silently broken on dev installs

**Source**: `archon_search/parser.py:119–124`; `pyproject.toml` (absent)

The wizard installs `markitdown` via pip at `archon_search/install.py:1296`, but `pyproject.toml` lists it nowhere in `[project.dependencies]` or `[project.optional-dependencies]`. Developers who follow the documented `uv sync --dev` setup path do not get `markitdown`. Any attempt to ingest `.docx`, `.pptx`, or `.xlsx` raises a generic `ParseError` with a `ModuleNotFoundError` buried inside — no actionable message.

**Acceptance criteria for fix**: `markitdown` is added as a core dependency in `pyproject.toml` (or as an `office` optional extra with a clear install message); `_parse_office` raises an `ImportError`-derived exception with a "install archon-search[office]" hint when the package is absent.

---

### Severity: Moderate

#### L6 — `ANTHROPIC_API_KEY` is not forwarded by the launchd/systemd service templates

**Source**: `archon_search/platform/macos.py:33–37`; `archon_search/platform/linux.py:26`; `Documentation/archon-search-notes.md` (Anthropic key section)

The managed service templates do not include `EnvironmentVariables` / `Environment=` entries for `ANTHROPIC_API_KEY`. After `archon-search start`, HyDE and RAG Fusion silently fall back (no query expansion) without any error or warning surfaced to the user. The user manual documents HyDE/RAG Fusion but does not explain that they stop working under the managed service unless the key is manually added to the service file.

**Acceptance criteria for fix**: The install wizard either (a) injects `ANTHROPIC_API_KEY` into the service template when the key is present at install time (via the `~/.archon-search/.secrets.env` EnvironmentFile pattern already noted in the notes doc), or (b) prints a prominent post-install warning when HyDE/RAG Fusion were enabled but the key will not be forwarded. `GET /status` should surface a `hyde.key_available: false` / `rag_fusion.key_available: false` field so operators can diagnose this remotely.

---

#### L7 — HyDE and RAG Fusion silently time out after 5 seconds with no user signal

**Source**: `archon_search/config.py:28, 36`

```toml
[hyde]
timeout_seconds = 5.0

[rag_fusion]
timeout_seconds = 5.0
```

5 seconds is below typical Anthropic API p95 latency during peak hours. On timeout the search falls back to non-expanded retrieval without indicating this in the response. The user sees fewer-relevant results and has no way to know the expansion stage was skipped.

**Acceptance criteria for fix**: `SearchResponse` gains an `expansion_used: bool` field and an `expansion_warning: str | null` field; timeout default raised to at least 10 seconds (configurable); the existing `TOML` knob (`timeout_seconds`) is documented in the user manual.

---

#### L8 — `--wait` for `maintenance run` and `export` times out at exactly 2 minutes

**Source**: `archon_search/cli/maintenance_cmd.py:29`

```python
_WAIT_MAX_POLLS = 60  # × 2 s poll = 120 s max
```

Large collections (100k+ chunks) take longer than 2 minutes to maintain or export. The CLI exits with a non-completion state that is indistinguishable from failure. There is no `--timeout` override.

**Acceptance criteria for fix**: `--wait` accepts an optional `--timeout SECONDS` (default 120, configurable); on expiry, the CLI prints "still running — job ID is X, poll with `archon-search maintenance status`" and exits 0 rather than implying failure.

---

#### L9 — `max_fanout` is hard-capped at 8 in API validation independently of TOML config

**Source**: `archon_search/server/routes_search.py:35`

```python
_FANOUT_VALIDATION_LIMIT = 8
```

This is a Pydantic-layer constant separate from `SearchConfig.max_fanout`. Raising `max_fanout` in `archon-search.toml` beyond 8 has no effect — the API rejects the request before the config value is consulted. The two caps are out of sync and the discrepancy is invisible to the operator.

**Acceptance criteria for fix**: `_FANOUT_VALIDATION_LIMIT` is removed; the Pydantic validator reads `max_fanout` from the loaded config at app startup (injected via `app.state`) so the API limit tracks the config value. A startup warning is logged when `max_fanout > 32` (reasonable sanity cap).

---

#### L10 — Failed ingest jobs are silently abandoned after 72 hours

**Source**: `archon_search/config.py:80`

```toml
[maintenance]
retry_max_age_hours = 72
```

After 3 days, failed `IngestJob`s age out of the retry queue with no notification to the user. A document that failed to ingest on Friday disappears from the retry queue by Monday with no trace in the UI or logs beyond DEBUG-level entries in the maintenance loop.

**Acceptance criteria for fix**: `GET /jobs` and `archon-search status` surface `FAILED_EXPIRED` as a terminal job state; the CLI prints a count of aged-out jobs with a "re-ingest with `archon-search ingest`" hint on `archon-search status`.

---

#### L11 — Telemetry entries silently dropped when serialised size exceeds 8 KB

**Source**: `archon_search/telemetry/writer.py:34`

```python
MAX_ENTRY_BYTES = 8192
```

A search that returns many results with long source paths can produce a telemetry entry larger than 8 KB. The entry is dropped entirely without incrementing any counter or setting a warning flag. Telemetry stats therefore undercount queries silently.

**Acceptance criteria for fix**: Oversized entries are truncated (e.g. `result_doc_ids` list shortened) rather than dropped; a `truncated: true` flag is set on the entry; `GET /telemetry/stats` includes a `truncated_count` field.

---

### Severity: Minor

#### L12 — Collection description samples only the first 20 chunks (insertion-order bias)

**Source**: `archon_search/description_generator.py:27`

```python
_MAX_SAMPLE_CHUNKS = 20
```

For large heterogeneous collections, the auto-generated description reflects whatever was ingested first. Re-ingesting a large PDF corpus will produce descriptions that ignore later-ingested content. There is no "force resample" CLI flag.

**Note**: `Documentation/Completed/D4-streaming-incremental-chunking-plan.md:42` already notes this as a known limitation and suggests `ORDER BY RANDOM()` as the upgrade path.

---

#### L13 — `top_k` API hard maximum of 100 results per query

**Source**: `archon_search/server/routes_search.py:41`

```python
top_k: int = Field(default=5, ge=1, le=100)
```

100 is not configurable by the operator. Bulk evaluation use-cases (fetching all plausibly relevant chunks for a downstream ranker) are blocked.

---

#### L14 — ACL sidecar files silently ignored above 64 KB

**Source**: `archon_search/acl.py:11`

```python
_ACL_SIDECAR_MAX_BYTES = 65536
```

When an `.archon-acl` file exceeds 64 KB, it is silently ignored — the document is ingested as if it had no ACL. No warning is surfaced to the user in the ingest response.

**Acceptance criteria for fix**: A `ParseWarning` (non-fatal) is attached to the `IngestResult` when the ACL file is skipped due to size; the limit is configurable.

---

## Part 2 — File-Type Gaps vs R2R

R2R (SciPhi-AI/R2R) is the primary competitive benchmark named in `Documentation/archon-search-notes.md`. The following types are supported by R2R but not archon-search.

### Critical gap: Legacy Office formats

| Extension | Type | R2R parser | archon-search today | Effort |
|---|---|---|---|---|
| `.doc` | Legacy Word (pre-2007) | `DOCParser` | ❌ ParseError (or plain-text garbage) | **Zero new deps** — markitdown already handles `.doc`; add to `_OFFICE_EXTENSIONS` in `parser.py` |
| `.xls` | Legacy Excel (pre-2007) | `XLSParser` | ❌ ParseError | **Zero new deps** — markitdown already handles `.xls` |
| `.ppt` | Legacy PowerPoint (pre-2007) | `PPTParser` | ❌ ParseError | **Zero new deps** — markitdown already handles `.ppt` |

Legacy Office formats are pervasive in enterprise and government document stores. They represent the highest-ROI fix: three lines of code, no new dependency.

**Source**: `archon_search/parser.py:39`

```python
_OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}  # ← add .doc, .xls, .ppt
```

---

### High value: Alternate document formats

| Extension | Type | R2R parser | archon-search today | Effort |
|---|---|---|---|---|
| `.odt` | OpenDocument Text (LibreOffice) | `ODTParser` | ❌ Treated as binary (skipped or garbage) | markitdown supports `.odt` — add to `_OFFICE_EXTENSIONS` |
| `.rtf` | Rich Text Format | `RTFParser` | ❌ Treated as plain text (markup noise) | markitdown supports `.rtf` |
| `.epub` | Ebook | `EPUBParser` | ❌ Not handled | markitdown supports `.epub` |

`.odt` and `.rtf` are widely used in academic and legal contexts. `.epub` is important for personal knowledge bases (books, long-form articles).

---

### Medium value: Email formats

| Extension | Type | R2R parser | archon-search today | Effort |
|---|---|---|---|---|
| `.eml` | Email (RFC 2822) | `EMLParser` | ❌ Not handled | markitdown supports `.eml` |
| `.msg` | Outlook email | `MSGParser` | ❌ Not handled | markitdown supports `.msg` |

Email ingestion is a major use case for personal and enterprise knowledge bases. Both formats are natively handled by markitdown with no new dependency.

---

### Low value: Already work via plain-text fallback

The following types produce usable output via archon-search's plain-text fallback (unknown extensions are passed to `_parse_plain`). They are listed for completeness, not as action items.

| Extension | Type | Notes |
|---|---|---|
| `.rst` | reStructuredText | Markup is visible but content is searchable |
| `.org` | Emacs Org-mode | Markup is visible but content is searchable |
| `.css` | CSS | Code content is searchable |
| `.tsv` | Tab-separated values | Parseable as CSV with minor adaptation |

---

### Not in scope (audio)

R2R has an `AudioParser` for transcription. This requires a separate ML model (Whisper or similar) and is out of scope for E0. Tracked separately in `Documentation/archon-search-notes.md`.

---

## Summary Table

### Limitations

| ID | Severity | Description | Source |
|---|---|---|---|
| L1 | Severe | PDF ~1 MB effective size limit | `parser.py:113`, notes |
| L2 | Severe | Filters rejected on multi-collection search | `routes_search.py:89` |
| L3 | Severe | Windows service management unimplemented | `platform/windows.py:11–23` |
| L4 | Severe | `list_documents` hard cap 1 000, no cursor | `store.py:2025` |
| L5 | Severe | `markitdown` missing from `pyproject.toml` | `pyproject.toml`, `parser.py:121` |
| L6 | Moderate | `ANTHROPIC_API_KEY` not forwarded by service templates | `platform/macos.py:33–37` |
| L7 | Moderate | HyDE/RAG Fusion 5 s timeout, silent fallback | `config.py:28, 36` |
| L8 | Moderate | `--wait` hard 2-minute timeout | `maintenance_cmd.py:29` |
| L9 | Moderate | `max_fanout` API cap (8) ignores TOML config | `routes_search.py:35` |
| L10 | Moderate | Failed ingest jobs abandoned after 72 h silently | `config.py:80` |
| L11 | Moderate | Telemetry entries >8 KB silently dropped | `telemetry/writer.py:34` |
| L12 | Minor | Description samples first 20 chunks only | `description_generator.py:27` |
| L13 | Minor | `top_k` API max 100, not operator-configurable | `routes_search.py:41` |
| L14 | Minor | ACL sidecar >64 KB silently ignored | `acl.py:11` |

### File-type gaps

| Extension(s) | Priority | Effort |
|---|---|---|
| `.doc`, `.xls`, `.ppt` | Critical | 1 line — add to `_OFFICE_EXTENSIONS` |
| `.odt`, `.rtf` | High | 2 lines — add to `_OFFICE_EXTENSIONS` |
| `.epub` | High | 1 line — add to `_OFFICE_EXTENSIONS` |
| `.eml`, `.msg` | Medium | 2 lines — add to `_OFFICE_EXTENSIONS` |
| `.tsv` | Low | 1 line — add to plain-text extensions |

---

## Suggested implementation order

1. **E0a** — Fix `markitdown` in `pyproject.toml` + add legacy Office triad (`.doc`, `.xls`, `.ppt`) + add `.odt`, `.rtf`, `.epub`, `.eml`, `.msg` to `_OFFICE_EXTENSIONS`. Zero new deps, ~5 lines of production code.
2. **E0b** — Fix `_FANOUT_VALIDATION_LIMIT` to track TOML config (L9) and lift `list_documents` cap with cursor pagination (L4). No schema change required.
3. **E0c** — Surface `expansion_used`/`expansion_warning` on `SearchResponse` (L7); raise default timeouts; add `FAILED_EXPIRED` job state (L10).
4. **E0d** — PDF size limit: streaming ingest path; configurable `max_file_mb` guard with user-facing `413`-equivalent error (L1). Depends on D4 streaming work already in `Completed/`.
5. **E0e** — Multi-collection filter support (L2). Requires per-leg filter injection into `SearchPipeline.search_many()`.
6. **E0f** — Windows service management (L3). Largest standalone work item; can be deferred to a platform-specific sprint.
