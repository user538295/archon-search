# Review: Architecture/500_development_workflows_and_conventions.md

## Summary

The document is broadly accurate. Almost every concrete claim about commands, naming, build configuration, markers, and PR conventions verifies against `pyproject.toml`, `release.sh`, `CLAUDE.md`, and the source. A small number of statements are minor over-specifications or could not be verified from authoritative sources (they reference policy / forward-looking docs rather than code).

Verified against: `pyproject.toml`, `release.sh`, `.github/workflows/archon-search-{pr,release}.yml`, `CLAUDE.md`, `contributing.md`, `tests/eval/conftest.py`, `archon_search/cli/main.py`, repo file listing.

## Inaccuracies (numbered)

1. **Line 21 — `[dependency-groups].dev` description.** The doc says "development tools live under `[dependency-groups].dev`." This is correct in form, but the doc then implies all dev *tooling* lives there. `pyproject.toml` `[dependency-groups].dev` only contains `pytest`, `pytest-asyncio`, `pytest-cov` — no Ruff, no mypy. The later mention of "tool warnings (Ruff, mypy if used locally)" (line 102) is consistent with this, but the contrast is worth noting: Ruff/mypy are *not* declared dev dependencies in this repo. Minor — not strictly an inaccuracy, but the doc reads as if a richer dev-tools set is configured.

2. **Line 102 — "tool warnings (Ruff, mypy if used locally)".** Ruff and mypy are not declared anywhere in `pyproject.toml` (no `[tool.ruff]`, no `[tool.mypy]`, not in `[dependency-groups].dev`). Claiming them as warning sources to resolve overstates what the project actually configures. They are not part of the merge-gate toolchain.

3. **Line 21 — "There is no … no `pip install -e .` workflow to maintain in parallel." (line 13).** Technically `pip install -e .` would still work against the `pyproject.toml`; the doc presents it as if it were structurally impossible. This is a policy statement, not a fact about the build system. Minor.

4. **Line 52 — eval invocation.** The doc shows `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`. The `--thresholds-path` option is only registered in `tests/eval/conftest.py`, so the option is only known when pytest collects `tests/eval/`. The command as written works (because `tests/eval/test_eval_suite.py` is in the path), but the `-m eval` filter is redundant given the explicit file path. Not wrong, just slightly noisy. The same command form appears in `CLAUDE.md`, so the doc is at least consistent with the project's stated canonical form.

## Verified claims

- Line 13: `uv` as sole package manager — confirmed by `[tool.uv].package = true` in `pyproject.toml` line 69-70 and absence of `requirements.txt` / `setup.py` / `setup.cfg` in repo root.
- Line 14, 41, 61, 96: `--cov-fail-under=85` enforced in default `pytest` — confirmed in `pyproject.toml` `[tool.pytest.ini_options].addopts` line 61.
- Line 17, 107, 122: `BREAKING.md` exists at repo root — confirmed.
- Line 21: `[project].dependencies` and `[dependency-groups].dev` exist — confirmed in `pyproject.toml`.
- Line 25, 36: `uv sync --dev` — standard `uv` command, consistent with `CLAUDE.md`.
- Line 38: entry point `archon_search.cli.main:main` — confirmed in `pyproject.toml` line 22 (`[project.scripts] archon-search = "archon_search.cli.main:main"`) and `archon_search/cli/main.py` exists.
- Line 39: `uv run archon-search` — consistent with `[project.scripts]` and `CLAUDE.md`.
- Line 51–55: marker-gated suites (`live`, `eval`, `benchmark`, `integration`) — all four markers confirmed in `pyproject.toml` `[tool.pytest.ini_options].markers` (lines 62-67) and in the addopts deselect filter `-m 'not live and not eval and not benchmark and not integration'`.
- Line 55: `benchmark` auto-skips when server unreachable — confirmed by marker description in `pyproject.toml` line 63.
- Line 58–60: `release.sh` flags `-y`, `--yes`, `--dry-run` — confirmed in `release.sh` argument-parsing block.
- Line 57: "Cut a release (tag + push; CI runs eval + publishes to PyPI via OIDC)" — confirmed by `release.sh` header comment and presence of `archon-search-release.yml` workflow.
- Lines 69–86: Package vs distribution naming (`archon-search` distribution, `archon_search` package, explicit `[tool.hatch.build.targets.wheel].packages = ["archon_search"]`) — exactly matches `pyproject.toml` lines 1-2, 22, 45-49. Quoted toml block (lines 78-84) is verbatim from `pyproject.toml`.
- Line 86: "Removing or normalising it will cause `hatch build` to fail with 'package directory not found'." — accurate behavior of hatchling auto-discovery when project name has hyphens. Consistent with CLAUDE.md's "don't 'fix' it".
- Line 108: CalVer (`YY.M.<rev-count>`) and `hatch-vcs` — confirmed in `pyproject.toml` lines 32, 35-43, and `release.sh` step 2.
- Lines 115–120: PR / commit conventions (one task per commit, surgical scope) — match `CLAUDE.md` Behavioral Guidelines and `contributing.md`'s commit-discipline rules.

## Unverifiable / ambiguous

- Line 3 ("Status: Draft") and lines 4–5 (review dates 2026-05-20 / 2026-08-20) — metadata, not verifiable against code.
- Line 16 "Warning-free at all times" — policy statement; matches the user's global `CLAUDE.md` directive but not enforced by any CI configuration I could find (no `-W error`, no Ruff/mypy gates).
- Line 28 "Do not add dependencies by hand-editing `uv.lock`." — convention, not enforced by code. Standard `uv` practice.
- Line 94 "Add edge-case tests as separate commits when the behaviour they cover is added." — policy; consistent with CLAUDE.md but no mechanical enforcement.
- Line 98, 126–130: cross-references to sibling architecture docs (200, 510, 520, 600, 150). Existence of those filenames is not verified here (out of scope for this review), but the per-doc CLAUDE.md "Documentation map" lists all of them, so the references are plausible.
- Line 121 "warnings stay at zero" as a PR merge gate — aspirational, no CI step found that fails on warnings.
