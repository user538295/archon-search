# Feature Brief: Replace fasttext-predict with lingua for language detection

## Problem

Language detection for the `multilingual` feature depends on `fasttext-predict`, a package whose public source repository no longer exists (`github.com/searxng/fasttext-predict` returns 404, checked 2026-10-29), whose last release is 2024-11-23, and which publishes no CPython 3.14 wheel (so installing it there needs a C++ compiler). Its only maintained successor, `fasttext-predict-v2`, is used by almost nobody (2,149 downloads in 30 days, 0 stars, 0 forks). The project must not rest on a package that cannot be inspected or that few others use.

## Goal

Language detection works from a widely used, maintained, openly accessible package that installs from a ready-made wheel on Python 3.13 and 3.14 with no compiler, no model download and no separate licence prompt. `lingua-language-detector` (Apache-2.0, 2.83 million downloads in 30 days, 1.8k GitHub stars, wheels for CPython 3.13 and 3.14 on Linux, macOS and Windows) is the chosen replacement. Documents ingested with `[database].multilingual = true` still get the same ISO 639-1 language code on every chunk, and the full-text-search tokenizer is still chosen from the dominant language.

## Users & Context

- Operators who enable `[database].multilingual = true`, choose a multilingual install profile in the wizard, or run the Docker image with `ARCHON_EXTRAS` containing `multilingual`.
- They want ingestion to tag documents by language and want installation to be one step, with no extra download.

## Core Flow

1. The operator installs archon-search with the `multilingual` extra; the extra installs `lingua-language-detector` and nothing else for language detection.
2. The operator turns `[database].multilingual` on and starts the server; startup checks that the package is importable.
3. The operator ingests a document; the pipeline detects the language from the first 2000 characters of its text.
4. The detected ISO 639-1 code is stored on every chunk of the document; the dominant language of a collection selects the full-text-search tokenizer.
5. If detection is not confident enough, or the language is not supported, the chunks are tagged as unknown, exactly as today.

## In Scope

- Replace the `fasttext-predict` implementation behind `LanguageDetector.detect(text, *, confidence_threshold)` with lingua, keeping the same contract: async, 2000-character truncation, ISO 639-1 result or `unknown`.
- Map lingua's results to the ISO 639-1 codes the store already expects (including Norwegian Bokmål and Nynorsk).
- Re-calibrate the confidence threshold for lingua's confidence scale and keep it configurable the way it is today.
- Remove what only existed for fasttext: the `lid.176.ftz` model download, the CC-BY-SA licence prompt and the `--accept-fasttext-license` flag, the fasttext models directory, the model-size estimate in pre-warm, the wizard feature bullet naming fasttext.
- Update the `multilingual` extra in `pyproject.toml` and `uv.lock`, the Docker image and the compose files if they reference fasttext, and every error message and doc that names fasttext.
- Update tests, documentation, and `BREAKING.md` plus the migration index.

## Out of Scope

- Detecting more languages than lingua supports (75) — documents in other languages are tagged unknown; widening coverage is a separate decision.
- Per-chunk or per-paragraph language detection — detection stays document-level.
- Re-tagging documents already ingested with fasttext — they keep their stored codes until re-ingested.
- Changing the Python floor (it stays 3.13 or later) or the Docker base image.
- Model2Vec, GlotLID, OpenLID, py3langid and langdetect — evaluated and rejected (see Key Decisions).

## Key Decisions

- **lingua over fasttext-predict-v2:** 2,149 versus 2.83 million downloads a month; the v2 fork has no public community. **Reopened 2026-10-30** after the smoke run and source diff in "Alternative: stay on fastText" below; the decision is pending.
- **lingua over fasttext-predict:** its source repo is gone and it has no 3.14 wheel.
- **lingua over langdetect:** langdetect has 10 million monthly downloads but its last release is 2021, it covers 55 languages, it returns `zh-cn` and `zh-tw` instead of `zh`, it has no `nb` or `nn`, and its results change between runs on short text.
- **lingua over py3langid:** 68 stars and 0.51 million monthly downloads, one maintainer, accuracy figures cover an unreleased model.
- **Accepted costs:** a 166 MB wheel, 75 languages instead of 176, about 1 ms instead of 0.08 ms per 2000-character document, and a different confidence scale. Hungarian is supported.
- **Evidence:** research report in the session of 2026-10-29 (`LangIdAlternatives`), PyPI JSON, pypistats.org and the GitHub pages for each repository.

## Alternative: stay on fastText (v2 package or our own fork)

Added 2026-10-30 after an investigation. This option was not weighed above and may beat lingua on cost; the choice is open.

**Verified facts**

- Upstream `facebookresearch/fastText` is archived (GitHub API: last push 2024-03-22), MIT-licensed.
- `fasttext-predict-v2` 1.1.0 (2026-08-19, snapAddy GmbH, MIT, `>=3.11`) ships wheels for cp311 to cp314 and a sdist with a cibuildwheel config. PyPI metadata has no repository URL.
- Its `src/` C++ sources are byte-identical to `fasttext-predict` 0.9.2.4 (`diff -r` of the two sdists): the new release only repackages the old code with a newer wheel matrix. `fasttext-predict` 0.9.2.4 has wheels up to cp313 only.
- Smoke run: `fasttext-predict-v2` 1.1.0 installed from wheels on Python 3.13.14 and 3.14.10 (macOS arm64) next to numpy 2; `lid.176.ftz` loaded; a Hungarian sentence returned `hu` at 0.997 and "Hello world" returned `en` at 0.168. The `import fasttext` API and results are the same as today, and the numpy `copy=False` failure of the older brief 2026-08-19-070 does not occur (this package does not use numpy).
- `lid.176.ftz` is 938,013 bytes, CC-BY-SA 3.0, hosted at `dl.fbaipublicfiles.com` (HTTP 200 on 2026-10-30).

