
**[2026-08-19] OOM RCA (compacted from learnings.md)**: original detail — per-file RSS CSV instrumentation; log-census = per-logger counts + uniq file paths; also check macOS DiagnosticReports and `sysctl kern.boottime` for crash/reboot correlation.

**[2026-08-19] bug briefs (compacted from learnings.md)**: dropped phrasing — "write the failing repro FIRST; probe every config permutation. A green test proves only code+test agree — diff it against the DOC contract."

## Evicted from learnings.md 2026-08-19 (lowest N, OCR-memory task)
- **[2026-08-18] (×1) instruction-file bloat**: before adding to `CLAUDE.md`, grep `Documentation/` and `pyproject.toml` for the fact — its Architecture section was 57% of the file and fully duplicated. Keep only what is not greppable.

## Evicted from learnings.md 2026-08-20 (lowest N, startup-sync crash-loop task)
- **[2026-08-19] (×1) assert the WIRING**: an unasserted ctor kwarg silently disconnects its feature and the suite stays green — `initializer=` (orphan watchdog), `max_tasks_per_child=` (operator knob). Assert the kwarg reaches the ctor, from a NON-default value.

- **[2026-08-21] brief triage (compacted into `briefs`)** — a doc-only fix is never behavioral: read the code after closing a DOC report, since the report may describe a real defect the doc merely mis-stated.

- **[2026-08-20] (×2) latch/guard state (compacted out of learnings.md)**: durable suppression gets its OWN data-dir file; clear it UNCONDITIONALLY. A broad catch around `await to_thread(...)` swallows `CancelledError` — re-raise FIRST or shutdown latches a degraded flag.

- **[2026-08-24] (×1) spike execution (merged into the `plan/task authoring` entry dated 2026-08-24)**: its `#team`-gate half survives in the live entry (learnings.md, "Plan-Making & Agent Process"), which absorbed the count as ×3. Long-form detail dropped in the merge: re-verify a subagent's stated BLOCKER CAUSE — testing 2 spike blockers exposed 2 false claims + 2 shipped defects. Also dropped from that same live entry: write derivation rules, not enumerations, in task bodies.

- **[2026-09-06] install/wizard (compacted from learnings.md)**: on a reflip of a wizard prompt, rewrite the GUARD DOCSTRINGS too — a stale guard docstring argues for flip #4.

- **[2026-09-06] install/wizard (compacted from learnings.md, 2nd pass)**: dropped clause — `resolve_torch_device(coreml)` returns `mps` in the dev venv, not `cpu`.

- **[2026-09-06] test vacuity (compacted from learnings.md, 2nd pass)**: dropped clause — a guard tripping the FIRST filter never reaches the branch it names.

- **[2026-09-06] test vacuity (compacted from learnings.md)**: dropped clause — a shared `MagicMock.read` ignoring its `n` arg silently voids a bounded `read(size+1)`; now documented in `_served_response`, `tests/test_e2e_wizard_optional_features.py`.

- **[2026-09-06] 3rd-party wiring (compacted from learnings.md)**: dropped example — the INPUT FORMAT half of the rule was docling `OcrOptions.scale`, which applies per input format (PDF vs image), not globally.
- **[2026-08-27] CI regex guards** (evicted 2026-09-06, ×1; now enforced by `tests/test_no_fstring_sql.py`): scan width = its kwarg list — positional `.where(f"` alone shipped `where=f"…"`. 7 patterns: +`filter=`/`updates_sql=`/`.add_columns(` (RAW SQL, unlike `updates=`) +`rf`/`F`. Guard only files with callsites.

- **[2026-09-07] guards (compacted from learnings.md)**: dropped clause — `eval/backends.py` edits drift `eval_hash`, so a backend change must regenerate it or the eval gate fails on the hash rather than on a metric.

- **[2026-09-07] smoke/subprocess/container + TypeSpec (compacted from learnings.md)**: dropped clauses — session-scoped fixtures use `tmp_path_factory`, not `tmp_path`; seed a smoke corpus with REAL text, not lorem, or retrieval asserts pass vacuously; TypeSpec reserves `namespace`/`model`/`op` as keywords, so a field of that name needs quoting.

