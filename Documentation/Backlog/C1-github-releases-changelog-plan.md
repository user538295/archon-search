# C1 — GitHub Releases with Auto-Generated Changelog

**Purpose**: Every `bash release.sh` run produces a CHANGELOG.md commit and, after PyPI publishes, a GitHub Release with structured release notes — giving operators and contributors a discoverable, formatted history without any manual step.
**Audience**: archon-search contributors implementing C1; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

When a new version of archon-search is published to PyPI there is no corresponding GitHub Release. Operators evaluating whether to upgrade have no structured changelog to read; contributors reviewing project history must trawl raw `git log`. The release process ends silently with a tag push.

The brief at `Documentation/Backlog/github-releases-changelog-brief.md` resolved all key design decisions: provisional-tag arithmetic, single git-cliff invocation, shell-level prepend, CI-side extraction from CHANGELOG.md, and least-privilege permissions per CI job.

---

## Goal

After C1 ships: running `bash release.sh` (1) invokes `git-cliff --unreleased` once to capture `$NOTES`, (2) prepends `$NOTES` to `CHANGELOG.md` and commits it, (3) verifies the commit count matches the provisional tag, then (4) pushes the tag. After the eval gate passes and PyPI publishes, a new `github-release` CI job extracts the first changelog section from CHANGELOG.md via `awk` and creates a GitHub Release via the GitHub API. `--dry-run` prints the provisional tag, `$NOTES`, and the `curl` command CI would run — with no writes, no pushes, and no API calls.

---

## Scope

### In Scope
- `cliff.toml` at the repo root: CalVer tag pattern, `## [version] - timestamp` heading format, conventional-commit grouping, scope stripping, `chore(release)` skip filter.
- `CHANGELOG.md` at the repo root: initial stub with `# Changelog` header and a note that prior history lives in git log.
- `release.sh` extended with: git-cliff pre-flight (version ≥ 2.4), provisional tag computation (`count + 1`), single `git-cliff --unreleased --tag "$TAG"` invocation, shell-prepend via temp file, commit, push, count verification, tag push, and updated `--dry-run` output.
- New `github-release` job in `.github/workflows/archon-search-release.yml`: `needs: [publish]`, `if: startsWith(github.ref, 'refs/tags/')`, `permissions: contents: write` at job level, checkout at tag ref, `awk` extraction, `jq` JSON construction, `curl` API call, 422-conflict idempotency.
- `CLAUDE.md` and `contributing.md` updated with `git-cliff >= 2.4` as a release-time prerequisite.

### Out of Scope
- `gh` CLI — `curl` + `GITHUB_TOKEN` is used for the API call in CI.
- git-cliff running in CI — CI reads from the committed CHANGELOG.md.
- Hand-written release notes / RELEASE.md.
- Draft → publish promotion pattern.
- Changelog backfill for prior tags.
- BREAKING.md integration — the two files serve different audiences and remain separate.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- REST/MCP API contract — no endpoints added, no schemas changed.
- `BREAKING.md` — no breaking changes.
- `pyproject.toml` / `uv.lock` — no new Python dependencies.
- Existing `release.sh` pre-flight checks (clean tree, `main` branch, in sync with origin) — extended, not replaced.
- `archon-search-release.yml` `test` and `publish` jobs — unchanged.
- `--cov-fail-under=85` threshold.

---

## Known limitations / accepted trade-offs
- CHANGELOG.md must not be manually edited by contributors. `release.sh` is the sole writer. Documented in CONTRIBUTING.md.
- Direct push to `main` is required by step 3 (commit + push CHANGELOG.md before tagging). If branch protection requiring PRs is ever added, this flow breaks. Known constraint, documented in the brief.
- The provisional-tag count-verification in step 8 assumes no pre-commit hooks create additional commits. This repo currently has none.
- Partial-failure recovery is documented in `release.sh` output but not automated — operator manual intervention is required.
- First release (no prior tags): `git-cliff --unreleased` with no prior tags will include all commits in history. This is acceptable behavior for the initial release and produces a complete changelog. Subsequent releases will only include commits since the last tag.
- `EXPECTED_COUNT_OVERRIDE` / `RELEASE_SH_TEST_MODE`: these env vars exist solely for testing and are guarded by a production bail. Operators must never export them in shell profiles or CI environments.

