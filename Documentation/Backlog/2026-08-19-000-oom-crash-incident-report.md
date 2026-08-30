# Incident Report: System crash from unbounded server memory growth during ingest (>58 GB on a 48 GB machine)

**Date:** 2026-08-19 · **Severity:** P0 (full system freeze, manual power-cycle) · **Status:** Root-caused; fixes tracked in briefs 010–080
**Affected version:** 26.8.1987 (installed uv tool == repo HEAD `22bfdea5` at time of incident)

## Summary

During the first ingest of two real-world source repositories (`financialwell`, `n1ka`), the
`archon-search` server process grew past 58 GB RSS on a 48 GB Apple Silicon Mac (macOS 26.5.2,
`Mac16,8`), the system froze at ~12:06 and was manually power-cycled (reboot 12:07:48, no kernel
panic report, no clean-shutdown record). On reboot, launchd restarted the server and the startup
sync **automatically re-started the same ingest**, and memory began climbing again until the
service was manually booted out of launchd.

Two independent memory mechanisms account for the growth; both were **reproduced empirically**
in a sandboxed, memory-capped rerun of the exact production pipeline over the exact
production-ordered file list:

1. **Image OCR native-memory retention (dominant, unbounded):** every raster image in the corpus
   is OCR'd through docling → RapidOCR → onnxruntime, at 3× native resolution (docling default).
   Follow-up experiments (2026-08-19) split the retention into a **majority component held alive
   by the docling converter/OCR object graph** (destroying the converter frees ~1.1 GB instantly)
   plus a **~8 MB/image native residue** that survives object destruction; it is **not** ORT
   arena growth (rapidocr ships `enable_cpu_mem_arena: false`). Measured: **~700 MB average
   retained per image** on this corpus mix (a single 1024×1024 app icon: ~800 MB on first
   encounter; ~120 MB at OCR scale 1.0). The corpus has **2,112 eligible PNGs**; ~1,000+ images
   were walked before the crash. → [[2026-08-19-010-image-ocr-unbounded-memory-brief.md]]
2. **Embedder under CoreMLExecutionProvider (large, step):** the first embed loads
   `intfloat/multilingual-e5-large` under CoreML — the 1237-node ONNX graph splits into 146
   CoreML partitions. Measured **+8.7 GB retained** at that step in-pipeline (4.6 GB isolated
   load + first 512-chunk batch arena). At startup the model is additionally loaded **twice
   concurrently** (eager warmup + model validation). → [[2026-08-19-040-double-embedder-load-startup-brief.md]]

Sum on the production corpus: ~8.7 GB (embedder) + reranker + torch layout models + hundreds of
MB × ~1,000 images ⇒ **~58 GB and still climbing** at freeze time. The reproduction reached
11.2 GB after only 15 images + 1 embedder load (guard-tripped at file 80 of 150; production
walked 1,325 files).

## Verified timeline (local time, 2026-08-19)

| Time | Event | Evidence |
|---|---|---|
| 10:41–11:21 | User ran llama.cpp with GLM-4.6V-Flash GGUF in iTerm2; downloaded 10 GB; died SIGABRT. **Unrelated to the server** — it was dead before the server session began. | `~/Library/Logs/DiagnosticReports/llama-2026-08-19-112134.ips` (parentProc zsh, coalition iTerm2) |
| 11:29–11:34 | Wizard reconfigured; service installed; server session 1 starts (PID 42979) | toml mtimes; log born 11:33:49; log line 11:34:26Z-2h |
| 11:34–11:36 | Startup loads e5-large twice concurrently (warmup + validation) + reranker; ×4 "Context leak detected" per load | log 11:34:52 (`embedder.py:43`), 11:35:23 (`model_validation.py:166`), warm-up complete 11:35:50 |
| 11:36:59 / 11:37:11 | User's two `collection add` jobs start; they run **concurrently** in one process | `archon-search-jobs.json`; interleaved per-file log lines |
| 11:41:29 | n1ka job dies: `ValueError: Table 'n1ka' was not found` in FTS optimize/rebuild | full traceback in log → [[2026-08-19-050-fts-rebuild-on-empty-collection-brief.md]] |
| 11:37–12:04:35 | financialwell ingest walks 1,325 of 4,117 eligible files (~2 s/file, image-OCR dominated); 734 spaCy download retries; only 24 docs persisted; memory climbs to the observed >58 GB | 759 langdetect warnings; 734 graph_extractor INFO; last log line 12:04:35 |
| ~12:06 | System freezes under memory pressure (a parallel Claude session investigating "the app is eating my memory" last wrote 12:06:14) | session jsonl mtimes |
| 12:07:48 | Manual power-cycle reboot (no shutdown record; no `.panic` file) | `sysctl kern.boottime`; empty `/private/var/db/PanicReporter` |
| 12:11–12:16 | launchd restarts server (PID 999); **startup sync auto-re-ingests**; RapidOCR reloading at 12:13; RSS 2.9 GB + 1.2 GB GPU and climbing; CLI `collection list` times out | session-2 log; live `ps`/`footprint`/`sample` capture → [[2026-08-19-020-startup-sync-crash-loop-brief.md]] |
| ~12:20 | Service stopped: `launchctl bootout gui/$UID/com.archon.search` | verified no `archon_search` processes remain |

