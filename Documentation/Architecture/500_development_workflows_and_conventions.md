**Purpose**: Describe the day-to-day development workflow for `archon-search` — dependency management, common commands, naming conventions, coding standards, and PR expectations.
**Audience**: Contributors and maintainers working on the `archon-search` codebase.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Development Workflows and Conventions

This document captures the local development loop and the conventions that contributors are expected to follow. Anything stricter than "preference" is called out explicitly; everything else falls back to what the surrounding code already does.

## Principles

1. **`uv` is the only supported package manager.** Dependencies, virtual environments, and tool invocations all go through `uv`. There is no `requirements.txt` and no `setup.py` in this repo. A `pip install -e .` invocation would still resolve against `pyproject.toml`, but it is not a supported parallel workflow and is not maintained.
2. **TDD-first, 85% coverage is a floor.** Tests come before implementation, and the default `pytest` invocation enforces `--cov-fail-under=85`. A red coverage run blocks merge.
3. **Surgical changes, one task per commit.** Edits should touch only what the task requires. When a plan or task list is executed, each task gets its own commit; commits and tasks map 1:1.
4. **Warning-free at all times.** Deprecation warnings and runtime warnings are treated as defects, not noise. Resolve them as part of the change that introduced them.
5. **No backward-compat hacks.** Breaking changes are recorded in `BREAKING.md` (see `510_release_and_environment_strategy.md`). The codebase does not carry shim layers, version-sniffing branches, or "compat" modules.

## Dependency Management with `uv`

`pyproject.toml` is the single source of truth. Runtime dependencies live under `[project].dependencies`; development tools live under `[dependency-groups].dev` (currently only `pytest`, `pytest-asyncio`, and `pytest-cov` — no linter or type-checker is declared as a dev dependency). `[tool.uv].package = true` tells `uv` to treat this repo as an installable project.

```bash
# Bootstrap a fresh checkout (creates .venv, installs runtime + dev deps).
uv sync --dev
```

Do not add dependencies by hand-editing `uv.lock`. Use `uv add <pkg>` (or `uv add --dev <pkg>`) so the lockfile stays consistent.

## Common Commands

Reproduced from the project `CLAUDE.md` so contributors do not have to context-switch:

```bash
# Dev install
uv sync --dev

# Run the server (entry point archon_search.cli.main:main)
uv run archon-search

# Full test suite (default addopts enforce --cov-fail-under=85)
uv run pytest

# Single test file / single test
uv run pytest tests/test_router.py
uv run pytest tests/test_router.py::test_name -x

# Skip coverage locally (developer override only — never bake into addopts)
uv run pytest --no-cov

# Targeted marker runs (these markers ALSO run in the default suite; listed for explicit targeting)
# Note: `--thresholds-path tests/eval/thresholds.toml` is already baked into `addopts`
# in pyproject.toml, so gated eval tests run by default. `-m eval` here narrows to just
# that marker and is kept as the canonical form (matches CLAUDE.md).
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
uv run pytest -m integration
uv run pytest -m live
uv run pytest -m benchmark   # needs a running server; auto-skips if unreachable

# live_benchmark and smoke suites — excluded from default runs; run separately
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov
uv run pytest tests/smoke/ --no-cov

# Cut a release (tag + push; CI runs eval + publishes to PyPI via OIDC)
bash release.sh           # interactive
bash release.sh -y        # non-interactive
bash release.sh --dry-run
```

The default `pytest` invocation excludes only the `live_benchmark` and `smoke` markers via `addopts` in `pyproject.toml` (`-m "not live_benchmark and not smoke"`); `live`, `eval`, `benchmark`, and `integration` markers run in the default suite and skip gracefully when their required infrastructure is absent. See `200_testing_strategy.md` for the full marker layout.

The default run is parallel by default (`-n 4 --dist=loadgroup` in `addopts`). The worker count is deliberately capped at 4 — never raise it back to `-n auto` (see `CLAUDE.md`). For debugging use `-n0` to run serially: `uv run pytest -n0`. Fail-fast requires `-n0 -x` and stdout passthrough requires `-n0 -s` (xdist suppresses both by default).

## Naming Convention: Package vs. Distribution