---

## Architecture

### New files
- `cliff.toml` — git-cliff configuration. No Python import; consumed only by `release.sh` via `git-cliff --unreleased --tag "$TAG"`.
- `CHANGELOG.md` — append-prepended by `release.sh` on each release. Initial stub only.
- `tests/test_release_sh.py` — Python subprocess-based tests for `release.sh` behavior in a temporary git repo.
- `tests/test_changelog_awk.py` — Unit tests for the `awk` extraction one-liner used by the CI job.

### Modified files
- `release.sh` — all changes are additive shell code around the existing tag-push logic. Three new logical sections: (a) git-cliff pre-flight, (b) provisional tag + cliff invocation + CHANGELOG.md prepend + commit + push + count-verify, (c) updated `--dry-run` output.
- `.github/workflows/archon-search-release.yml` — new `github-release` job appended after `publish`.
- `CLAUDE.md` — `git-cliff >= 2.4` added to release prerequisites.
- `contributing.md` — `git-cliff >= 2.4` install instructions added under release workflow section.
- `.gitattributes` — add `CHANGELOG.md text eol=lf` to prevent CRLF issues on Windows checkouts.

### Data flow
```
release.sh
  → git-cliff --unreleased --tag "$TAG"   # produces $NOTES
  → prepend $NOTES to CHANGELOG.md        # via mktemp
  → git commit + push                     # chore(release): update CHANGELOG.md for $TAG
  → verify rev-list count == expected
  → git tag + push                        # triggers CI

CI github-release job (after publish succeeds)
  → checkout at tag ref
  → awk '/^## /{...} found' CHANGELOG.md  # extracts first section body
  → jq -n --arg tag ... --arg body ...    # safe JSON construction
  → curl POST /repos/.../releases         # creates GitHub Release
  → 422 already_exists → exit 0
```

### cliff.toml configuration shape
```toml
[changelog]
header = ""
body = """
## [{{ version }}] - {{ timestamp | date(format="%Y-%m-%d") }}
{% for group, commits in commits | group_by(attribute="group") %}
### {{ group }}
{% for commit in commits %}- {{ commit.message }}
{% endfor %}
{% endfor %}
"""
trim = true

[git]
tag_pattern = "^\\d+\\.\\d+\\.\\d+$"
skip_tags = ""
sort_commits = "oldest"

commit_parsers = [
  { message = "^chore\\(release\\)", skip = true },
  { message = "^feat", group = "Features" },
  { message = "^fix", group = "Bug Fixes" },
  { message = "^docs", group = "Documentation" },
  { message = "^refactor", group = "Refactoring" },
  { message = "^test", skip = true },
  { message = "^chore", skip = true },
  { message = ".*", skip = true },
]
```

### release.sh new sections (pseudo-code)

```bash
# Pre-flight: git-cliff version check
check_git_cliff() {
  command -v git-cliff || bail "git-cliff not found in PATH — install with: brew install git-cliff"
  local ver; ver=$(git-cliff --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
  # compare major.minor >= 2.4
}

# Provisional tag: count + 1 (accounts for the CHANGELOG.md commit)
# EXPECTED_COUNT_OVERRIDE is a test-only backdoor; bail if set in production
[ -n "${EXPECTED_COUNT_OVERRIDE:-}" ] && [ -z "${RELEASE_SH_TEST_MODE:-}" ] && \
  bail "EXPECTED_COUNT_OVERRIDE is set — unset it before running a real release"
EXPECTED_COUNT="${EXPECTED_COUNT_OVERRIDE:-$(( $(git rev-list --count HEAD) + 1 ))}"
TAG="${yy}.${m}.${EXPECTED_COUNT}"

# Cliff invocation
NOTES=$(git-cliff --unreleased --tag "$TAG") || bail "git-cliff failed — check cliff.toml and git history"

# Empty notes check - always bail
if [ -z "$(echo "$NOTES" | tr -d '[:space:]')" ]; then
  bail "No conventional commits found since last tag. Nothing to release."
fi

# CHANGELOG.md prepend (insert after the # Changelog header)
[ -f CHANGELOG.md ] || bail 'CHANGELOG.md not found — run git-cliff setup first'
grep -q '^# Changelog$' CHANGELOG.md || bail 'CHANGELOG.md is missing the exact # Changelog header'
tmp=$(mktemp -p .)
trap 'rm -f "$tmp"' EXIT
NOTES="$NOTES" awk '
  /^# Changelog/ {
    print           # print "# Changelog"
    getline         # consume the blank line following the header
    print ""        # print one blank line before notes
    print ENVIRON["NOTES"]
    print ""        # print one blank line after notes
    next
  }
  { print }
' CHANGELOG.md > "$tmp" && mv "$tmp" CHANGELOG.md

# Commit + push
git add CHANGELOG.md
git commit -m "chore(release): update CHANGELOG.md for $TAG"
git push origin main

# Count verification
actual_count=$(git rev-list --count HEAD)
[ "$actual_count" -eq "$EXPECTED_COUNT" ] || bail "Unexpected commit count..."

# Tag + push (existing logic, unchanged)
git tag "$TAG"
git push origin "$TAG"
```

