# Feature Brief: Multilingual Server Crashes After `--skip-preload`

## Problem
A user who sets up a multilingual profile with `--skip-preload` gets a wizard that reports success but a server that crashes on every start — because the tiny language-detection model the server needs was never downloaded, and nothing downloads it later.

## Goal
Any multilingual install produces a server that starts. The ~1 MB language-detection model is always present after a completed multilingual setup, so the startup check passes instead of crashing.

## Users & Context
Anyone who runs the wizard with a multilingual profile **and** `--skip-preload` — typically people deferring the large model downloads (the embedder and reranker weights are hundreds of MB) to save time or work around a slow connection, and automated/CI installs. They expect that skipping the *heavy* downloads still leaves them with a working server. Today it leaves them with a server that won't boot and a technical error in a log.

## Core Flow
1. User runs the wizard with a multilingual profile and `--skip-preload` (interactively or non-interactively with `--multilingual`).
2. The wizard downloads the small language-detection model regardless of `--skip-preload`, because it is a required ~1 MB runtime asset, not one of the heavy model weights that flag is meant to skip.
3. The wizard still skips the heavy embedder and reranker downloads, exactly as `--skip-preload` promises.
4. The wizard finishes; the server starts normally in multilingual mode.

## In Scope
- Always download the language-detection model (`lid.176.ftz`) for a multilingual profile, even under `--skip-preload`.
- Always run the model's license step for a multilingual profile (see Key Decisions for how the non-interactive case is handled), since it currently rides on the same `--skip-preload` gate as the download.
- Tests: a multilingual install with `--skip-preload` downloads the model and produces a config that passes the server's startup check.

## Out of Scope
- The heavy embedder/reranker weights — they legitimately stay deferred under `--skip-preload` and download on first query via the existing mechanism. This fix does not touch them.
- A broader "install or download every missing dependency at server start" recovery path — that is the separate Future Iteration named in the multilingual-extra brief (`2026-07-15-040`) and would change the server's startup sequence. Not here.
- Changing where the model lives or how it is fetched (the existing download helper is reused as-is).

## Key Decisions
- **Always fetch the tiny model, keep skipping the heavy ones**: the download is decoupled from `--skip-preload` because the language-detection model is ~1 MB and *required*, while `--skip-preload` exists to skip the hundreds-of-MB embedder/reranker weights. Downloading a 1 MB required file is not what the user asked to skip. Chosen over a lazy download-at-startup approach (which would add network access and offline-failure handling to the server's startup path) and over a warning-only approach (which leaves the server unbootable).
- **License handling stays consistent with today**: for a multilingual profile the model's license (CC-BY-SA 3.0) must be accepted. In interactive mode the wizard still shows the acceptance prompt; in non-interactive mode the install requires `--accept-fasttext-license` and stops with a clear message if it is absent — the same rule that already applies to a multilingual install without `--skip-preload`.

## Edge Cases & Constraints
- **Model already present (re-run)**: the download helper is a no-op when the file already exists, so re-running the wizard costs nothing.
- **Download fails (e.g. no network)**: recommended handling — revert `multilingual = false` so the server still boots in English-only mode (reusing the rollback built for the multilingual package extra), rather than aborting the whole install. This keeps the feature's promise ("the server starts") true even when the tiny download fails, and matches the rollback philosophy already established for the package extra. Confirm with `/plan-maker`.
- **Non-interactive without the license flag**: the install stops with an actionable message telling the user to pass `--accept-fasttext-license` — no silent multilingual downgrade in this path (the user explicitly asked for multilingual and only omitted the license flag).
- **Non-multilingual profiles**: unchanged — no language model is involved.
- **Documentation contradiction to fix**: the wizard docs currently say `--skip-preload` means "models download on first query instead." That is true for the embedder/reranker but was never true for the language-detection model. The wizard/CLI docs must be updated to state that the small language model is always downloaded for multilingual profiles.

## Open Questions
- Confirm the exact edit: the fasttext license + download block in `run()` is gated by `if is_multilingual and not skip_preload:` (install.py Step 3b, ~line 1925). The fix removes the `and not skip_preload` condition so both the `_prompt_fasttext_license` call and the `_download_fasttext_model` call run for any multilingual profile. Verify no other behavior currently depends on that combined gate.
- Decide the download-failure path (abort with error vs. revert `multilingual=false` and continue English-only). Brief recommends the revert-and-continue path for consistency with `_revert_multilingual_flag` (from `2026-07-15-040`); `/plan-maker` should confirm and, if chosen, wire the revert at the Step 3b failure return the same way the extra-install failure does.
- `_check_multilingual_deps` (`server/app.py:86`) hard-fails on two conditions (package absent, model absent). This fix addresses the model-absent condition for the wizard path; confirm no change to the check itself is needed (the package-absent condition is already handled by `2026-07-15-040`).
- Confirm the smoke/e2e suites that pass `--multilingual --skip-preload` still behave correctly once the tiny model download is no longer skipped (they may need `--accept-fasttext-license` and a mocked `_download_fasttext_model`).

## Future Iterations
- The unified "install/download all missing dependencies at server start" recovery path (deferred from `2026-07-15-040`), which would also cover the graph and code extras and turn a startup crash into a self-heal.

## References
- `Documentation/Completed/2026-07-15-040-wizard-multilingual-extra-not-installed-brief.md` `[user+docs-agent]` — the multilingual package-extra fix; explicitly scoped the model-download step OUT, naming this as its deferred follow-up; source of the `_revert_multilingual_flag` rollback pattern reused here
- `archon_search/install.py` `[user+code-agent]` — `_download_fasttext_model` (~line 874, the reusable helper), the Step 3b license+download gate `if is_multilingual and not skip_preload` (~line 1925), and the config-write branches that write `multilingual=true`
- `archon_search/server/app.py` `[user+code-agent]` — `_check_multilingual_deps` (line 86, called at startup ~line 258) hard-crashes when the model file is absent; `_multilingual_model_path()` and the `LanguageDetector` instantiation
- `archon_search/language_detector.py` `[code-agent]` — `LanguageDetector.__init__` raises `FileNotFoundError` if the model is missing; confirms there is no lazy-load at runtime
- `archon_search/cli/install_cmd.py` `[code-agent]` — threads the `--skip-preload` and `--accept-fasttext-license` flags into `run()`
- `archon_search/pipeline.py` `[code-agent]` — pipeline factory also instantiates `LanguageDetector` when `multilingual=true`, confirming the model is required, not optional
- `Documentation/UserManual/02_wizard.md` `[docs-agent]` — documents `--skip-preload` as "models download on first query" (line 401) — the contradiction this fix resolves — and the multilingual model download step
- `Documentation/UserManual/01_installation.md` `[docs-agent]` — documents the `lid.176.ftz` download location and CC-BY-SA 3.0 license requirement
- `Documentation/Completed/C2-multilingual-retrieval-plan.md` `[docs-agent]` — original design of `_check_multilingual_deps` and its two distinct startup errors

## Recommendation
This is worth doing now: it is a completed-wizard-then-dead-server experience for a real user path (`--multilingual --skip-preload`, including CI), and the fix is small — dropping one condition so a ~1 MB required file is no longer treated like a heavy optional download. The hardest part is not the code but the judgment call on download failure (abort vs. degrade to English-only); pick degrade-to-English so the server always boots, consistent with the rollback already shipped in `2026-07-15-040`. Do not compromise the license gate — the non-interactive path must still require `--accept-fasttext-license`.
