# Feature Brief: GitHub Releases with Auto-Generated Changelog

## Problem
When a new version of archon-search is published to PyPI, there is no corresponding GitHub Release — operators and contributors have no way to discover what changed without reading raw git log. The release process ends silently with a tag push.

## Goal
Every `bash release.sh` run produces: (1) a CHANGELOG.md commit in the repo on `main`, (2) a GitHub Release on the tag with the changelog section as the release body. Operators see structured release notes on the GitHub Releases page; the changelog is also available offline in the repo.

## Users & Context
- **Operators** evaluating whether to upgrade archon-search — they visit the GitHub Releases page to read "what's new."
- **Contributors** reviewing project history — they read CHANGELOG.md in the repo.
- **The developer cutting the release** — they run `release.sh` and expect the notes to appear automatically with no extra manual step.

## Core Flow

### release.sh (developer machine)
1. Developer runs `bash release.sh` (or `bash release.sh -y`).
2. Pre-flight checks: `git-cliff` in PATH with version `>= 2.4`, working tree clean, on `main`, in sync with `origin/main`.
3. `release.sh` computes the provisional CalVer tag: `YY.M.<git-rev-list-count + 1>`. The `+1` accounts for the CHANGELOG.md commit added in step 5.
4. `release.sh` runs `git-cliff --unreleased --tag "$TAG"` once and captures the output to `$NOTES`. This is the only git-cliff invocation in the entire flow.
5. `release.sh` prepends `$NOTES` to `CHANGELOG.md` using a temp-file pattern to avoid the shell truncation footgun: `tmp=$(mktemp); { printf '%s\n\n' "$NOTES"; cat CHANGELOG.md; } > "$tmp" && mv "$tmp" CHANGELOG.md`.
6. `release.sh` commits `CHANGELOG.md` with message `chore(release): update CHANGELOG.md for $TAG`.
7. `release.sh` pushes the commit to `main`.
8. `release.sh` verifies `git rev-list --count HEAD` equals the expected count (step 3 value), then creates and pushes the tag `$TAG`.
9. CI is triggered by the tag push.

### CI workflow (GitHub Actions — new `github-release` job)
10. After the `publish` job succeeds (eval gate passed, PyPI published), a new `github-release` job runs.
11. The job is gated with `if: startsWith(github.ref, 'refs/tags/')` so it is skipped on `workflow_dispatch` from a branch ref.
12. The job derives the tag name: `TAG="${GITHUB_REF#refs/tags/}"` (safe because the gate ensures `GITHUB_REF` is a tag ref).
13. The job checks out the tagged commit (`ref: ${{ github.ref }}`, `fetch-depth: 1` is sufficient).
14. The job extracts the body of the first version section from CHANGELOG.md using `awk`, **skipping the `## [version]` heading line** (redundant in a GitHub Release body where the tag name is already the title):
    ```
    awk '/^## /{if(found) exit; found=1; next} found' CHANGELOG.md
    ```
15. The job constructs the JSON payload using `jq` (pre-installed on `ubuntu-latest`) to safely handle newlines, quotes, and backslashes in the notes:
    ```bash
    jq -n --arg tag "$TAG" --arg body "$NOTES" \
       '{tag_name: $tag, name: $tag, body: $body}'
    ```
16. The job calls the GitHub Releases API via `curl`, with `GITHUB_TOKEN` explicitly mapped via `env: GITHUB_TOKEN: ${{ github.token }}`:
    ```
    POST https://api.github.com/repos/$GITHUB_REPOSITORY/releases
    Authorization: Bearer $GITHUB_TOKEN
    Accept: application/vnd.github+json
    X-GitHub-Api-Version: 2022-11-28
    Body: <jq output from step 15>
    ```
17. If the API returns HTTP 422 with `"code":"already_exists"`, the job logs a message and exits 0 (idempotent re-run behavior).
18. GitHub Release page shows the formatted changelog body; CHANGELOG.md in the repo contains the same content plus the heading — because CI reads directly from the committed file.