---

## Task breakdown

### Phase 1 — git-cliff configuration and initial CHANGELOG.md
> **Releasable**: after Task 1.2; the cliff config can be tested locally with `git-cliff --unreleased` before any release.sh changes.

#### Task 1.1 — cliff.toml
- [x] **File**: `cliff.toml`
- **Depends on**: nothing
- **Description**:
  - New file at repo root consumed exclusively by `release.sh`'s `git-cliff --unreleased --tag "$TAG"` invocation.
  - `[git].tag_pattern = "^\\\\d+\\\\.\\\\d+\\\\.\\\\d+$"` — anchored regex matching CalVer without `v` prefix.
  - `[changelog].body` template: heading line `## [{{ version }}] - {{ timestamp | date(format="%Y-%m-%d") }}` using double `##` so the `awk '/^## /'` CI extraction pattern fires on each version boundary.
  - Conventional commit groups: `feat` → "Features", `fix` → "Bug Fixes", `docs` → "Documentation", `refactor` → "Refactoring". `test` and `chore` commits are skipped from output.
  - `{ message = "^chore\\(release\\)", skip = true }` filter prevents CHANGELOG.md maintenance commits from appearing in any release output (current or next).
  - Scope stripping: when `conventional_commits = true` (git-cliff default), `commit.message` in the template context contains the `<description>` portion only — the text after `type(scope): `. A commit `feat(C0-3.x): add search endpoint` renders as `add search endpoint`. There is no `commit.description` field in git-cliff's template context; the full unparsed subject is available as `commit.raw_message` (added in 2.7.0). Note that in `commit_parsers`, the `message` field matches against the full subject line (e.g., `feat(C0-3.x): add search endpoint`), not the parsed description.
  - Catch-all `{ message = ".*", skip = true }` at the end of `commit_parsers` ensures non-conventional commits do not appear ungrouped in output.
  - `[changelog].header = ""` — no prefix header in git-cliff output; `# Changelog` is preserved by the shell-prepend pattern, not managed by git-cliff.
  - **Releasable**: after this task, `git-cliff --unreleased` produces correctly formatted output locally.
- **Tests (TDD)** — `tests/test_cliff_toml.py`:
  - Unit: `test_cliff_toml_is_valid_toml` — parse `cliff.toml` with `tomllib`; assert no parse error.
  - Unit: `test_required_keys_present` — assert `git.tag_pattern`, `changelog.body`, and `git.commit_parsers` keys are present.
  - Unit: `test_chore_release_skip_filter_present` — assert the `commit_parsers` list contains an entry with `message = "^chore\\(release\\)"` and `skip = true`.
  - Unit: `test_tag_pattern_matches_calver` — assert the regex in `tag_pattern` matches `"26.5.42"`; assert does NOT match `"v1.2.3"`, `"26.5"` (two segments), or `"1.2.3.4"` (four segments).
  - Unit: `test_body_template_heading_matches_awk_pattern` — extract the first line of the `[changelog].body` template from `cliff.toml`; assert it starts with `## [` (matching the awk `/^## /` pattern used in CI extraction).
  - Unit: `test_cliff_output_strips_scope` — create a temp git repo, make one commit with message `feat(myscope): add feature`, run `git-cliff --config cliff.toml --unreleased --tag "26.5.1"` in that repo; assert stdout contains `add feature` and does NOT contain `feat(myscope)`. This is the integration-level guard that the static TOML tests cannot provide: it verifies the template renders correctly against a real git-cliff invocation. (Skip with `pytest.importorskip` or `pytest.mark.skipif` if `git-cliff` is not installed.)
  - TDD note: run `uv run pytest tests/test_cliff_toml.py` before creating `cliff.toml` to confirm tests fail with `FileNotFoundError` (the expected red state). Tests become meaningfully green only after `cliff.toml` is created with correct content.
  - Checkpoint: `uv run pytest tests/test_cliff_toml.py -v`