## Production log census (session 1, lines 1–7628)

- 759 × `language detection failed … Unable to avoid copy` — **every** parsed file; all paths unique (no double-ingest of the same file) → [[2026-08-19-070-fasttext-numpy2-language-detection-brief.md]]
- 734 × `spaCy model 'en_core_web_sm' not found; auto-downloading (first call only).` — retried per file; download always fails ("No package installer found" — uv tool venv has no pip) → [[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]]
- 599 × `RapidOCR returned empty result!` + 214 RapidOCR warnings — blank icons through OCR
- 288 × `.png` paths reached language detection (images whose OCR produced text)
- 24 × `centroid stale, recompute queued` — the only 24 documents actually persisted
- 10 × `Context leak detected, CoreAnalytics returned false` — CoreML context leaks at the 2 model loads
- 1 × ERROR — the n1ka job traceback

## Reproduction (all measured 2026-08-19, same venv, sandboxed `ARCHON_SEARCH_DATA_DIR`)

**A. docling image parse alone** (production pattern: one shared `DocumentConverter`), 60 corpus
images: RSS 1,229 MB (after model load) → 3,063 MB. Growth is step-wise on new image shapes:
+233 MB (512px icon), **+788 MB (first 1024px icon)**, +446 MB (1104×944 photo with real text).
MPS/GPU stayed flat at 1,311 MB — the growth is CPU-side native memory (mechanism split in brief 010:
majority object-graph retention + small unreclaimable residue; not ORT arena, which rapidocr disables).

**B. same 60 images with `do_ocr=False`:** 684 → 700 MB. **Flat.** The leak is entirely the OCR stage.

**C. full `SearchPipeline.ingest_directory`** (production config, graph enabled, CoreML embedder)
over the exact production-ordered first 150 files:
files 1–65 (text) flat at ~385 MB → file 66 (`db design.png`, 1102×1111) **+956 MB** → file 69
(`AppDelegate.swift`, first file to reach the embedder) **+8,699 MB in 16.7 s** → icons +160…+788 MB
each → **11,229 MB at file 80 of 150**, kill-switch tripped. Exit even reproduced session 1's dying
`resource_tracker: leaked semaphore` warning.

**D. embedder isolated:** `TextEmbedding("intfloat/multilingual-e5-large", providers=["CoreMLExecutionProvider"])`
→ 86 MB (import) → **4,629 MB (after load)** → 3,692–3,801 MB steady; CoreML reports 146 partitions
/ 786 of 1237 nodes; ×4 "Context leak detected" on first embed.

Repro scripts are embedded in brief 010 (image loop) and reproducible from brief 040 (one-liner).

## Ruled out (verified, not assumed)

- **GLM GGUF / llama.cpp:** user-launched in a terminal, crashed 13 min before the server session;
  the server's `llama_cpp` provider is HTTP-only ([[archon_search/providers/llama_cpp_provider.py]])
  and llama-server was unreachable throughout (log warnings both sessions). The GGUF was never
  loaded by the server.
- **LanceDB data volume:** 24 docs / 48 chunks persisted — negligible.
- **torch/MPS:** GPU allocation flat at 1.3 GB during the leak.
- **Graph LLM enrichment:** never invoked (spaCy failed first; llama-server unreachable).
- **Duplicate ingest of the same files:** all 759 per-file log lines are unique paths.

