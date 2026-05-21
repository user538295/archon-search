# Review: Architecture/510_release_and_environment_strategy.md

Reviewed against: `release.sh`, `.github/workflows/archon-search-release.yml`,
`.github/workflows/archon-search-pr.yml`, `pyproject.toml`,
`archon_search/server/app.py`, `archon_search/cli/main.py`, `README.md`.

## Summary

The document is largely accurate. All structural claims (tag-driven release,
CalVer formula, hatch-vcs configuration, OIDC publish, wheel-version
verification, env-var overrides, state directory layout) match the
authoritative sources. Findings below are mostly minor wording issues and a
couple of small but verifiable inaccuracies.

## Inaccuracies (numbered)

1. **Line 77, PR table row, "coverage report ... on the combined data".**
   The PR workflow file (`archon-search-pr.yml` lines 41-46) explicitly
   documents that there is *no* `coverage combine` step — `--cov-append`
   writes to a single `.coverage` file. Calling it "combined data" is
   misleading wording; the data is appended, not combined. Minor.

2. **Line 108, "Re-running a previously published tag" listed under "What
   does NOT trigger a release".** This is ambiguous/incorrect as written.
   The release workflow's `on:` block includes `workflow_dispatch:`
   (release.yml line 16), so manually re-dispatching against a tag ref
   *does* re-trigger the workflow (it will then fail at the PyPI upload
   step because the version already exists, but the workflow itself
   runs). What actually does not trigger a release is *re-pushing* a tag
   that already exists on origin — git rejects it.

3. **Line 75, "It is triggered by `push: tags: "*"` (and `workflow_dispatch:`
   against a tag ref)".** The `workflow_dispatch:` declaration in the
   workflow has no `inputs:` block and no requirement that it be invoked
   against a tag ref; the "Resolve tag name" step *expects* a tag context
   and errors out if none is resolvable (release.yml lines 80-88), but the
   trigger itself is unrestricted. Minor wording.

4. **Line 119, "Neither workflow installs anything not declared in
   `pyproject.toml`".** The release workflow installs `hatch` as a uv tool
   (`uv tool install hatch`, release.yml line 101). `hatch` is *not* a
   declared project dependency in `pyproject.toml` (only `hatchling` and
   `hatch-vcs` are in `[build-system].requires`). Strictly speaking the
   claim is false; the spirit (no surprise third-party deps) is preserved
   but `hatch` itself is installed out-of-band.

## Verified claims

- CalVer formula `YY.M.<commit-count>` with two-digit UTC year, UTC month
  with no leading zero, `git rev-list --count HEAD`. Matches `release.sh`
  lines 69-72 (`date -u +%y`, `date -u +%-m`, `git rev-list --count HEAD`).
- `release.sh` pre-flight: clean tree, on `main`, `HEAD == origin/main`.
  Matches release.sh lines 53-66.
- Tag uniqueness check (local + origin). Matches release.sh lines 75-80.
- `-y` / `--yes` non-interactive, `--dry-run`. Matches release.sh lines 28-42,
  94-98.
- `release.sh` ends with `git tag` + `git push origin <tag>`. Matches
  release.sh lines 113-114.
- Hatch config block: `source = "vcs"`, `no-guess-dev`, `no-local-version`,
  `fallback_version = "0.0.0+local"`. Matches `pyproject.toml` lines 35-43.
- Fallback sentinels: `"dev"` in `archon_search/server/app.py` (line 31) and
  `"0.0.0+source"` in `archon_search/cli/main.py` (line 17).
- Release workflow trigger: `push: tags: "*"` + `workflow_dispatch:`.
  Matches release.yml lines 12-16.
- Two jobs (`test`, `publish`); publish has `needs: test`. Matches
  release.yml lines 26, 62.
- Test job runs default suite (markers excluded) with `--cov-append`, eval
  slice with `--thresholds-path tests/eval/thresholds.toml`, then
  `coverage report --fail-under=85`. Matches release.yml lines 48-55.
- Publish job: full-history checkout, resolves tag from `GITHUB_REF` and
  falls back to `git describe --tags --exact-match` for workflow_dispatch,
  installs hatch, builds with `hatch build --clean`, compares wheel
  filename version to tag, refuses to publish on mismatch, then uses
  `pypa/gh-action-pypi-publish@release/v1` with `id-token: write` (OIDC).
  Matches release.yml lines 67-123.
- Both workflows pin Python 3.12 (`actions/setup-python@v5` with
  `python-version: "3.12"`) and use `astral-sh/setup-uv@v3`. Matches
  release.yml lines 34-40 and pr.yml lines 21-27.
- PR workflow triggers on `pull_request:`, runs `uv sync --dev`, default
  suite + eval slice with `--cov-append`, then
  `coverage report --fail-under=85`. Matches pr.yml lines 8-46.
- State layout (`~/.archon-search/archon-search.toml`, `.search.env` mode
  600, LanceDB tables under configured `db_path`, `search-logs/` for
  telemetry). Matches project CLAUDE.md description and config layer.
- Env overrides `ARCHON_SEARCH_API_KEY` and `ARCHON_SEARCH_KEY_FILE`.
  Matches project CLAUDE.md description of `key_manager.py`.
- `[telemetry].export_enabled = true` is coerced to `false` with a warning.
  Matches project CLAUDE.md telemetry section.

## Unverifiable / ambiguous

- Line 14 "tag push is what runs the publish workflow" — true for `push`
  trigger; obscured by the additional `workflow_dispatch` path (see
  inaccuracy #2 / #3).
- Line 17 "PyPI's trusted publisher" — the workflow uses the standard
  OIDC publish action with `id-token: write` permission and no `password:`
  parameter, which is the trusted-publisher pattern. The actual trusted
  publisher configuration lives on PyPI's side and cannot be verified
  from this repo. Consistent with the configuration, but not directly
  verifiable here.
- Line 51 "Do not introduce a `__version__` constant" — a normative
  instruction, not a factual claim; consistent with the two existing
  call sites using `importlib.metadata.version`.
- `BREAKING.md` quote on line 155 — not re-verified against
  `BREAKING.md` itself in this pass; quoted text is presented as direct
  citation.