#### Task 1.2 — Initial CHANGELOG.md stub
- [x] **File**: `CHANGELOG.md`
- **Depends on**: Task 1.1
- **Description**:
  - New file at repo root. Content:
    ```markdown
    # Changelog

    All notable changes to archon-search are recorded here.
    Prior release history is available via `git log`.
    ```
  - This exact structure ensures the insert-after-header prepend preserves the `# Changelog` header at the top of the file on every subsequent release. The CHANGELOG.md stub must have exactly one blank line after `# Changelog` (matching the stub template). The shell-prepend in Task 2.3 uses `getline` to consume that blank line before inserting notes, preventing double-blank accumulation.
  - Ensure `.gitattributes` contains `CHANGELOG.md text eol=lf` to prevent CRLF issues on Windows checkouts (the awk extraction assumes LF line endings).
  - **Releasable**: after this task, the file exists and can be used as the prepend target by release.sh.
- **Tests (TDD)** — `tests/test_changelog_awk.py`:
  - Unit: `test_awk_extraction_on_stub` — run `awk '/^## /{if(found) exit; found=1; next} found'` on the stub content (no `##` sections); assert empty output.
  - Unit: `test_awk_extraction_single_section` — run the awk command on a CHANGELOG.md with one `## [1.0.0] - 2026-01-01` section followed by body lines and a `## [0.9.0]` section; assert only the first section's body lines are returned, not the heading.
  - Unit: `test_awk_extraction_heading_not_included` — assert the `## [version]` heading line itself is absent from the awk output (the brief specifies it is skipped).
  - Unit: `test_awk_extraction_multiple_sections` — assert content after the second `## [...]` boundary is excluded.
  - Checkpoint: `uv run pytest tests/test_changelog_awk.py -v`

---

### Phase 2 — release.sh enhancements
> **Releasable**: after Task 2.4; `release.sh --dry-run` is the verification path. No live release is needed to validate correctness.

#### Task 2.1 — git-cliff pre-flight check
- [x] **File**: `release.sh`
- **Depends on**: Task 1.1
- **Description**:
  - Add `check_git_cliff()` function immediately after the existing pre-flight checks (working-tree-clean, branch, remote-sync block — currently ends at `git fetch --tags origin main`).
  - `command -v git-cliff >/dev/null 2>&1 || bail "git-cliff not found in PATH — install with: brew install git-cliff or cargo install git-cliff --version '>=2.4'"`.
  - Parse `git-cliff --version` output: extract the first `MAJOR.MINOR` pair via `grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1`, split on `.`, compare `MAJOR > 2 || (MAJOR == 2 && MINOR >= 4)`.
  - On version mismatch: `bail "git-cliff >= 2.4 required, found $VER"`.
  - Call `check_git_cliff` at the end of the pre-flight block.
  - **Releasable**: after this task, `release.sh` exits early with a clear message when git-cliff is absent or outdated.
