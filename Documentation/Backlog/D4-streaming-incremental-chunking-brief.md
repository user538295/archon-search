# Feature Brief: D4 — Streaming / Incremental Chunking

## Problem

Ingesting large files (multi-hundred-MB PDFs, large corpora) materialises the entire parsed text, all chunks, all vectors, and all LanceDB rows as in-memory lists before anything is written to disk. On a 300-page PDF this can spike RAM by hundreds of MB; on a directory of thousands of files the `all_vectors` accumulator in `ingest_directory()` holds every vector in the corpus simultaneously. The result is unpredictable memory pressure, silent OOM kills on constrained hosts, and stalled ingests.

## Goal

A single-file ingest and a directory ingest complete successfully on a host with limited RAM (e.g., a 1 GB Docker container) regardless of document size or corpus size, by flushing embed+write batches incrementally rather than accumulating everything before the first write.

## Users & Context

Operators and developers running archon-search on memory-constrained hosts — Docker containers, small VMs, Raspberry Pi-class machines, or CI environments. They ingest large PDFs (technical manuals, research papers) or large corpora (thousands of source files) and expect the process to complete without OOM-killing the server.

## Core Flow

**Single-file ingest (post-parse):**
1. Parser runs as today — full document parsed to markdown string. Parse-time memory is owned by docling/markitdown and is not addressed here.
2. Chunker produces the full `list[ChunkRecord]` as today — Chonkie and the enrichers (MarkdownEnricher, CodeEnricher) require the full text; their two-pass design is correct and stays unchanged.
3. Pipeline slices the chunk list into fixed-size batches (constant `_INGEST_CHUNK_BATCH_SIZE = 512`).
4. For each batch: embed → assign chunk IDs and vectors → write to store. The centroid incremental update (B5) handles per-batch centroid accumulation.
5. FTS rebuild / optimize fires once after all batches complete, as today.
6. `IngestResult` is returned after the final batch.

**Directory ingest:**
1. File loop remains sequential (as today).
2. Per-file: parse → chunk → enrich → slice into batches → embed + write each batch immediately.
3. The `all_vectors` and `all_chunks` accumulators in `ingest_directory()` are **removed**. Centroid and description state are derived from the incremental B5 path (`needs_recompute` signal) instead of accumulating all vectors.
4. Description regeneration trigger (`_should_regenerate`) is checked once per file against running doc/chunk counts rather than against the full corpus accumulator.
5. FTS rebuild fires once after all files complete, as today.

## In Scope

- Add `_INGEST_CHUNK_BATCH_SIZE = 512` named constant to `constants.py` (not a config key — see Key Decisions).
- Refactor `pipeline.py::ingest_file()` to slice `records` into batches of `_INGEST_CHUNK_BATCH_SIZE` and call `store.ingest_chunks()` once per batch.
- Refactor `pipeline.py::ingest_directory()` to remove the `all_vectors` and `all_chunks` list accumulators; derive centroid and description state from the B5 incremental path and per-file `IngestResult` signals.
- Verify `store.ingest_chunks()` and `store._do_ingest()` work correctly for partial-document batches (they call `table.add(rows)` per call — already compatible; no store changes needed).
- Verify the B5 incremental centroid path (`centroid_incremental_enabled=True`, default) accumulates correctly across intra-file batches; it should since each `ingest_chunks()` call updates `centroid_sum` independently.
- Update any integration tests that assert on the call graph of `ingest_chunks()` (may now be called N times per file).

## Out of Scope

- **Parse-time streaming** — docling, markitdown, trafilatura, and Chonkie all materialise the full document internally. Streaming their output requires switching parsers or chunkers, which is a separate architectural decision with significant risk.
- **Enricher streaming** — `MarkdownEnricher.prepare()` requires the full text to build the heading offset table; `CodeEnricher.prepare()` requires the full AST. Both are correct two-pass designs. Making them incremental would degrade chunk metadata quality for no measurable gain.
- **File-size threshold** — the "always batch" approach is uniformly better; a threshold adds config complexity with no benefit (see Key Decisions).
- **Parallel file ingestion** within a single `ingest_directory()` call — a separate concern with its own locking implications.
- **Parser-level memory reduction** — docling's internal memory during PDF conversion is bounded by docling itself. A file-size guard (reject files above N MB) is a useful standalone hardening item but belongs in a separate brief.
- **Configurable batch size** — `_INGEST_CHUNK_BATCH_SIZE` is a named constant, not a TOML config key. Operators don't need to tune this; 512 chunks × ~1 KB text + ~1.5 KB vector = ~1.3 MB per batch, well within any reasonable host.
- **`store.ingest_chunks()` API changes** — the method signature stays as-is; callers simply call it multiple times.

## Key Decisions

- **Always batch, no threshold**: A configurable threshold ("stream only above N MB") adds cognitive load and a config knob with no real benefit. Every single-file ingest that produces > 512 chunks is made cheaper; small files produce one batch identical to today. Uniform batching also simplifies the code path.
- **512 chunks per batch**: At ~4 KB average (text + vector + metadata) per chunk, 512 chunks ≈ 2 MB per batch. Safe on any host including 512 MB containers. No operator tuning needed.
- **Remove `all_vectors` / `all_chunks` accumulators in `ingest_directory()`**: These lists grow O(corpus size) and serve only centroid computation and description regeneration. Both are handled incrementally: centroid via B5's `centroid_sum` in the store, description via `_should_regenerate()` against running per-file counts.
- **Enrichers stay two-pass**: `MarkdownEnricher` and `CodeEnricher` both operate on the full parsed text to build their offset tables (heading tree and AST scope table). This is architecturally correct — chunk-level heading/scope context requires global document knowledge. Do not break this.
- **Centroid via B5 incremental path**: With `centroid_incremental_enabled=True` (default since B5), `ingest_chunks()` already updates `centroid_sum` on every call. Batched ingest uses this path naturally; no centroid logic changes needed.
- **FTS rebuild once, at the end**: FTS index rebuild is expensive. It fires once after all batches for a file (or after all files for a directory), not per batch. The existing `rebuild_fts=True/False` parameter continues to control this.
- **`_vector_collector` / `_chunk_collector` parameters**: These exist on `ingest_file()` for integration tests. With the accumulator refactor they become unnecessary but must be preserved for backward compatibility; they can be populated from batch results if callers pass them.