## In Scope
- Add `cliff.toml` to the repo root, configured for:
  - `tag_pattern = "\\d+\\.\\d+\\.\\d+"` so git-cliff recognizes CalVer tags (no `v` prefix).
  - Version heading format: `## [{{ version }}] - {{ timestamp }}` (double `##`) so the `awk '/^## /'` extraction pattern in CI works correctly.
  - Conventional commit grouping: `feat` → "Features", `fix` → "Bug Fixes", `docs` → "Documentation", `refactor` → "Refactoring", `test` / `chore` → omitted from output.
  - Scope stripping: task-ID scopes (`C0-3.x`, `B6`, etc.) omitted from rendered output — only the commit subject is shown.
  - `{ message = "^chore\\(release\\)", skip = true }` commit filter so the CHANGELOG.md commit itself never appears in changelog output.
  - A stable `# Changelog` header line preserved by using shell-level prepend rather than git-cliff's `--prepend` flag.
- `release.sh` extended with: git-cliff pre-flight (version check), provisional tag computation, single `git-cliff` invocation, CHANGELOG.md shell-prepend, commit, push, tag verification, push tag.
- `CHANGELOG.md` added to the repo root in this feature's PR, with an initial header and a brief note that prior release history is in git log. First `release.sh` run then prepends the first real entry.
- New `github-release` job in `archon-search-release.yml`:
  - `permissions: contents: write` at the **job level only** (not workflow level, to preserve least-privilege for the `publish` job's OIDC token).
  - `needs: [publish]` so it only runs after eval gate and PyPI publish succeed.
  - `if: startsWith(github.ref, 'refs/tags/')` condition so the job is skipped entirely on `workflow_dispatch` triggered from a branch ref (where no tag exists and no GitHub Release should be created).
  - Checkout step: `actions/checkout@v4` with `ref: ${{ github.ref }}`.
  - TAG derivation: `TAG="${GITHUB_REF#refs/tags/}"` (safe because the `if` condition guarantees `GITHUB_REF` starts with `refs/tags/`).
  - `awk`-based extraction from CHANGELOG.md — no new external tool installation.
  - `jq` for safe JSON construction (pre-installed on `ubuntu-latest`).
  - `curl` call with `env: GITHUB_TOKEN: ${{ github.token }}`.
  - 422-conflict handling: log and exit 0.
- `--dry-run` flag in `release.sh`: print provisional tag, print `$NOTES`, print the `curl` command that CI would run — no writes, no pushes, no API calls.
- Update `CLAUDE.md` and `contributing.md` to document `git-cliff >= 2.4` as a release-time prerequisite (install via `brew install git-cliff` or `cargo install git-cliff --version 2.4`).

## Out of Scope
- `gh` CLI — not used. `curl` + `GITHUB_TOKEN` covers the GitHub API call in CI.
- git-cliff running in CI — CI extracts from the committed CHANGELOG.md with `awk`.
- Hand-written release notes / RELEASE.md.
- Draft → publish promotion pattern — adds complexity without benefit.
- Changelog sections for pre-existing tags — not worth backfilling.
- BREAKING.md integration — BREAKING.md tracks API contract changes for operators; CHANGELOG.md tracks release summaries for users. They serve different audiences and remain separate.

## Key Decisions
- **Provisional tag = count + 1**: CalVer is `YY.M.<git-rev-list-count-HEAD>`. The CHANGELOG.md commit advances the count by exactly 1 (no pre-commit hooks are active in this repo). Step 8 verifies the count before tagging as a safety check. If the count does not match (e.g., a race commit or hook), the script bails before creating the tag.
- **Single git-cliff invocation, output written to CHANGELOG.md, CI reads from file**: git-cliff runs once in release.sh. CI does not re-run git-cliff — it extracts from the committed CHANGELOG.md. This guarantees the GitHub Release body and the CHANGELOG.md entry are byte-identical. No git-cliff in CI; no divergence.
- **Shell-level prepend via temp file, not git-cliff `--prepend`**: `git-cliff --prepend` strips the CHANGELOG.md header on every run. The temp-file pattern — `tmp=$(mktemp); { printf '%s\n\n' "$NOTES"; cat CHANGELOG.md; } > "$tmp" && mv "$tmp" CHANGELOG.md` — preserves the header and avoids the shell truncation footgun (writing to the same file you are reading from).
- **GitHub Release created in CI, not release.sh**: The GitHub Release appears only after the eval gate passes and PyPI publish succeeds. A GitHub Release for a broken build should never exist. `$GITHUB_TOKEN` is automatic in CI; no developer-machine auth needed.
- **`contents: write` at job level, not workflow level**: The `publish` job's OIDC token (`id-token: write`) must not be paired with `contents: write`. Each job declares only its required permissions. The `github-release` job gets `contents: write` only.
- **CHANGELOG.md committed before the tag**: The tag always points to a commit that already includes its own release notes.
- **`chore(release)` commits skipped in cliff.toml**: The CHANGELOG.md maintenance commit is filtered out so it never appears in any release's changelog output — not the current release (it's added after `$NOTES` is captured) nor the next one.

