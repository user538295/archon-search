# Bug Brief: Image OCR ingest grows native memory without bound (~700 MB retained per image) until the OS dies

**ID:** 2026-08-19-010 · **Severity:** P0 · **Status:** Fixed in code 2026-08-19, uncommitted; **item 3 (the two upstream filings) NOT done**
**Decision (2026-08-19, owner):** implement the Fix section as written: OCR scale 1.0 + recycled parse-worker process (`max_tasks_per_child` + RSS budget) + two upstream filings; in-process converter recycling explicitly rejected

**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]] — this is the dominant mechanism of the >58 GB crash.

## Status detail (2026-08-19)

- **Stale line cites below are historical, not errors to chase (C2-T-10).** The Root-cause and
  References sections quote pre-fix line numbers: `_IMAGE_EXTENSIONS` was `parser.py:45` and
  `_parse_with_docling` was `parser.py:102-122`. Both moved when the parse worker landed — read
  those sections as the record of what was true before the fix, and grep `archon_search/parser.py`
  by symbol for current locations. The body is deliberately left as written.
- **Fix 1 (OCR scale 1.0) — done, IMAGE-ONLY (corrected in cycle-1 review, C1-B-1).**
  `parser.py::_build_docling_converter` passes a concrete `RapidOcrOptions(backend="onnxruntime",
  scale=1.0)` inside a `PdfPipelineOptions`, wired to `InputFormat.IMAGE` only. The original text
  here said this was wired to both `InputFormat.IMAGE` and `InputFormat.PDF`; that was wrong and
  has since been fixed in code, not just in this doc. For a PDF, `OcrOptions.scale` is a 72-DPI
  render multiplier off the page geometry (no source bitmap to preserve), so applying `scale=1.0`
  there would have rendered scanned PDFs at 72 DPI instead of docling's default 216 DPI — nine
  times fewer pixels, and a regression, not a fix. `InputFormat.PDF` is now absent from
  `format_options`, so PDFs keep docling's own defaults; the worker-recycle bound is what caps
  PDF parse memory instead.
- **Fix 2 (recycled parse worker) — done.** `DocumentParser` owns a lazy single-worker
  `ProcessPoolExecutor` (`spawn`, `max_tasks_per_child`). New config key
  `[ingest].max_tasks_per_child`, default **25** (`DEFAULT_DOCLING_MAX_TASKS_PER_CHILD` in
  `constants.py`), must be `>= 1`. No separate RSS budget knob was added — the recycle count is the
  bound; the RSS budget lives in the regression test.
- **Fix 3 (two upstream filings) — NOT done.** Neither the docling/rapidocr per-convert retention
  report nor the auto-OCR options-discard report has been filed. Still open.
- **Fix 4 (do not add in-process converter recycling) — honoured;** not implemented.
- **Verification — done.** `tests/test_parser_ocr_memory.py::test_image_ocr_memory_bounded_over_mixed_size_corpus`
  (marker `docling`) asserts growth `< 500 MB` and `>= 2` distinct worker PIDs (i.e. >= 1 recycle)
  over 40 mixed-size images; measured **1,304 MB → 210 MB** retained with 2 worker generations. The
  scale guard is `::test_parse_path_requests_ocr_scale_one` (plus the real-docling-lane `::test_build_docling_converter_effective_scale_is_one_image_only_default_pdf`, added in cycle-1 review) and the pool shape is pinned by
  `::test_docling_parsing_runs_in_recycled_worker_process` (both default lane); OCR text is checked
  by `tests/test_parser.py::test_image_ocr_still_reads_text_at_scale_one` (`docling` lane).
- **Accepted trade-off:** the worker recycles after `max_tasks_per_child` *cumulative* parses, not
  on any idle-time trigger — a server sitting idle keeps its last parse worker (models resident,
  ~1 GB) resident indefinitely, and only enough further parses (spread across however much time
  that takes) eventually recycle it. No idle shutdown was added.
- **Version citations do not match this venv.** This brief cites docling 2.120.3 / rapidocr 3.9.2 /
  onnxruntime 1.29.0 (Measured, and References); `.venv` actually has **docling 2.117.0 / rapidocr
  3.9.2 / onnxruntime 1.28.0**. The mechanism reproduced and the fix was verified against those
  installed versions — the reproduction test and the full suite were run in this venv, and
  `parser.py::_build_docling_converter` records the auto-OCR options-discard as "verified on docling
  2.117". The original measurement provenance above is left as written.
