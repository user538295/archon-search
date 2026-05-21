**Purpose**: Explain how `archon-search` versions are produced and how to read them, so operators and maintainers can reason about what an installed version actually represents.
**Audience**: Operators upgrading installs; maintainers cutting or auditing releases.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Versioning and Release Model

`archon-search` uses CalVer (`YY.M.<commit-count>`). Versions tell you **when** a release was cut. They do **not** tell you whether the release breaks you — that information lives in [`/BREAKING.md`](../../BREAKING.md).

## Principles

1. **CalVer encodes time, not compatibility.** Read `BREAKING.md` for every upgrade.
2. **No hardcoded versions.** The wheel version is derived from the git tag via `hatch-vcs`; there is no `__version__` literal in the package.
3. **Tags drive releases.** Only a tag push (typically from [`release.sh`](../../release.sh)) publishes a wheel to PyPI. Plain pushes to `main` do not publish.

## How to read a version like `26.5.123`

```
26    .   5    .   123
YY        M        <commit-count>
```

- `YY` — two-digit UTC year at tag time (e.g. `26` for 2026).
- `M` — UTC month with no leading zero (e.g. `5`, not `05`).
- `<commit-count>` — `git rev-list --count HEAD` at the moment `release.sh` ran. It increases monotonically across the lifetime of the repository; it does **not** reset.

Consequences:

- Two versions with the same `YY.M` differ only by commit count, not by month boundary.
- A higher commit count is always newer than a lower one in the same calendar position.
- The number gap between two releases is the total commits merged between them — it is not a "minor" or "patch" hint.

Between tags, `hatch-vcs` produces `<tag>.post{N}` strings (`no-guess-dev`), where `N` is the commit distance from the most recent tag. These post-versions appear only on local dev builds; they are never published to PyPI.

## Where the version comes from

- The canonical source is the **git tag** created by [`release.sh`](../../release.sh).
- At build time, [`hatch-vcs`](https://github.com/ofek/hatch-vcs) reads the tag and stamps the wheel filename and metadata.
- At runtime, the package reads its own version via `importlib.metadata.version("archon-search")`. See `archon_search/cli/main.py` and `archon_search/server/app.py`.
- If installation metadata is missing (e.g. running from a source checkout without `pip install -e .`), the CLI falls back to `"0.0.0+source"` and the server falls back to `"dev"`. Neither sentinel ever appears on a PyPI release.

Build-time options (from `pyproject.toml`):

- `version_scheme = "no-guess-dev"` — deterministic post-versions between tags.
- `local_scheme = "no-local-version"` — strips `+gXXXXX` suffixes so PyPI accepts the upload.
- `fallback_version = "0.0.0+local"` — the **build-time** sentinel used by `hatch-vcs` when there is no `.git` directory (e.g. building from a tarball). This is distinct from the **runtime** `importlib.metadata` fallbacks (`"0.0.0+source"` for the CLI, `"dev"` for the server) described above, which fire when the installed-package metadata cannot be located.

See [`Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md) "Versioning" for the same rules from the release-engineering angle.

## Finding the version of an installed copy

The CLI exposes `--version` (wired through Click's `version_option` in `archon_search/cli/main.py`):

```bash
archon-search --version
# archon-search, version 26.5.123
```

Equivalent checks:

```bash
# Python metadata view
python -c "from importlib.metadata import version; print(version('archon-search'))"

# Pip view
pip show archon-search | grep ^Version
```

The HTTP `GET /health` response also exposes the running server's version; see [`Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md). Use this when you want to confirm the version of a **running** process rather than the installed wheel.

## How to read `BREAKING.md`

[`/BREAKING.md`](../../BREAKING.md) is the compatibility contract. Its structure is fixed:

1. A **Compatibility Policy** preamble restating that CalVer encodes time only.
2. A **Changelog** organised by release. The policy is one section per tagged release, plus a `[next release]` section for changes already on `main` but not yet tagged. As of the current `BREAKING.md`, only `[next release]` entries exist — no per-tag sections have been cut yet, so the per-tag layout is forward-looking and will materialise from the first tagged breaking change onward. #Unverified
3. Each entry has four fields: **Surface**, **Change**, **Migration**, **Announced in**.

When upgrading from version `A` to version `B`:

1. Open `BREAKING.md`.
2. Read every entry between `A` (exclusive) and `B` (inclusive). If `B` is the next un-tagged build of `main`, also read the `[next release]` entries — they describe behavior **already present** in `main`.
3. For each entry, apply the **Migration** step to the relevant client (REST, MCP, or config).

There is no deprecation window inside the codebase: when a contract changes, the old form is removed in the same release that documents the change. The only "warning period" is the time an entry spends under `[next release]` before its tag is cut. See [`Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) for the design rules behind this discipline.

## Related documents

- [`02_upgrade_procedure.md`](./02_upgrade_procedure.md) — the generic upgrade flow.
- [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) — chronological index of breaking changes.
- [`Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md) — release engineering and CI.
- [`/BREAKING.md`](../../BREAKING.md) — compatibility contract.