## Edge Cases & Constraints

- **Single chunk document**: If a file produces exactly 1 chunk, it becomes a 1-item batch. Behaviour is identical to today.
- **Empty file / zero chunks**: Returns `IngestResult(chunks_created=0, status="ok")` immediately, as today. No batching loop entered.
- **File producing exactly `_INGEST_CHUNK_BATCH_SIZE` chunks**: One full batch written; no edge case.
- **`store.ingest_chunks()` failure mid-file**: If a batch fails (e.g., LanceDB error), the error propagates up to `ingest_file()` which returns `IngestResult(status="error")`. Partial batches already written to the store remain (same semantics as today — there is no transactional rollback per-file). A subsequent re-ingest of the same file calls `delete_document()` first, which cleans up the partial write.
- **`StoreBusyError` on a mid-file batch**: Propagates up as an error result, same as today.
- **`centroid_incremental_enabled=False` (pre-B5 path)**: The old path in `ingest_directory()` computed centroid from `all_vectors`. With accumulators removed, this path must either (a) call `recompute_collection_meta()` after all files complete (full scan, correct), or (b) be treated as unsupported after D4 (acceptable — the flag has been `True` by default since B5 and the pre-B5 path is legacy). Decision: keep the flag respected but emit a WARNING when `centroid_incremental_enabled=False` and D4 batching is active; the centroid is recomputed via full scan after the directory completes.
- **`_vector_collector` and `_chunk_collector` callers**: Only `ingest_directory()` passes these to `ingest_file()`. After the accumulator refactor, `ingest_directory()` no longer needs them. The parameters remain on `ingest_file()` with their existing semantics (populated per-batch) for any external callers.
- **Description regeneration timing**: `_should_regenerate(batch_doc_count, batch_chunk_count, described_at)` is called with the running doc/chunk count after each file completes. This matches today's per-directory semantics; no change to when descriptions regenerate.
- **Watcher / sync path**: `watcher.py` and `sync.py` call `pipeline.ingest_file()` and `pipeline.ingest_directory()` with the same signatures. No changes needed there; they benefit automatically.

## Resolved Decisions (formerly Open Questions)

- **`_INGEST_CHUNK_BATCH_SIZE` stays an internal constant** — not exposed in `archon-search.toml`. A wrong operator value (e.g. 10000) silently reintroduces OOM. If benchmarks prove 512 is wrong, change it in code with a profiling note. Add `# ponytail: profile on large PDFs before changing` comment in `constants.py`.
- **Ship 512 as the constant** — 512 × ~4 KB ≈ 2 MB per batch is safe on any host. If real-world profiling shows chunks average significantly more (tables, code blocks), bump to 256. Not a D4 blocker.
- **LanceDB fragmentation is out of scope for D4** — `table.optimize()` already exists for compaction. If post-D4 benchmarks show fragmentation, add a single `optimize()` call after the final batch. Add `# ponytail: monitor for LanceDB fragmentation on large corpora` comment near the batch loop.
- **Remove `centroid_incremental_enabled` flag in D4** — the pre-B5 path has defaulted to `True` since B5 and is effectively dead. Remove it, simplify `ingest_directory()`, and add a `BREAKING.md` entry.
- **`_vector_collector` / `_chunk_collector`: no test changes needed** — verified that no test outside `ingest_directory()` passes these parameters to `ingest_file()` directly (only `pipeline.py:499-500` uses them). Parameters stay on the `ingest_file()` signature for backward compatibility; no assertion updates required.

## Future Iterations

- **Parse-time streaming for plain text**: `_parse_plain()` could use a line/paragraph generator for enormous text files, avoiding `read_text()`. Low priority — Python string reads are fast; the bottleneck is docling, not plain text.
- **Docling page-by-page export**: If docling exposes a page iterator, `_parse_pdf()` could yield one page's markdown at a time, allowing the enricher to run per-page. This would cap parse-time RAM at ~1 page regardless of PDF size. Currently blocked on docling's API surface.
- **Parallel batch embedding**: Multiple embed batches per file could be pipelined (embed batch N+1 while writing batch N). Would reduce wall-clock ingest time for large files. Introduces concurrency at the per-file level; revisit in Phase E.
- **Configurable batch size** for operators who know their chunk sizes are unusually large (e.g., long-context code files).
- **File-size guard**: Reject files above a configurable MB limit at ingest time with a clear error and a `413`-equivalent response, to prevent docling from OOM-ing the process on pathological inputs. Separate brief.

## Recommendation

Build this now. The change is surgical: two functions in `pipeline.py`, a single constant in `constants.py`, no new configuration surface, no API changes. The risk is low because the B5 incremental centroid path already handles per-batch centroid accumulation correctly — D4 just stops accumulating `all_vectors` before writing begins. The one thing that must not be compromised is the enricher design: do not touch `MarkdownEnricher` or `CodeEnricher`. Their two-pass design is correct. The only question worth settling before coding is whether 512 chunks is the right batch constant — profile one real large PDF first.