- Docs updated for the fix: `UserManual/30_configuration.md`, `UserManual/50_ingestion_and_collections.md`,
  `OperatorGuide/80_capacity_and_performance.md`, `Architecture/110_component_catalog_and_layer_breakdown.md`,
  `Architecture/140_error_handling_strategy.md`, `Architecture/200_testing_strategy.md`,
  `Architecture/210_performance_and_scalability.md`, `Architecture/530_technical_debt_refactoring_roadmap.md`,
  `archon-search.toml.example`, and the parser-race references in
  [[2026-08-19-060-ingest-job-level-lock-dropped-brief.md]].

## Problem

`archon-search` OCRs **every raster image** it encounters during ingest (`.png .jpg .jpeg .tiff
.tif .bmp .webp`) inside the long-lived server process, via docling → RapidOCR → onnxruntime.
Memory retained by the OCR stage accumulates for the life of the process: on the incident corpus,
**~700 MB average per image**, never returned while the converter lives. docling renders every
image at **3× its native resolution** before OCR (`OcrOptions.scale` default `3.0`), so a
1024×1024 icon becomes a 3072×3072 bitmap. A real source repository contains thousands of images
(app icon sets at every resolution, screenshots, photos). There is no cap, no opt-out, no
converter recycling, and no memory guard. `max_file_mb` (`pipeline.py:446-462`) is disk-size
based and default-off — it cannot help (a 200 KB icon on disk costs ~800 MB of RAM).

Image OCR is an intentional, wizard-enabled feature and stays **on** — the fix bounds its memory,
it does not remove the feature.

## Failing repro (write the regression test from this)

Run with the production venv. Guarded: RSS cap + per-file alarm. Feed it ~60 mixed-size PNGs
(icon sets with multiple resolutions are ideal — e.g. any `Assets.xcassets`):

```python
# repro: one shared DocumentConverter (exactly what parser.py does), sequential converts
import os, subprocess, sys, time
from pathlib import Path
def rss_mb():
    out = subprocess.run(["ps","-o","rss=","-p",str(os.getpid())],capture_output=True,text=True).stdout
    return int(out.strip())/1024
from docling.document_converter import DocumentConverter
conv = DocumentConverter()
files = [Path(p) for p in Path(sys.argv[1]).read_text().splitlines()][:60]
for i, p in enumerate(files):
    conv.convert(str(p)).document.export_to_markdown()
    print(i, f"{rss_mb():.0f}MB", flush=True)
    assert rss_mb() < 8000, "leak reproduced — abort before hurting the host"
```

**Measured (2026-08-19, docling 2.120.3 / rapidocr 3.9.2 / onnxruntime 1.29.0):**
RSS 1,229 MB after first file (model load) → **3,063 MB after 60 small icons**. Step jumps keyed
to image size: +233 MB (512px), **+788 MB (first 1024px icon)**, +446 MB (1104×944 photo with
real text). Same 60 files with `do_ocr=False`: 684 → 700 MB — **flat**: the growth is entirely
the OCR stage. GPU/MPS memory flat (1,311 MB) — this is CPU-side native memory.

End-to-end: `SearchPipeline.ingest_directory` over the production-ordered first 150 incident
files reached **11.2 GB at file 80** (15 images + 1 embedder load).

## Root cause (mechanism verified by experiment, 2026-08-19)

1. Routing: `parser.py:45` (`_IMAGE_EXTENSIONS`) sends images to `_parse_image` →
   `_parse_with_docling` (`parser.py:102-122`); `pipeline.py:181-194` deliberately removed raster
   images from `_BINARY_EXTENSIONS` so `ingest_directory` walks them all.
2. Amplification: docling renders the OCR bitmap at `scale=3.0`
   (`docling/datamodel/pipeline_options.py`, `OcrOptions.scale`; used in
   `models/stages/ocr/rapid_ocr_model.py` via `get_page_image(scale=self.scale)`) — 9× the pixels.
3. Retention — **two components, measured by the recycle experiment** (process the 60-image set,
   `del converter; gc.collect()` every 20 files):
   - **Majority: memory reachable from the docling converter/OCR object graph.** Destroying the
     converter freed **1,148 MB instantly** (2,432 → 1,283 MB), and a rebuilt converter re-processing
     the same shapes stayed flat (~1,327 MB). So most of the "leak" is third-party object
     retention inside docling/rapidocr, reclaimable by dropping the objects.
   - **Residue: ~8 MB/image that survives `del` + gc** (post-recycle floor crept
     1,283 → 1,326 → 1,774 MB across 60 images) — native-level retention only process exit reclaims.
   - **Not** the onnxruntime CPU arena: rapidocr ships `enable_cpu_mem_arena: false`
     (`rapidocr/config.yaml:28`, applied at `rapidocr/inference_engine/onnxruntime/main.py:81`).
