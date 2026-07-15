**Purpose**: Onboard contributors to `archon-search` — local setup, the development loop, and the rules that gate a merge.
**Audience**: External contributors and new maintainers.
**Status**: Stable
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Contributing

Thanks for your interest in `archon-search`. This page is the entry point for contributors; the full conventions live under [`Documentation/Architecture/`](Documentation/Architecture/) and are cross-linked below.

## Principles

1. **Code is the source of truth.** `GET /openapi.json`, `BREAKING.md`, and the test suite are authoritative; docs explain intent.
2. **TDD-first.** Write tests before implementation. Start with happy paths, then edge cases.
3. **One task, one commit.** Each task in a plan or PR scope maps to a single commit with non-empty file changes.
4. **OpenAPI and `BREAKING.md` are the contracts.** Any change to a REST or MCP surface must land in both.
5. **Warning-free, 85% coverage.** The default `pytest` run enforces `--cov-fail-under=85` and treats warnings as defects.

## Local setup

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
uv run pytest
```

`uv` is the only supported package manager — do not edit `uv.lock` by hand; use `uv add` / `uv add --dev`. For running the server and configuring `~/.archon-search/`, see [`Documentation/quick_start.md`](Documentation/quick_start.md).

## Development workflow

1. Branch off `main`.
2. Write tests first; then implement until they pass and coverage holds.
3. If you change a public REST or MCP contract, add a `BREAKING.md` entry describing what changed, the migration path, and the release it was announced in.
4. If you add or move a doc under `Documentation/`, update the index in [`Documentation/Architecture/990_documentation_index_and_contribution_guide.md`](Documentation/Architecture/990_documentation_index_and_contribution_guide.md). Orphan docs are a smell.
5. Keep changes surgical: touch only what the task requires, match surrounding style.

## Coding standards

Full standards live in [`Documentation/Architecture/500_development_workflows_and_conventions.md`](Documentation/Architecture/500_development_workflows_and_conventions.md). The load-bearing invariants:

- Package directory is `archon_search/` (underscore); distribution is `archon-search` (hyphen). The `[tool.hatch.build.targets.wheel].packages` setting in `pyproject.toml` is explicit about this — do not "fix" it.
- Telemetry entry factory methods in `archon_search/telemetry/entry.py` must not accept a `query` parameter. Raw query strings never enter the telemetry path.
- Never hardcode versions. The wheel version is derived from git tags via `hatch-vcs`.
- Never bake `--no-cov` into `addopts`. It is a local-only CLI override for iterating.

## Tests

The default suite is fast, hermetic, and runs in parallel via `pytest-xdist`:

```bash
uv run pytest
```

For debugging, use serial mode:

```bash
uv run pytest -n0          # serial execution
uv run pytest -n0 -x       # stop on first failure (xdist workers don't support -x directly)
uv run pytest -n0 -s       # show stdout (suppressed by xdist)
```

`live`, `eval`, `benchmark`, and `integration` markers run in the default suite and skip gracefully when their infrastructure is absent. They can also be run explicitly:

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
uv run pytest -m integration
uv run pytest -m live
uv run pytest -m benchmark   # needs a running server; auto-skips if unreachable
```

`live_benchmark` and `smoke` are the only markers excluded from the default run:

```bash
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov
uv run pytest tests/smoke/ --no-cov
```

The test pyramid, marker semantics, parallelism configuration, and the role of the eval harness are documented in [`Documentation/Architecture/200_testing_strategy.md`](Documentation/Architecture/200_testing_strategy.md). The eval fixture and threshold maintenance guide is [`tests/eval/README.md`](tests/eval/README.md).

## Pull requests

- Keep the PR scoped to one logical change; commits inside should map 1:1 to tasks.
- CI must be green: the default unit run, the eval gate, and any other workflow in [`.github/workflows/archon-search-pr.yml`](.github/workflows/archon-search-pr.yml).
- A PR that changes a REST or MCP surface must include the corresponding `BREAKING.md` entry.
- Include a short "why" in the description. The "what" is the diff.

## Releases

Releases are manual and maintainer-only. Cut a release with:

```bash
bash release.sh           # interactive
bash release.sh -y        # non-interactive
bash release.sh --dry-run
```

`release.sh` computes the next CalVer tag (`YY.M.<commit-count>`), prepends a changelog section to `CHANGELOG.md`, commits and pushes it, then pushes the tag. The tag push triggers [`.github/workflows/archon-search-release.yml`](.github/workflows/archon-search-release.yml) — which runs the eval gate, builds the wheel, publishes to PyPI via OIDC, and creates a GitHub Release with the changelog body. Plain pushes to `main` never publish. Versioning, the CalVer formula, and the OIDC publish flow are described in [`Documentation/Architecture/510_release_and_environment_strategy.md`](Documentation/Architecture/510_release_and_environment_strategy.md).

**Release prerequisites** (one-time setup, not a dev dependency):

```bash
brew install git-cliff          # macOS
cargo install git-cliff --version '>=2.4'  # cross-platform
```

`git-cliff >= 2.4` is required by `release.sh` to generate the changelog. `CHANGELOG.md` is managed exclusively by `release.sh` — do not edit it manually.

## Reporting issues

Open a GitHub issue at <https://github.com/user538295/archon-search/issues>. For sensitive reports (security, credential exposure), contact the maintainers directly via the repository owner's GitHub profile rather than filing a public issue.