## Edge Cases & Constraints
- **git-cliff not installed or wrong version**: `release.sh` must `command -v git-cliff` and parse `git-cliff --version` to confirm `>= 2.4` before any work begins.
- **No conventional commits since last tag**: `git-cliff --unreleased` produces empty or near-empty output. `release.sh` warns and requires explicit confirmation (or `-y`) before proceeding.
- **Provisional count wrong (race or hook)**: Step 8 verifies `git rev-list --count HEAD` equals the expected value. On mismatch, the script exits with a message: "Unexpected commit count — re-run release.sh from a clean state."
- **Partial failure — commit pushed, tag not pushed**: CHANGELOG.md is on `main` with no tag. On retry, the count is now +1, producing a different provisional tag. Recovery: `git revert HEAD --no-edit && git push origin main`, then re-run. (Force-push is cleaner but may be blocked by branch protection; the revert approach always works.)
- **Partial failure — push race (another commit lands between steps 7 and 8)**: `git push origin main` in step 7 succeeds, but another commit may land before step 8's tag push, making the local HEAD stale. Recovery: `git reset HEAD~1` (remove local changelog commit), `git pull`, re-run `release.sh`. The reverted state is clean for a fresh attempt.
- **Partial failure — tag pushed, CI `github-release` job fails**: PyPI published, GitHub Release not created. Recovery: re-run the `github-release` job from the Actions UI. The job is idempotent — HTTP 422 on an existing release causes exit 0.
- **CalVer collision**: Existing guard in `release.sh` handles this; no change needed.
- **Direct push to `main`**: Steps 7 pushes a commit directly to `main`. This requires that `main` allows direct pushes (no PR-required branch protection). This repo currently has no such protection. If branch protection is ever added, this flow breaks; the brief notes it as a known constraint.
- **CHANGELOG.md manual edits by contributors**: Contributors must not manually edit CHANGELOG.md — it is managed exclusively by `release.sh`. This must be documented in CONTRIBUTING.md.

## Open Questions
None — all design decisions are resolved.

## Future Iterations
- If branch protection requiring PRs is ever enabled, move the CHANGELOG.md commit to a bot PR created by CI after the tag push.
- Backfill changelog entries for prior tags if a full history is ever needed.
- Promote the GitHub Release to "latest" only after a manual confirmation step, for more control over the release visibility.
- Add git-cliff as a pre-release lint step to validate commit message quality before tagging.

## Recommendation
This is the right feature to build now. The hardest part is the provisional-tag arithmetic and the count-verification step in `release.sh` — build and test the dry-run path first. The CI `github-release` job is intentionally simple (awk + curl) to avoid new dependencies. Do not cut corners on the pre-flight checks or the 422 idempotency handling — these are the failure modes most likely to be hit in practice.