**Options**

| | A. Switch to `fasttext-predict-v2` | B. Own fork of the v2 sdist | C. lingua (this brief) |
|---|---|---|---|
| Code change | dependency line in `pyproject.toml` plus relock | same, plus a new repo, CI and PyPI name | about 60 files |
| Languages / time per document | 176 / 0.08 ms | 176 / 0.08 ms | 75 / about 1 ms |
| Threshold recalibration | no | no | yes |
| Python 3.14 wheel | yes | yes, we build it | yes |
| Licence prompt and model download | stay | stay | go |
| Ongoing owner | snapAddy, one account | us | none |

**Can we fork v2? Yes.** The licence is MIT (the sdist `LICENSE` carries the Facebook copyright and permits copying), and the full C++ source is in the sdist, so the missing public repository is no obstacle. A fork would:

- start from the v2 sdist (or from upstream plus `fasttext-predict`'s predict-only reduction), commit it to a public repo and keep the LICENSE and copyright notice;
- publish under a new PyPI name (PyPI rejects direct-URL dependencies in published packages, so a git dependency in the `multilingual` extra will not work), with PyPI trusted publishing as the release flow already uses;
- keep a cibuildwheel matrix (manylinux and musllinux on x86_64 and aarch64, macOS x86_64 and arm64, Windows AMD64 and ARM64) and add one new CPython wheel every October;
- expose the same `import fasttext` module name as v2, so it conflicts with any other fasttext distribution in the same environment; this is acceptable only if the fork is the sole provider.

**What no variant of this option fixes**

- The model is frozen and the C++ is archived; we would maintain only the wrapper.
- The CC-BY-SA licence prompt and the download from Facebook's host stay unless we mirror the model, and mirroring or bundling it carries share-alike terms that need a licence review [INFERENCE: not reviewed].
- v2 as it stands depends on one maintainer account with no public source link, which is the risk the brief wanted to remove; the fork removes it at the price of owning the build.

**Suggested staging:** take A now (one line, restores 3.14), vendor the v2 sdist with its hash into our own storage as insurance, and open B only if v2 stops publishing or yanks a release. Take C only if dropping the licence prompt and download outweighs 176-to-75 languages, the 166 MB wheel and the recalibration.

## Edge Cases & Constraints

- Very short text (one or two words): lingua may be wrong or unsure; the confidence gate must send it to unknown, as the 0.7 gate does today for "Hello world".
- Text in a language lingua does not support: tagged unknown instead of the nearest supported language; this is a coverage regression against `lid.176`'s 176 languages and must be documented.
- Mixed-language documents: only the first 2000 characters decide, as today.
- Memory: lingua loads language models into memory; the detector must be built once and reused, and it must be safe to call from `asyncio.to_thread`.
- Container image and install size grow by about 166 MB because lingua bundles its models in the wheel.
- Existing installs that already downloaded `lid.176.ftz`: the file is no longer read; nothing deletes it.

## Open Questions

- What confidence threshold replaces 0.7, and does lingua's relative confidence need a different rule (for example a minimum distance between the top two languages)?
- Does removing the CC-BY-SA licence gate need a licence review of lingua's bundled language data (training-data terms were not verified)?
- Should the detector be limited to a configured subset of languages to reduce memory, or always load all 75?
- Which lingua accuracy mode (high or low) fits the 2000-character document-level use?
- Does the `multilingual` extra keep its name, and does the wizard keep a separate "language detection" step now that nothing is downloaded?
- Should Python 3.14 support return now that lingua ships 3.14 wheels (separate change)?
- Is dropping the CC-BY-SA licence prompt and the `lid.176.ftz` download worth moving from fastText (176 languages, 0.08 ms per document, no recalibration) to lingua (75 languages, about 1 ms, 166 MB wheel, new threshold)? If not, this brief is replaced by a dependency change to `fasttext-predict-v2`.
- Should we publish our own fork of the `fasttext-predict-v2` sdist now, or only once v2 yanks a release or publishes no wheel for a new CPython version?
- If we publish a fork: who owns the repository, the PyPI name and the yearly wheel update, and does mirroring `lid.176.ftz` need a licence review first?

## Future Iterations

- Restore Python 3.14 support, since lingua has 3.14 wheels on every platform.
- Per-chunk language detection for mixed-language documents.
- Widen language coverage beyond 75 if a real corpus needs it.

## Recommendation

Do it, but as its own release after the current one: the change touches about 60 files (14 package files, 29 tests, 17 documents per the research report) and needs a full re-run of the release gate. The hardest part is the confidence threshold: lingua's scores are not comparable with fastText's probabilities, so calibrate it on real sample documents including Hungarian before choosing a number. Do not drop the Hungarian samples from the tests.

**Update 2026-10-30:** the smoke run shows `fasttext-predict-v2` works today on Python 3.13 and 3.14 with only a dependency-line change. The 60-file lingua change is justified only if the licence prompt and the model download are themselves unacceptable; settle the first two new Open Questions (fastText versus lingua, fork now versus later) before starting.

---

## References

- **Team plan:** [2026-10-29-010-replace-fasttext-with-lingua-language-detection-team-plan.md](./2026-10-29-010-replace-fasttext-with-lingua-language-detection-team-plan.md)