4. Context: all of this runs inside the server process (`asyncio.to_thread` from
   `pipeline.ingest_file`), so accumulation is process-lifetime and also starves the event loop
   (CLI timeouts observed during ingest).

**Also found:** with default auto OCR options, docling **silently discards** user `ocr_options`
fields — `auto_ocr_model.py:88-91` builds a fresh `RapidOcrOptions(backend=…, mode=self.options.mode)`
copying only `mode`. Setting `scale` on the default options object has no effect; a concrete
`RapidOcrOptions(scale=…)` must be passed as `ocr_options`. (Upstream-report-worthy on its own.)

## Measured effect of the two fix levers

| Variant (60 incident images) | RSS after model load → final | Post-load growth |
|---|---|---|
| Default (`scale=3`, shared converter) | 1,229 → 3,063 MB | +1,834 MB |
| `RapidOcrOptions(scale=1.0)` | 1,112 → 1,650 MB | **+538 MB (3.4× less)**; 1024px-icon jump +788 → ~+120 MB |
| Converter recycle every 20 files (`scale=3`) | floor 1,283 → 1,774 MB | bulk reclaimed per recycle; **floor still creeps ~8 MB/image** |

Neither lever alone is bounded; together they cut the slope ~10×+ but the residue still grows.

## Fix (decided direction: keep OCR on, make memory finite)

1. **Stop the 3× upscale:** in `parser.py:_parse_with_docling`, build the converter with explicit
   `ImageFormatOption(pipeline_options=PdfPipelineOptions(ocr_options=RapidOcrOptions(
   backend="onnxruntime", scale=1.0)))` — a **concrete** options instance (see the auto-options
   discard above). docling's own field docstring recommends lowering scale for already-high-res
   sources. Measured 3.4× retention cut + faster OCR.
2. **Hard ceiling — parse in a recycled worker process:** move docling parsing out of the server
   into a single-worker `concurrent.futures.ProcessPoolExecutor(max_workers=1,
   max_tasks_per_child=N)` (stdlib, present in the shipped CPython 3.13 —
   `concurrent/futures/process.py:632`). Worker exit returns **all** memory (both components) to
   the OS — the only complete reclaim, since CPython can never unload C-extension libraries
   in-process. Choose N so worst-case worker growth stays modest (at scale=1, N=50 ≈ ≤0.5 GB
   excess; make N and/or a worker RSS budget configurable under `[ingest]`). Side benefit: OCR
   CPU leaves the server process — no more request starvation during ingest.
3. **File upstream with the minimal repro:** (a) docling/rapidocr per-convert retention (the
   1.1 GB reclaimed-on-del evidence localizes it to their object graph — fixable at source);
   (b) the auto-OCR options-discard bug. A root-cause fix upstream shrinks the problem for
   everyone, but containment (1)+(2) stays regardless — dependencies regress.
4. Do **not** bother with in-process converter recycling once (2) exists — it's strictly weaker
   (measured creeping floor) and adds a second lifecycle to maintain.

## Verification

- Regression test (from the repro): ≥40 mixed-size images through the new parse path must end
  with total-process `RSS(final) − RSS(baseline) < 500 MB` and the worker respawn observable
  (assert ≥1 recycle). Mark slow/serial per `tests/CLAUDE.md`.
- Scale assertion: instrument or introspect the effective OCR scale == 1.0 (guards against the
  auto-options discard silently reverting to 3.0 on a docling upgrade).
- Behavior: OCR text output unchanged on a text-bearing fixture image (scale change must not
  break recognition of normal screenshots).

## References

- [[archon_search/parser.py]] — `_IMAGE_EXTENSIONS` (:45), `_parse_with_docling` (:102-122)
- [[archon_search/pipeline.py]] — `_BINARY_EXTENSIONS` raster removal (:181-194), file filter (:896-911), size guard (:446-462)
- installed docling 2.120.3: `datamodel/pipeline_options.py` (`OcrOptions.scale = 3.0`), `models/stages/ocr/rapid_ocr_model.py` (`get_page_image(scale=self.scale)`; user-params merge :439-452), `models/stages/ocr/auto_ocr_model.py` (options discard :88-91)
- installed rapidocr 3.9.2: `config.yaml:28` (`enable_cpu_mem_arena: false`), `inference_engine/onnxruntime/main.py:78-87`
- [[2026-08-19-000-oom-crash-incident-report.md]] — measured curves A/B/C and the recycle/scale experiments