This trips people up often enough to deserve its own section.

| Surface             | Spelling         | Why                                                                          |
| ------------------- | ---------------- | ---------------------------------------------------------------------------- |
| PyPI distribution   | `archon-search`  | Hyphenated — PyPI convention for project names.                              |
| Importable package  | `archon_search`  | Underscored — Python identifier rules forbid hyphens in module names.        |
| On-disk directory   | `archon_search/` | Matches the importable name.                                                 |
| CLI script          | `archon-search`  | Defined under `[project.scripts]`; hyphenated to match the distribution.    |

Because the two names differ, `pyproject.toml` contains an explicit instruction to the build backend:

```toml
[tool.hatch.build.targets.wheel]
# The project name uses a hyphen (PyPI convention) but the importable package
# directory uses an underscore — tell hatchling explicitly so it does not try
# to look for a directory named `archon-search`.
packages = ["archon_search"]
```

**Do not "fix" this entry.** Removing or normalising it will cause `hatch build` to fail with "package directory not found". The mismatch is deliberate and load-bearing.

## Coding Standards

These are not aspirational; they are merge gates.

### Tests First (TDD is mandatory)

- Write the happy-path test before the implementation. Add edge-case tests as separate commits when the behaviour they cover is added.
- A failing test in the suite blocks all other work. Fix it before moving on.
- The default `pytest` run enforces `--cov-fail-under=85`. Coverage that dips below the floor must be restored in the same change that caused the drop.

For details on the marker layout, the eval harness, and how the 85% floor is enforced across CI matrix runs, see `200_testing_strategy.md`.

### Warning-Free Codebase

- Resolve all warnings — Python `DeprecationWarning`, `RuntimeWarning`, and library-emitted deprecations. (Ruff and mypy are not configured as merge gates in this repo: no `[tool.ruff]` or `[tool.mypy]` sections exist in `pyproject.toml`, and neither is a declared dev dependency. If a contributor runs them locally, the warning-free expectation extends there too. #Unverified — no CI step enforces a warning-free run.)
- If a warning is genuinely unavoidable (e.g. an upstream bug), silence it at the narrowest possible scope and add a comment explaining why.

### No Backward Compatibility

- Breaking changes go in `BREAKING.md` with a migration note. The codebase does not carry compatibility shims past one release.
- CalVer segments encode time, not compatibility. Consumers subscribe to `BREAKING.md`, not to the version number.

### No Smelling Code

- Minimum code that solves the problem. No speculative abstractions, no "flexibility" the task didn't ask for, no error handling for impossible scenarios.
- If a 200-line change could be 50 lines, rewrite it.

## PR Workflow Expectations

1. **One task per commit.** When working from a plan or task list, every task produces exactly one commit with a non-empty diff. Do not batch multiple tasks into a single commit; do not split one task across two commits.
2. **Commit messages explain the why.** The diff explains the what. Use the imperative mood ("add", "fix", "refactor"), 1–2 sentences in the body when the change isn't self-evident.
3. **Surgical scope.** Every changed line should trace directly to the stated task. Do not "improve" adjacent code, reformat unrelated files, or refactor things that aren't broken.
4. **Clean up your own orphans.** Imports, variables, and helpers that your changes made unused get removed in the same commit. Pre-existing dead code stays unless the task asks for it.
5. **Tests pass, coverage holds, warnings stay at zero.** A PR that breaks any of the three is not ready. (Tests + coverage are enforced by `pytest` addopts and CI; the warnings-at-zero rule is a project convention — no CI step currently fails the build on warnings. #Unverified)
6. **REST/MCP changes update `BREAKING.md`.** If the change alters request/response shapes, endpoint paths, MCP tool signatures, or auth behaviour, add a `BREAKING.md` entry in the same PR.

## Related Documents

- Release flow, CI workflows, and environment layout: `510_release_and_environment_strategy.md`
- API design principles and contracts: `520_api_design_and_contracts.md`
- Endpoint reference (do not duplicate route lists here): `600_api_reference_or_public_interface.md`
- Test layout, markers, and the eval harness: `200_testing_strategy.md`
- Security and privacy posture (auth, telemetry, ACL): `150_security_and_privacy_architecture.md`