- **Tests (TDD)** — `tests/test_release_sh.py`:
  - (Create file) Harness: `_run_release_sh(args, env_overrides=None, git_repo=None)` helper using `subprocess.run`. Set up: `git init` (worker), `git init --bare` (local remote), `git remote add origin <bare_path>`, initial commit and push to set up `origin/main` tracking. The bare repo serves as a local origin so `git fetch`, `git push`, and `git rev-parse origin/main` all work without network access.
  - Unit: `test_missing_git_cliff_exits_with_error` — override `PATH` to exclude git-cliff; assert exit code != 0 and stderr contains "git-cliff not found".
  - Unit: `test_old_git_cliff_version_exits_with_error` — stub `git-cliff` binary that prints "git-cliff 2.3.0"; assert exit code != 0 and stderr contains ">= 2.4".
  - Unit: `test_valid_git_cliff_version_passes_preflight` — stub that prints "git-cliff 2.4.0"; assert the script proceeds past the version check (may fail later for unrelated reasons — just confirm no version-check error).
  - Checkpoint: `uv run pytest tests/test_release_sh.py::test_missing_git_cliff_exits_with_error tests/test_release_sh.py::test_old_git_cliff_version_exits_with_error tests/test_release_sh.py::test_valid_git_cliff_version_passes_preflight -v`

#### Task 2.2 — Provisional tag computation (count + 1)
- [x] **File**: `release.sh`
- **Depends on**: Task 2.1
- **Description**:
  - Replace the current `count="$(git rev-list --count HEAD)"` + `TAG="${yy}.${m}.${count}"` block with the provisional-tag formula: `EXPECTED_COUNT="${EXPECTED_COUNT_OVERRIDE:-$(( $(git rev-list --count HEAD) + 1 ))}"` and `TAG="${yy}.${m}.${EXPECTED_COUNT}"`.
  - `EXPECTED_COUNT_OVERRIDE` is a test-only injection point. To prevent accidental use in production, the script bails if `EXPECTED_COUNT_OVERRIDE` is set without `RELEASE_SH_TEST_MODE=1`. Tests must set both: `EXPECTED_COUNT_OVERRIDE=9999 RELEASE_SH_TEST_MODE=1 bash release.sh ...`.
  - The `+1` accounts for the CHANGELOG.md commit added later in the script. Existing tag-collision checks remain unchanged — they already use `$TAG`.
  - Add a count verification step after the CHANGELOG.md commit and push (implemented in Task 2.3): `actual="$(git rev-list --count HEAD)"; [ "$actual" -eq "$EXPECTED_COUNT" ] || bail "Unexpected commit count ($actual vs $EXPECTED_COUNT) — re-run release.sh from a clean state."`.
  - The count-verify step must occur before `git tag "$TAG"` and `git push origin "$TAG"` to ensure the tag points to the right commit.
  - **Releasable**: after this task, the provisional tag formula is correct; tests validate the arithmetic in an isolated git repo.
- **Tests (TDD)** — `tests/test_release_sh.py`:
  - Unit: `test_provisional_tag_is_count_plus_one` — in a temp repo with N commits, assert `--dry-run` output contains a tag matching `YY.M.<N+1>` format (not `YY.M.<N>`).
  - Unit: `test_count_mismatch_bails` — run release.sh with `EXPECTED_COUNT_OVERRIDE=9999 RELEASE_SH_TEST_MODE=1` in a temp repo with fewer than 9999 commits; assert script exits with 'Unexpected commit count'.
  - Checkpoint: `uv run pytest tests/test_release_sh.py::test_provisional_tag_is_count_plus_one tests/test_release_sh.py::test_count_mismatch_bails -v`

