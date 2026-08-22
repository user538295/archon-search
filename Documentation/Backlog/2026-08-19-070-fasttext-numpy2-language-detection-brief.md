# Bug Brief: Language detection fails for every file — fasttext 0.9.2 `np.array(..., copy=False)` is illegal under NumPy 2

**ID:** 2026-08-19-070 · **Severity:** P2 (silent feature-wide failure + per-file log spam; not memory) · **Status:** Open
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

With `multilingual = true`, every `ingest_file` call logs
`language detection failed for <path> — tagging chunks as untagged: Unable to avoid copy while
creating an array as requested.` and tags the chunks `language=""`. The C2 language feature
(and everything downstream that keys on chunk language, e.g. dominant-language FTS tokenization
via `get_dominant_language`) is effectively disabled — for **all** files, always — while
producing one WARNING per file (759 lines in the incident session).

Cause: the installed `fasttext-wheel 0.9.2` returns predictions via
`np.array(probs, copy=False)` (`fasttext/FastText.py:232`). NumPy 2 (installed: 2.5.2) changed
`copy=False` to *raise* `ValueError` when a copy cannot be avoided — which is always the case for
this pybind list — so `model.predict()` raises on every call. `LanguageDetector.detect`
(`language_detector.py:175-199`) calls `predict` and the pipeline catches the exception per file
(`pipeline.py:542-548`).

## Failing repro

```python
# venv: fasttext-wheel 0.9.2 + numpy 2.5.2, model ~/.archon-search/models/lid.176.ftz
import fasttext
m = fasttext.load_model(str(Path.home()/".archon-search/models/lid.176.ftz"))
m.predict("hello world", k=1)
# ValueError: Unable to avoid copy while creating an array as requested.
```

Pipeline-level: ingest any text file with `multilingual = true` → the WARNING above fires and
`ChunkRecord.language == ""`.

## Root cause

`fasttext-wheel 0.9.2` predates NumPy 2 (`FastText.py:232: return labels, np.array(probs,
copy=False)`); the project ships NumPy 2.5.2. The incompatibility is in the dependency, but
archon-search selected the pair and its only handling is the per-file catch-and-warn.

## Fix

Write the failing test first (assert `detect()` returns a real language code for an obvious
English/Hungarian sample). Then pick one:

1. **(Recommended) Bypass the broken wrapper line:** in `LanguageDetector`, call the pybind layer
   directly instead of `model.predict` — `self._model.f.predict(cleaned, k, threshold, 'strict')`
   returns plain labels/probs without the `np.array(copy=False)` conversion. Verify the exact
   pybind signature against the installed `fasttext_pybind` before relying on it; wrap in the
   existing try/except. Zero dependency churn, ~3 lines.
2. Swap the dependency for a NumPy-2-compatible fork (e.g. a maintained `fasttext` build) —
   check licensing/wheel availability for macOS arm64 + Linux before choosing.
3. Do **not** pin `numpy<2` — the rest of the stack (onnxruntime 1.29, docling 2.120) is already
   on NumPy 2; a downgrade is a larger regression risk than the 3-line bypass.

Also demote the per-file WARNING to one WARNING + per-file DEBUG once detection is functional —
a systemic failure should not scale log volume with corpus size.

## Verification

- Unit test: `LanguageDetector.detect("The quick brown fox…", confidence_threshold=0.7)` returns
  `"en"` (and a Hungarian sample returns `"hu"`) under the shipped NumPy — this is the test that
  fails today with the ValueError-driven `""`.
- Integration: ingest a text file → chunk rows carry a non-empty `language`; no per-file WARNING.

## References

- [[archon_search/language_detector.py]] — `detect` → `predict` (:175-199)
- [[archon_search/pipeline.py]] — per-file catch/warn (:534-550)
- installed `fasttext/FastText.py:232` — the `copy=False` site (fasttext-wheel 0.9.2, numpy 2.5.2)
