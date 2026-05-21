# Review: MigrationGuide/01_versioning_and_release_model.md

## Summary

The doc is largely accurate against `pyproject.toml`, `release.sh`, `.github/workflows/archon-search-release.yml`, `BREAKING.md`, and `README.md`. Two minor discrepancies and a few ambiguities. No load-bearing inaccuracies.

## Inaccuracies (numbered)

1. **`fallback_version` value mismatch.** The doc states: `fallback_version = "0.0.0+local"` "only used when there is no `.git` directory." `pyproject.toml` line 43 confirms `fallback_version = "0.0.0+local"` — this matches. However, the doc earlier (line 41) says the CLI sentinel is `"0.0.0+source"` and the server sentinel is `"dev"`. Verified in `archon_search/cli/main.py:17` (`"0.0.0+source"`) and `archon_search/server/app.py:31` (`"dev"`). These are the *runtime* fallbacks when `PackageNotFoundError` is raised — distinct from the *build-time* `fallback_version`. The doc correctly distinguishes them, but a careless reader could conflate them; not an inaccuracy, just fragile wording.

2. **`BREAKING.md` structural claim about "one section per tagged release."** The doc (line 77) asserts the Changelog has "one section per tagged release, plus a `[next release]` section." Current `BREAKING.md` contains **only** `[next release]` entries — no per-tag sections exist yet. This is a forward-looking policy description, not a description of the present file. Accurate as policy, but readers expecting per-tag sections in `BREAKING.md` today will not find any.

## Verified claims

- CalVer scheme `YY.M.<commit-count>` — confirmed in `release.sh:69-72` (`yy=date -u +%y`, `m=date -u +%-m`, `count=git rev-list --count HEAD`).
- "No hardcoded versions … no `__version__` literal" — confirmed; `pyproject.toml` uses `dynamic = ["version"]` with `[tool.hatch.version] source = "vcs"`. CLI and server both resolve via `importlib.metadata.version("archon-search")`.
- "Tags drive releases. Only a tag push … publishes" — confirmed in `archon-search-release.yml:12-16` (`on: push: tags: - "*"`) and `release.sh` header comments.
- `YY` UTC two-digit year, `M` no leading zero — confirmed (`date -u +%y`, `date -u +%-m` in `release.sh:70-71`).
- `<commit-count>` from `git rev-list --count HEAD`, monotonic, does not reset — confirmed (`release.sh:69`).
- `version_scheme = "no-guess-dev"`, `local_scheme = "no-local-version"`, `fallback_version = "0.0.0+local"` — confirmed in `pyproject.toml:41-43`.
- CLI `--version` via Click `version_option` — confirmed in `archon_search/cli/main.py:21`.
- CLI fallback `"0.0.0+source"` — confirmed (`cli/main.py:17`).
- Server fallback `"dev"` — confirmed (`server/app.py:31`).
- `hatch-vcs` reads the tag and stamps the wheel — confirmed in release workflow (`archon-search-release.yml:108-120`); workflow even refuses to publish if wheel version drifts from tag.
- `GET /health` exposes running version — confirmed in `routes_health.py:13,20` (`HealthResponse(status="running", version=_VERSION)`).
- `BREAKING.md` four fields (Surface, Change, Migration, Announced in) — confirmed (`BREAKING.md:14-16`, `21-23`).
- Compatibility Policy preamble exists — confirmed (`BREAKING.md:3-7`).
- "Plain pushes to `main` do not publish" — confirmed (workflow triggers only on tag push or `workflow_dispatch`).
- `release.sh` invocation flags `-y`/`--yes`, `--dry-run` — confirmed (`release.sh:28-42`).

## Unverifiable / ambiguous

- **"Between tags, `hatch-vcs` produces `<tag>.post{N}` strings."** This describes `no-guess-dev` behavior. The doc-comment in `pyproject.toml:37-40` states "between tags it is `26.5.0.post{N}` where N is the commit distance," which corroborates the doc — but neither the workflow nor `release.sh` exercises this path, and there is no test asserting the post-version format. Trusted via `pyproject.toml` comment + hatch-vcs convention.
- **"No deprecation window inside the codebase: when a contract changes, the old form is removed in the same release."** Policy statement; not directly verifiable from `BREAKING.md` because no tagged release entries exist yet. The two `[next release]` entries both say "no prior deprecation period," which is consistent with the policy.
- **Link target `Architecture/600_api_reference_or_public_interface.md`** — file exists in `Documentation/Architecture/` (per directory listing in companion reviews), but its accuracy regarding `/health` is outside this review's scope.
- **`importlib.metadata.version("archon-search")` returning a `.post{N}` string for a non-tag checkout** — depends on hatch-vcs build, not asserted by tests. Not verifiable here but consistent with `pyproject.toml` configuration.