#### Task 2.3 — CHANGELOG.md shell-prepend, commit, and push
- [x] **File**: `release.sh`
- **Depends on**: Task 2.2, Task 1.2
- **Description**:
  - After the existing confirmation prompt (or `-y` bypass) but before `git tag`, insert the CHANGELOG.md pipeline:
    1. `NOTES=$(git-cliff --unreleased --tag "$TAG") || bail "git-cliff failed — check cliff.toml and git history"` — the single git-cliff invocation. Output captured to `$NOTES`. Stderr surfaces; nonzero exits are caught.
    2. If `$NOTES` is empty or contains only whitespace: `bail 'No conventional commits found since last tag. Nothing to release.'` — the release aborts. This keeps the tag arithmetic simple (always one CHANGELOG.md commit) and prevents empty entries in CHANGELOG.md.
    3. Insert-after-header prepend: use `awk` to insert `$NOTES` immediately after the `# Changelog` header line (and its following blank line). Use `tmp=$(mktemp -p .)` to ensure the temp file is on the same filesystem, and register `trap 'rm -f "$tmp"' EXIT` immediately after so orphaned temp files cannot break subsequent pre-flight checks. Add guards before the awk prepend: `[ -f CHANGELOG.md ] || bail 'CHANGELOG.md not found — run git-cliff setup first'` and `grep -q '^# Changelog$' CHANGELOG.md || bail 'CHANGELOG.md is missing the exact # Changelog header — cannot prepend. Ensure the file starts with: # Changelog'`. Use `ENVIRON["NOTES"]` instead of awk `-v notes=` to prevent backslash escape processing that would mangle paths like `C:\\new\\file` in commit messages. The awk uses `getline` to consume the blank line immediately following the `# Changelog` header before inserting notes, preventing blank line accumulation on repeated releases.
    4. `git add CHANGELOG.md && git commit -m "chore(release): update CHANGELOG.md for $TAG"`.
    5. `git push origin main`.
    6. Count verification (from Task 2.2): `actual="$(git rev-list --count HEAD)"; [ "$actual" -eq "$EXPECTED_COUNT" ] || bail "..."`.
  - The `--dry-run` path (Task 2.4) must still skip all writes. Implement via the existing `DRY_RUN` flag check before each write.
  - **Releasable**: after this task, a non-dry-run `release.sh` correctly prepends, commits, pushes, and verifies before tagging.
- **Tests (TDD)** — `tests/test_release_sh.py`:
  - Unit: `test_changelog_prepend_preserves_header` — in a temp repo with `CHANGELOG.md` stub, run the prepend; assert the file's first line is `# Changelog`.
  - Unit: `test_changelog_prepend_adds_notes_after_header` — assert `$NOTES` content appears AFTER the `# Changelog` line but BEFORE the preamble text (i.e., immediately after the `# Changelog` header).
  - Unit: `test_commit_message_format` — assert commit message matches `chore(release): update CHANGELOG.md for YY.M.N`.
  - Unit: `test_empty_notes_bails` — stub git-cliff to produce empty output; assert script exits nonzero with "No conventional commits found".
  - Unit: `test_git_cliff_execution_failure_bails` — stub `git-cliff` binary to exit with code 1; assert `release.sh` exits nonzero and stderr contains 'git-cliff failed'.
  - Unit: `test_missing_changelog_md_exits_with_error` — run the prepend step in a temp repo without CHANGELOG.md; assert script exits with a clear error message (not a raw shell `awk: can't open file CHANGELOG.md` error).
  - Unit: `test_malformed_changelog_header_exits_with_error` — run the prepend with a CHANGELOG.md whose first line is `# CHANGELOG` (not `# Changelog`); assert script exits with "missing the exact # Changelog header".
  - Checkpoint: `uv run pytest tests/test_release_sh.py -k "prepend or commit_message or empty_notes or cliff_execution or missing_changelog or malformed_changelog" -v`

#### Task 2.4 — Updated --dry-run output
- [ ] **File**: `release.sh`
- **Depends on**: Task 2.3
- **Description**:
  - The existing `--dry-run` exit block (currently after the confirmation message) must be moved to after `$NOTES` is captured (including the empty-notes bail check) but before any writes or pushes. Since the empty-notes path always bails, the dry-run output does not need to handle or mention an empty-notes prompt.
  - Dry-run output (printed to stdout, then `exit 0`):
    ```
    [dry-run] provisional tag : YY.M.N
    [dry-run] cliff notes     :
    <$NOTES>
    [dry-run] CI would call   :
      curl -s -w "\n%{http_code}" \
        -X POST https://api.github.com/repos/<REPO>/releases \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        -d '<jq -n --arg tag "$TAG" --arg body "<first-section>" ...>'
    [dry-run] no writes, no pushes, no API calls.
    ```
  - The `<REPO>` placeholder in the curl preview is derived from `git remote get-url origin` with a trailing `.git` strip.
  - The body in the curl preview is the awk-extracted first section of `$NOTES` (same extraction as CI would perform).
  - git-cliff IS invoked in dry-run (read-only operation) to produce the preview.
  - **Releasable**: after this task, `bash release.sh --dry-run` shows a full preview with no side effects.