- **[2026-09-07] smoke/container (compacted from learnings.md)**: dropped clause — TypeSpec optional-nullable field syntax is `field?: T | null`; both the `?` and the `| null` are needed, one alone does not compile to an optional nullable.

- **[2026-09-07] 3rd-party wiring (compacted from learnings.md)**: dropped clauses — a bare `"other"` entity label ate all GLiNER spans; a `label<>desc` mismatch yielded 0 relations, while a descriptive `related_to` description LIFTS typed relation yield. Both are now baked into `_ENTITY_LABEL_DESCRIPTIONS`/`_RELATION_LABEL_DESCRIPTIONS` in `archon_search/prose_extraction_backend.py` and guarded by `tests/test_graph_ner_real_artifact_lane.py`.

- **[2026-09-07] smoke/container (compacted from learnings.md)**: dropped clauses — session-scoped smoke fixtures use `tmp_path_factory`; `HF_HUB_OFFLINE=1` blocks fastembed too, so ingest FAILS rather than degrading. Also: the smoke lane's model-cache symlink (`_link_model_cache`, `tests/smoke/conftest.py`) exists because `get_models_dir()`/`get_graph_models_dir()` both derive from `ARCHON_SEARCH_DATA_DIR` by design (no per-cache env override), so a throwaway data dir re-downloaded ~1.3 GB per run: the cross-encoder download made the first `POST /search` ~14 s against a 5 s bound, and the GLiNER download made the graph corpus pre-seed ~195 s against a 60 s bound.

- **[2026-09-14] warm-up/lifespan (compacted from learnings.md)**: dropped clauses — a cold embedder degrades to `fts_only`, **not** a 503; bounding a lazy ONNX build with `asyncio.wait_for` *kills* the build unless the build is single-flighted behind a shared task awaited via `asyncio.shield` first (`Embedder.warmup`/`Reranker.warmup`), otherwise every subsequent cold request starts another `to_thread` encode that queues on `ModelEmbedder._lock` and pins one executor thread each; and the distinct `*WarmupTimeout` exception exists because since 3.11 `asyncio.TimeoutError` IS the builtin `TimeoutError` (an `OSError`), so a socket ETIMEDOUT from a model download is otherwise indistinguishable from the ceiling expiring. Also: extra await hops cancel background jobs started by a `TestClient` used without `with`. Dropped 2026-09-15 (S582): a `wait_for` catch needs `done()` — since 3.11 `asyncio.TimeoutError` IS the builtin `TimeoutError`, so `except TimeoutError` around `wait_for` cannot tell "the budget expired" from "the awaited coroutine itself raised `TimeoutError`" (e.g. a socket ETIMEDOUT deep in the store, or an httpx read timeout in a HyDE provider). Inspect the task instead — `asyncio.ensure_future` it first, and only map to the budget-exceeded error when `task.cancelled()` is true (`_search_budget.run_within_budget`); otherwise re-raise so the inner error keeps its own status mapping.

- **[2026-09-14] two ingest modes (S293, compacted into the "briefs" entry)**: `_dispatch_ingest` (`archon_search/server/routes_jobs.py`) guarded the `documents` branch with `if hasattr(pipeline, "ingest_documents")` — a capability probe for a method of its own production class, which `SearchPipeline` never had. The documented `documents`-only ingest therefore logged a WARNING, wrote nothing, and still transitioned the job to DONE; the next `POST /search {"collections": [...]}` 404'd on `CollectionNotFoundError`. Fix: split `ingest_file`'s post-parse half into `SearchPipeline._ingest_text(markdown, path, collection, doc_id, ...)` (enrich → chunk → embed → persist → graph) and `ingest_directory`'s tail into `_finalize_batch_ingest`, then build `ingest_documents` on both. In `_ingest_text` the `path` is provenance only — it names the document and need not exist on disk (the `stat()` for `updated_at` and the `.acl` sidecar probe already degrade cleanly on a missing file), so an inline document's client-supplied `source_path` seeds `doc_id = sha256(source_path)` and re-ingesting the same `source_path` replaces that document.

- **[2026-09-15] guards (compacted from learnings.md)**: a cited SCRIPT FILENAME inside a doc or brief trips the removed-engine name guard — reword the citation rather than widening the guard's allowlist.
