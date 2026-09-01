# CI gap (RESOLVED): `[graph]`/`[code]` extras never installed

**Status:** RESOLVED (2026-09-01) — extras are installed today, via a self-referencing
`dependency-groups` entry rather than the explicit `--extra` flag originally proposed below.
Residual risk: this is implicit — removing `pyproject.toml:77` would silently reopen the gap,
and the "non-skip assertion" follow-up below was never implemented, so no CI guard would catch it.

## Problem (as originally filed — line numbers below are as originally reported, since corrected in Evidence trail)

`.github/workflows/archon-search-pr.yml` installs deps with `uv sync --dev` only
(line 32). Neither `[graph]` nor `[code]` (`pyproject.toml:41-58`) is installed,
so every test gated on those extras silently skips on every CI run via
`pytest.importorskip("tree_sitter")` / `pytest.importorskip("leidenalg")`.

Confirmed affected (grep across `tests/`):
- `tests/test_defref_extractor.py`
- `tests/test_chunker.py` (AST chunking tests)
- `tests/test_e2g_be3_pipeline_defref_hook.py`
- `tests/integration/test_e2g_be3_defref_pipeline_integration.py`
- `tests/integration/test_defref_extractor_integration.py`
- `tests/integration/test_ast_chunker_integration.py`
- `tests/integration/test_be5_swift_csharp_forced_failure.py`
- `tests/test_be5_community_builder_seed.py`
- `tests/eval/conftest.py`, `tests/eval/test_real_community_eval_backend.py`
- `tests/eval/test_code_lane_eval_gate.py` (BE-10, new)

This means the entire E2G code-def/ref-graph feature (BE-1 through BE-10) has
never actually executed in CI — only locally, where the dev venv happens to
have the extras installed.

## Resolution (superseding note)

Commit `a15673e2` ("test: install all feature extras so no test skips; fix
drifted tests", 2026-07-28) changed `pyproject.toml`'s `[dependency-groups].dev`
list (`pyproject.toml:64-82`) to include a self-referencing extras pull —
`"archon-search[multilingual,hyde,rag_fusion,ollama,openai-provider,graph,code]"`
(`pyproject.toml:77`). Because both `archon-search-pr.yml` and
`archon-search-release.yml` install deps via `uv sync --dev`, this
self-reference now pulls in both `[graph]` and `[code]` on every CI run — not
via an explicit `--extra graph --extra code` flag as originally proposed below,
but the effect is the same: the gap described above no longer exists.

## Fix (original proposal — superseded, kept for history)

Add a step installing both extras before the test steps in
`archon-search-pr.yml`, e.g.:

```yaml
- name: Clean install (uv sync --dev)
  run: uv sync --dev --extra graph --extra code
```

Consider caching the `en_core_web_sm` spaCy model download and tree-sitter
grammar builds (mirrors the existing fastembed/HuggingFace cache step) since
`[graph]` pulls a ~500MB model and `[code]` compiles 9 tree-sitter grammar
packages — expect CI to get noticeably slower.

## Follow-up (still open, NOT superseded by the Resolution above)

Add a non-skip assertion for the graph/code test surface, mirroring the existing
`Verify benchmark tests ran (not just skipped)` step (`archon-search-pr.yml:84-85`),
so a regression that removes the `pyproject.toml:77` self-reference fails CI loudly
instead of silently skipping again. This was proposed in the original report and has
never been implemented.

## Evidence trail

- `pyproject.toml:41-58` — `[graph]` and `[code]` optional-dependency groups.
- `pyproject.toml:64-82` — `[dependency-groups].dev`, with the self-referencing
  extras pull at `pyproject.toml:77`.
- `.github/workflows/archon-search-pr.yml:32-33` — `uv sync --dev` (now pulls
  `[graph]`/`[code]` transitively via the dev-group self-reference above).
- `.github/workflows/archon-search-release.yml:44-45` — same `uv sync --dev`,
  same resolution.