- **Tests (TDD)** — `tests/test_release_sh.py`:
  - Unit: `test_dry_run_prints_provisional_tag` — assert stdout contains `[dry-run] provisional tag`.
  - Unit: `test_dry_run_prints_notes` — stub git-cliff to emit a known string; assert it appears in dry-run stdout.
  - Unit: `test_dry_run_prints_curl_command` — assert stdout contains `curl` and `api.github.com` and `releases`.
  - Unit: `test_dry_run_makes_no_git_changes` — assert no commits created, no tags created, CHANGELOG.md unchanged after `--dry-run`.
  - Unit: `test_dry_run_exits_zero` — assert exit code is 0.
  - Checkpoint: `uv run pytest tests/test_release_sh.py -k "dry_run" -v`

---

### Phase 3 — CI github-release job
> **Releasable**: after Task 3.1; the GitHub Release appears on the Releases page after the next tag push that passes the eval gate and publishes to PyPI.

#### Task 3.1 — github-release job in archon-search-release.yml
- [ ] **File**: `.github/workflows/archon-search-release.yml`
- **Depends on**: Task 1.2, Task 2.3
- **Description**:
  - Append a new `github-release` job after the `publish` job:
    ```yaml
    github-release:
      needs: [publish]
      runs-on: ubuntu-latest
      if: startsWith(github.ref, 'refs/tags/')
      permissions:
        contents: write
      steps:
        - name: Checkout (at tag)
          uses: actions/checkout@v4
          with:
            ref: ${{ github.ref }}
            fetch-depth: 1

        - name: Derive tag name
          id: tag
          run: echo "tag=${GITHUB_REF#refs/tags/}" >> "$GITHUB_OUTPUT"

        - name: Extract changelog section body
          id: notes
          run: |
            NOTES=$(awk '/^## /{if(found) exit; found=1; next} found' CHANGELOG.md)
            delimiter="NOTES_$(openssl rand -hex 8)"
            echo "notes<<${delimiter}" >> "$GITHUB_OUTPUT"
            echo "$NOTES" >> "$GITHUB_OUTPUT"
            echo "${delimiter}" >> "$GITHUB_OUTPUT"

        - name: Create GitHub Release
          env:
            GITHUB_TOKEN: ${{ github.token }}
            TAG: ${{ steps.tag.outputs.tag }}
            NOTES: ${{ steps.notes.outputs.notes }}
          run: |
            payload=$(jq -n --arg tag "$TAG" --arg body "$NOTES" \
              '{tag_name: $tag, name: $tag, body: $body}')
            response=$(curl -s -w "\n%{http_code}" \
              -X POST "https://api.github.com/repos/$GITHUB_REPOSITORY/releases" \
              -H "Authorization: Bearer $GITHUB_TOKEN" \
              -H "Accept: application/vnd.github+json" \
              -H "X-GitHub-Api-Version: 2022-11-28" \
              -d "$payload")
            http_code=$(echo "$response" | tail -1)
            body=$(echo "$response" | head -n -1)
            if [ "$http_code" = "422" ]; then
              code=$(echo "$body" | jq -r '.errors[0].code // empty')
              if [ "$code" = "already_exists" ]; then
                echo "Release already exists — idempotent exit."
                exit 0
              fi
            fi
            if [ "$http_code" != "201" ]; then
              echo "GitHub API error $http_code: $body" >&2
              exit 1
            fi
            echo "GitHub Release created: $(echo "$body" | jq -r '.html_url')"
    ```
  - `permissions: contents: write` is declared at **job level only** — the `publish` job's `id-token: write` is not widened.
  - The `if: startsWith(github.ref, 'refs/tags/')` condition ensures the job is skipped entirely on `workflow_dispatch` invoked from a branch ref.
  - **Releasable**: after this task, the GitHub Release creation is fully wired in CI.
