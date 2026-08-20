# Bug Brief: spaCy model download failure is retried for every file and aborts each file's ingest before persist — 734 retries, near-total ingest failure

**ID:** 2026-08-19-030 · **Severity:** P1 · **Status:** Decided 2026-08-19 (owner) — degrade-not-abort + failure latch + **wizard auto-provisions the model** so the graph always works with zero runtime install/network dependency (see Fix). Full-LLM extraction (former Option D) evaluated and rejected — see "Engine comparison" below.
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

Two coupled defects in the graph-extraction path when `en_core_web_sm` is missing and cannot be
installed (the normal state in a `uv tool install` venv — it has **no pip**, so
`spacy.cli.download` always fails with "No package installer found"):

1. **No failure latch:** `GraphExtractor._load_nlp_sync` logs
   `spaCy model 'en_core_web_sm' not found; auto-downloading (first call only).` and attempts the
   download. On failure, `self._nlp` stays `None`, so the **next file's** `extract()` call runs
   the entire probe + download again. "(first call only)" is false — the incident log shows the
   line **734 times** in 27 minutes. Each retry re-scans installed-package metadata
   (`spacy.util.get_installed_models()`) and re-runs the downloader — pure CPU/log churn per file.
2. **Auxiliary failure kills the primary operation:** the load failure becomes
   `GraphExtractionResult.fatal_error` (`graph_extractor.py:318-332`), and `ingest_file` then
   returns `status="error"` **before embed/persist** (`pipeline.py:646-655`). Result: every file
   that produces at least one plain-text chunk fails ingest entirely. In the incident, only 24
   documents out of 1,325 walked files were persisted — the ingest that crashed the machine was
   also failing to index almost everything. This contradicts the project invariant that auxiliary
   graph work must never fail a primary ingest (CLAUDE.md "Auxiliary writes never fail their
   primary operation" — extraction currently sits *before* persist and is fatal).

## Failing repro

In a venv without pip (`uv tool install archon-search`) and without `en_core_web_sm`, with
`[graph] enabled = true`, ingest a directory of ≥3 `.md` files. Observed:

- the "auto-downloading (first call only)" INFO appears **once per file** (assert ≥3 occurrences),
- every file's `IngestResult.status == "error"` with the
  `Failed to load spaCy model 'en_core_web_sm'` message, `chunks_created == 0`,
- the collection ends empty despite parseable input.

## Root cause

- `graph_extractor.py:149-177` (`_load_nlp_sync`): download attempted whenever the model is not
  installed; the SystemExit→RuntimeError conversion is correct, but no state records the failure.
- `graph_extractor.py:297-332`: under `_load_lock`, `self._nlp is None` re-triggers the load per
  `extract()` call; the exception path returns `fatal_error` without latching.
- `pipeline.py:625-655`: `fatal_error` → `return IngestResult(status="error")` before the embed/
  persist block (:664+). The extractor's docstring (:230-233) prescribes exactly this — the
  contract itself is the bug for this failure class.

## Production evidence

734 × the INFO line between 11:37:00 and 12:04:06; per-file red "✘ No package installer found"
blocks; 24 docs / 48 chunks persisted from 1,325 walked files; the n1ka job's 28 files all failed
or produced zero chunks (feeding [[2026-08-19-050-fts-rebuild-on-empty-collection-brief.md]]).

## Fix

1. **Latch the failure:** add `self._nlp_load_error: str | None` set in the load-exception path;
   when set, skip probe/download and immediately take the degraded path below. Log the WARNING
   **once** (first failure), not per file. Fix the INFO wording or demote the retry log to DEBUG.
2. **Degrade instead of abort (always — no strict mode):** on spaCy unavailability, return a
   result with `fatal_error=None`, empty prose-NER output, and a one-time warning — the file
   still embeds and persists (code-symbol chunks already work without spaCy). Aligns extraction
   with the auxiliary-write invariant. Remove the `fatal_error` pathway for model-unavailability
   entirely; `fatal_error` remains only for spaCy-not-importable (missing `[graph]` extra).
3. **Wizard provisions the model; runtime never installs or downloads (owner decision):**
   `spacy.cli.download` can never work in the uv tool venv (no pip), and runtime downloads are
   the wrong layer anyway. Verified enabler: `spacy.load()` accepts a **filesystem path**
   (`spacy/__init__.py:50` — "installed package or a local path"; `spacy/util.py:523-528`).
   - **Wizard:** in the existing model-download step (embedder/reranker), also fetch the pinned
     `en_core_web_sm` wheel from the spacy-models GitHub release (pin the exact version against
     the installed spaCy per its compatibility table — spaCy 3.8.15 → model 3.8.x; a wheel is a
     zip: extract the inner `en_core_web_sm-<ver>/` model directory), place it under the data
     dir (`<data_dir>/models/spacy/en_core_web_sm-<ver>/` via a `paths.py` accessor — mind the
     `Path.home()` allowlist invariant), then **smoke-load it** (`spacy.load(path)`) before the
     wizard reports success. Same pattern as the existing fasttext `lid.176.ftz` in
     `<data_dir>/models/`.
   - **Runtime resolution order** in `_load_nlp_sync`: installed package (back-compat for pip
     installs that have it) → data-dir path → latched degrade + one-time WARNING. Delete the
     `spacy.cli.download` call from runtime entirely.
   - **Visibility:** when `[graph] enabled`, startup validation probes model presence
     (package-or-path) and surfaces a missing model in `ModelValidationResult.provider_warnings`
     / `GET /status`, like the llama-server probe — never only in per-file logs.
   - **Docs:** `Documentation/OperatorGuide/60_graph_operations.md` + `UserManual/30_configuration.md`:
     wizard provisions automatically; non-wizard installs get one documented command (re-run
     `archon-search wizard`, or the manual download-and-place instructions).

## Engine comparison — spaCy NER vs full-LLM extraction (owner question, resolved 2026-08-19)

Replacing spaCy with LLM-based entity extraction (former Option D) was evaluated and **rejected**:

| Dimension | spaCy `en_core_web_sm` (current) | Full-LLM extraction (local llama-server) |
|---|---|---|
| Latency per chunk | milliseconds, CPU | seconds (9B-class GGUF) → ingest slows orders of magnitude |
| Availability | in-process, offline, deterministic | depends on a running llama-server — which was down for the **entire** incident; D would have extracted nothing |
| Memory | ~50–100 MB | ~10 GB model + server process |
| Output reliability | deterministic labels | stochastic; malformed pairs are a known reality — `_resolve_labeled_pair` (graph_extractor.py:87-117) exists precisely to repair them |
| Quality | solid on English prose (per spaCy's published metrics, ~0.85 F1 class on OntoNotes-style English); weak on non-English and code-adjacent text | better zero-shot coverage incl. multilingual — the one real advantage |

Verdict: the existing layering is right — fast deterministic NER as the always-on base, LLM as an
*optional* relationship-enricher behind the existing AND-gate. **Follow-up (decided 2026-08-19,
filed):** `en_core_web_sm` is English-only while the deployment is `multilingual = true`; the
committed successor is a single GLiNER-class multilingual ONNX engine, eval-gated —
[[2026-08-19-035-multilingual-graph-ner-brief.md]] (per-language routing and LLM extraction both rejected
there, with reasons). Until it lands, this brief's scope includes the honest disclosure: when
`multilingual = true` and graph is enabled, the wizard summary and `GET /status` note that prose
entity extraction is English-only.

**Install-story ground truth (verified in pyproject.toml):** `en-core-web-sm` already exists as a
**dev-group-only** dependency via a `[tool.uv.sources]` URL (pyproject.toml:78-81, :174) with an
explicit comment that it cannot ship in the published wheel (PyPI rejects URL dependencies). So
end-user installs have **no package channel for the model at all** — the wizard/data-dir
provisioning above is the only PyPI-compatible path, which is why runtime auto-download existed
(and why it can never work in a uv tool venv).

## Verification

- Test: extractor whose `_load_nlp_sync` raises → first `extract()` returns degraded result with
  warning; second `extract()` performs **no** load attempt (mock assert call count == 1);
  `ingest_file` persists chunks (`status == "ok"`, `chunks_created > 0`, warning propagated).
- Test: the one-time WARNING fires exactly once across N files.
- Test: runtime never calls `spacy.cli.download` (assert the symbol is gone / never invoked).
- Test: model resolution order — package present → package wins; else data-dir path present →
  path loads; else degrade. Wizard test: after the provisioning step, `spacy.load(<data-dir
  path>)` succeeds (fixture-sized artifact or mocked fetch + real extract logic).
- Test: `[graph] enabled` + model absent → validation result carries the warning and `GET /status`
  surfaces it.

## References

- [[archon_search/graph_extractor.py]] — `_load_nlp_sync` (:149-177), load/fatal path (:297-332), fatal contract docstring (:230-233)
- [[archon_search/pipeline.py]] — fatal abort before persist (:625-655)
- CLAUDE.md — "Auxiliary writes never fail their primary operation"
