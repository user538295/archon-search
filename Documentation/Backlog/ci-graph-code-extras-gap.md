# CI gap: `[graph]`/`[code]` extras never installed

**Status:** open, deferred — user will handle later.

## Problem

`.github/workflows/archon-search-pr.yml` installs deps with `uv sync --dev` only
(line 31). Neither `[graph]` nor `[code]` (`pyproject.toml:29-40`) is installed,
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

## Fix

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

Also add a non-skip assertion for this test surface, mirroring the existing
`Verify benchmark tests ran (not just skipped)` step (lines 82-83), so a
future extras-install regression fails loudly instead of silently skipping
again.

## Evidence trail

- `pyproject.toml:29-40` — `[graph]` and `[code]` optional-dependency groups.
- `.github/workflows/archon-search-pr.yml:30-31` — only `uv sync --dev`.
- `archon-search-release.yml:42-43` — same gap.