- **Tests (TDD)** — `tests/test_changelog_awk.py` (existing) + `tests/test_release_ci.py` (new):
  - Unit: `test_yaml_is_valid` — parse `archon-search-release.yml` with `yaml.safe_load`; assert no parse error.
  - Unit: `test_github_release_job_present` — assert `jobs.github-release` key exists in the parsed YAML.
  - Unit: `test_github_release_needs_publish` — assert `needs` contains `"publish"`.
  - Unit: `test_github_release_permissions_contents_write` — assert `permissions.contents == "write"` at the job level and that there is no `permissions.id-token` at job level.
  - Unit: `test_github_release_if_condition` — assert the `if` field equals `"startsWith(github.ref, 'refs/tags/')"`.
  - Unit: `test_no_workflow_level_contents_write` — assert that `permissions` at the workflow top level does not include `contents: write` (preserving least-privilege for publish's OIDC token).
  - Unit: `test_awk_extracts_first_section_only` — re-uses `test_changelog_awk.py::test_awk_extraction_single_section` (already written in Task 1.2).
  - Checkpoint: `uv run pytest tests/test_release_ci.py tests/test_changelog_awk.py -v`

---

### Phase 4 — Documentation
> **Releasable**: after Task 4.1, all developer-facing docs reflect the new git-cliff prerequisite and CHANGELOG.md ownership rule.

#### Task 4.1 — CLAUDE.md and contributing.md updates
- [ ] **File**: `CLAUDE.md`, `contributing.md`
- **Depends on**: Task 2.1 (establishes the git-cliff prerequisite)
- **Description**:
  - `CLAUDE.md` — in the "Common commands" section, add a subsection or inline note under the release command block:
    ```
    # Release prerequisite (one-time setup)
    brew install git-cliff          # macOS
    cargo install git-cliff --version '>=2.4'  # cross-platform
    ```
  - `contributing.md` — in the release workflow section, add:
    - `git-cliff >= 2.4` listed as a release-time prerequisite (not a dev dependency; only needed when cutting a release).
    - Install instructions: `brew install git-cliff` (macOS) or `cargo install git-cliff --version '>=2.4'` (cross-platform).
    - A one-sentence rule: "CHANGELOG.md is managed exclusively by `release.sh` — do not edit it manually."
  - **Releasable**: after this task, all documentation reflects the new release process.
- **Tests (TDD)** — `tests/test_docs.py` (existing file — add new assertions):
  - Unit: `test_claude_md_mentions_git_cliff` — read `CLAUDE.md`; assert `"git-cliff"` appears in content.
  - Unit: `test_contributing_md_mentions_git_cliff` — read `contributing.md`; assert `"git-cliff"` and `">= 2.4"` appear.
  - Unit: `test_contributing_md_changelog_ownership_rule` — assert `"CHANGELOG.md"` and `"release.sh"` co-appear within 5 lines of each other in `contributing.md` (proximity check).
  - Checkpoint: `uv run pytest tests/test_docs.py -v`

#### Task 4.2 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, CHANGELOG) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - In particular verify:
    - `Documentation/Architecture/510_release_and_environment_strategy.md` — does it describe the release flow? If so, update it to include the git-cliff + CHANGELOG.md + GitHub Releases steps.
    - `Documentation/Architecture/000_introduction_and_guiding_principles.md` and `Documentation/roadmap.md` — note C1 as delivered if applicable.
    - `Documentation/Architecture/990_documentation_index_and_contribution_guide.md` — does CHANGELOG.md need an entry?
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `cliff.toml` exists at repo root and is valid TOML with `tag_pattern`, `body` template, and `chore(release)` skip filter.
  - `CHANGELOG.md` exists at repo root with `# Changelog` as its first line.
  - `bash release.sh --help` output mentions `git-cliff` as a prerequisite.
  - `bash release.sh --dry-run` (in a repo where git-cliff is installed) prints provisional tag, cliff notes, and the curl command preview, then exits 0 with no git changes.
  - `release.sh` exits with a clear error when `git-cliff` is absent or below version 2.4.
  - `release.sh` exits with "Unexpected commit count" if the HEAD count after the CHANGELOG.md commit does not match `EXPECTED_COUNT`.
  - `.github/workflows/archon-search-release.yml` has a `github-release` job with `needs: [publish]`, `if: startsWith(github.ref, 'refs/tags/')`, and `permissions: contents: write` at job level only.
  - `uv run pytest` passes with coverage ≥ 85%.
  - No warnings in the default test run.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