## Contributing defects (each has its own brief)

**The filename number is the normative execution order** (owner rule, 2026-08-19): fix in
ascending order, 010 → 090. The tens spacing leaves room to insert work between existing items;
re-prioritizing means **renaming the file** to its new slot (and updating links) so the sequence
on disk never lies about the plan.

| Brief | Severity | One-liner |
|---|---|---|
| [[2026-08-19-010-image-ocr-unbounded-memory-brief.md]] | P0 | Every raster image OCR'd in-process; onnxruntime arena ratchet never releases; no cap/opt-out |
| [[2026-08-19-020-startup-sync-crash-loop-brief.md]] | P0 | Startup sync auto-re-ingests on every boot — re-enters the OOM death spiral unattended — **Done 2026-08-20**, brief archived in `Documentation/Completed/` |
| [[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]] | P1 | spaCy download retried per file (734×); failure aborts each file's ingest pre-persist |
| [[2026-08-19-040-double-embedder-load-startup-brief.md]] | P1 | e5-large loaded twice concurrently at startup; CoreML load costs 4.6 GB + leaks contexts |
| [[2026-08-19-050-fts-rebuild-on-empty-collection-brief.md]] | P1 | FTS optimize/rebuild fires for ok-but-zero-chunk results → `Table not found` kills the job |
| [[2026-08-19-060-ingest-job-level-lock-dropped-brief.md]] | P1 | Job-level collection exclusivity silently dropped; 3 disjoint lock domains; parser init race |
| [[2026-08-19-070-fasttext-numpy2-language-detection-brief.md]] | P2 | fasttext 0.9.2 × NumPy 2: language detection fails for every file |
| [[2026-08-19-080-process-restart-stale-updated-at-brief.md]] | P1 | Crash-recovery FAILED marking keeps the pre-crash `updated_at` — corrupts forensics and lets `_evict_old` silently drop the job — **Done 2026-08-30** |
| [[2026-08-19-035-multilingual-graph-ner-brief.md]] | Enhancement | Committed successor for multilingual prose NER (GLiNER-class ONNX, eval-gated) — filed during the fix-decision review of 030 |

## Operational state & warning

The service is **stopped and unloaded for this login session only**
(`launchctl bootout gui/$UID/com.archon.search`). The LaunchAgent reloads at next login/reboot,
and with `[collections]` still listing both repos the startup sync will immediately re-ingest and
re-enter the memory spiral. Until 010 + 020 are fixed: keep the service unloaded, or remove the
two paths from `[collections]` in `~/.archon-search/archon-search.toml`.

**Update 2026-08-20:** 010 (`fix(parser): bound image-OCR memory with a recycled parse worker`) and
020 (the crash-loop guard) are both fixed; 010's item 3, the two upstream filings, is still open.
The guard keys on `JobStore.crashed_ingest_on_load`, i.e. a job record rewritten to
`FAILED / "process_restart"` on load (`jobs/store.py`). The first cut of the guard covered only a
crash during a job-backed ingest (`POST /ingest`, `POST /sync`): the lifespan startup sync
called `collection_sync.sync()` directly and wrote **no** job record, so a crash during the
automatic startup sync itself — the sequence observed at 12:11–12:16 above — left no marker for the
next boot, and a loop whose every iteration dies inside the unattended sync was unbroken. Closed the
same day: `_run_startup_sync` now opens a `SyncJob` (`_start_startup_sync_job`, `server/app.py`) and
drives it to `DONE`/`FAILED` on every exit path, so a `kill -9` mid-startup-sync leaves a `RUNNING`
SyncJob that the next boot rewrites to `process_restart` — the guard arms itself. Regression test:
`tests/test_c1_bug_repros.py::test_crash_during_startup_sync_arms_the_guard_on_the_next_boot`. The
operational advice above therefore no longer applies to 020's failure mode.

## References

- [[2026-08-19-010-image-ocr-unbounded-memory-brief.md]] — dominant memory mechanism + repro harness
- `~/.archon-search/logs/archon-search.log` — both production sessions (7,670 lines)
- `~/.archon-search/archon-search-jobs.json` — the two failed jobs
- `~/Library/Logs/DiagnosticReports/llama-2026-08-19-112134.ips` — rules out llama.cpp
