# Feature Brief: Wizard Install Output — Two Cosmetic Fixes

## Problem
During setup, two unrelated technical messages appear that users cannot act on and cannot ignore: (1) when the wizard tries to download the spaCy language model for graph analysis, it fails with "No virtual environment found" — a Python packaging error that looks like a broken install; (2) when pre-downloading the embedding model, the library prints a warning about a "pooling method" change — an internal numerical detail that has no user-visible effect. Both leave users uncertain whether their install succeeded.

## Goal
The wizard completes without printing any unexplained technical warnings — users see clean progress output and finish confident the install worked.

## Users & Context
- **Bug A (spaCy):** Any user who enables graph features during setup. They see a red-looking warning mid-install; the model actually downloads successfully at first use, so the install is not broken — but the error looks fatal.
- **Bug B (fastembed warning):** Any user who chooses a multilingual profile. The warning appears during the model pre-download step and sounds like something changed in a way that might break search quality (it didn't — mean pooling is the correct behavior for this model).

## Core Flow

**Bug A — spaCy download:**
1. User enables graph features during setup.
2. The wizard runs `python -m spacy download en_core_web_sm` to pre-install the language model.
3. Instead of failing silently (the current behavior, which is already handled by a warning fallback), the download succeeds — because the command now includes the flag required to install into a non-venv Python environment (`--system`), or uses the `uv pip install` route consistent with all other extra installs.

**Bug B — fastembed warning:**
1. User chooses a multilingual profile.
2. The wizard pre-downloads the embedding model (`TextEmbedding(...)` call in `install.py:455`).
3. The UserWarning about pooling method is suppressed at that call site — the model downloads and the wizard continues without printing anything.

## In Scope
- **Bug A:** Add `--system` to the `spacy download` subprocess call in `_install_graph_extra()`, or replace the subprocess call with a `uv pip install` approach consistent with `_install_extra()`.
- **Bug B:** Wrap the `TextEmbedding(profile.embedder, lazy_load=True)` call in `install.py:455` with a `warnings.filterwarnings("ignore", category=UserWarning, message=".*mean pooling.*")` filter (or equivalent narrowly-scoped suppression).
- Tests for both: confirm spaCy download no longer surfaces the venv error in a tool-install context; confirm the fastembed warning does not appear in wizard output.

## Out of Scope
- Changing what spaCy is used for, or which model is downloaded — that is the graph feature's decision.
- Changing which embedding model the multilingual profile uses — that is a profile configuration decision.
- Suppressing warnings at server runtime (only at wizard install time).

## Key Decisions
- **Suppress the fastembed warning rather than pinning the library version**: The "pooling method" change (CLS → mean) is actually the correct behavior for `intfloat/multilingual-e5-large`. Pinning to the old version would deliver lower-quality search results. Suppressing the warning at the call site is the right call.
- **Prefer `--system` flag for spaCy download over replacing the approach entirely**: The `--system` flag is the minimal, targeted fix. Replacing the whole download mechanism with `uv pip install en-core-web-sm` (the pip package name for the model) is also valid and more consistent with the rest of the install flow — plan-maker should decide which is cleaner given the test surface.

## Edge Cases & Constraints
- **Bug A — warning suppression is already there**: The current code already catches `CalledProcessError` and prints a non-fatal warning if spaCy download fails; this fix prevents the failure from happening in the first place rather than changing the fallback behavior.
- **Bug B — narrow filter scope**: The `warnings.filterwarnings` call must target the specific message string to avoid suppressing unrelated fastembed warnings that might be actionable in the future. Use `warnings.catch_warnings()` as a context manager scoped to the `TextEmbedding(...)` line.
- **Non-uv installs**: If `--system` is the chosen fix for Bug A, verify it is supported by the spaCy version pinned in `pyproject.toml` (`spacy>=3.8,<3.9`).

## Decisions

- **Bug A — spaCy download approach:** Replace the `python -m spacy download` call with `uv pip install --python <sys.executable> en-core-web-sm`. This is consistent with how `_install_extra()` installs every other package and removes the environment-detection assumption that caused the bug. The `--system` flag is a narrower fix but stays within spaCy's tooling with different environment assumptions from the rest of the installer. Confirm the PyPI wheel name `en-core-web-sm` matches the `spacy>=3.8,<3.9` pin before shipping.
- **Bug B — fastembed warning suppression scope:** Suppress only at `install.py:455` (the confirmed site). Use `warnings.catch_warnings()` as a context manager scoped to the `TextEmbedding(...)` line with a narrow message filter. Check `embedder.py` separately — if the warning fires at server startup, add suppression there with evidence. Suppressing a call site where you haven't confirmed the warning fires is speculative and could hide a real future warning if the message string changes.

## Future Iterations
- A unified "clean install output" pass that audits all subprocess calls in `install.py` for similar environment-assumption failures — there may be other edge cases in tool-install contexts.

## References
- `archon_search/install.py` `[user+code-agent]` — `_install_graph_extra` spaCy download (lines 1323–1338); fastembed prewarm call (line 455)
- `pyproject.toml` `[code-agent]` — `[graph]` extra: `spacy>=3.8,<3.9`; `[multilingual]` extra
- `Documentation/UserManual/05_searching.md` `[docs-agent]` — Documents manual spaCy install; does not warn about tool-install context
- `Documentation/Completed/e1a-graphrag-entity-extraction-naive-mode-brief.md` `[docs-agent]` — Notes spaCy model auto-downloads at first use (the existing fallback)

## Recommendation
Both fixes are small and safe — each is a one- to three-line change with no architectural risk. Bundle them in a single planning session. The spaCy fix has a clear right answer once the `--system` flag is verified against the pinned version; the fastembed fix is a one-liner. Neither should block on anything. Ship alongside bug-031 or immediately after.
