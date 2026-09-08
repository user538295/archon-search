#!/usr/bin/env bash
# Cut a release of archon-search.
#
# What this script does:
#   1. Pre-flight: working tree clean, on `main`, in sync with origin/main,
#      git-cliff >= 2.4 is available, then every lane archon-search-release.yml's
#      `test` job gates publish on — default suite, eval slice, integration
#      suite, docling parser lane (real OCR), graph_real_artifact lane (real
#      GLiNER checkpoint) — followed by the same coverage >= 85% enforcement.
#      A lane broken on `main` therefore fails HERE, before the tag is even
#      created, instead of only after it is already pushed to origin.
#   2. Compute the provisional CalVer tag: YY.M.<git-rev-list-count-HEAD + 1>
#      (the +1 accounts for the CHANGELOG.md commit added in step 4).
#   3. Confirm the tag is new (locally + on origin).
#   4. Invoke git-cliff to generate the changelog section; prepend it to
#      CHANGELOG.md, commit as `chore(release): update CHANGELOG.md for $TAG`,
#      and push the commit to origin/main.
#   5. Verify the commit count matches the provisional tag segment (guards
#      against partial-run divergence).
#   6. `git tag $TAG` + `git push origin $TAG`.
#
# After the push, GitHub Actions runs `archon-search-release.yml` which:
#   - runs the eval gate,
#   - builds the wheel with `hatch build` (hatch-vcs reads the tag),
#   - publishes to PyPI via OIDC,
#   - creates a GitHub Release from the CHANGELOG.md section (github-release job).
#
# Plain pushes to main do NOT trigger publishing. Only this script (or an
# equivalent tag push) starts a release.
#
# Prerequisites:
#   - git-cliff >= 2.4  (brew install git-cliff  or  cargo install git-cliff --version '>=2.4')
#
# Usage:
#   bash release.sh           # interactive: prints tag, asks to confirm
#   bash release.sh -y        # non-interactive: tag + push without prompting
#   bash release.sh --dry-run # preview tag, cliff notes, and GitHub Releases API
#                             # call — no writes, no pushes, no API calls

set -euo pipefail

ASSUME_YES=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "release.sh: unknown argument: $arg" >&2
            echo "Try: bash release.sh --help" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

bail() {
    echo "release.sh: $*" >&2
    exit 1
}

# 1. Pre-flight checks
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || bail "must be on branch 'main' (currently on '$branch')"

if [ -n "$(git status --porcelain)" ]; then
    bail "working tree is not clean — commit or stash changes first"
fi

git fetch --tags origin main >/dev/null 2>&1

local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/main)"
if [ "$local_head" != "$remote_head" ]; then
    bail "local main ($local_head) does not match origin/main ($remote_head) — pull or push first"
fi

check_git_cliff() {
    command -v git-cliff >/dev/null 2>&1 || bail "git-cliff not found in PATH — install with: brew install git-cliff or cargo install git-cliff --version '>=2.4'"
    local ver
    ver=$(git-cliff --version 2>&1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
    [ -n "$ver" ] || bail "could not parse git-cliff version — check 'git-cliff --version' output"
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if ! { [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 4 ]; }; }; then
        bail "git-cliff >= 2.4 required, found $ver"
    fi
}

check_git_cliff

if [ -z "${RELEASE_SH_TEST_MODE:-}" ]; then
    # Lanes 1-5 mirror archon-search-release.yml's `test` job step for step (same
    # markers, same --cov-append/--no-cov split, same coverage enforcement at the end)
    # so a lane that would fail the publish gate fails here instead, before the tag
    # exists. Lanes 6-11 run ONLY here, never in CI: benchmark/smoke need a local
    # server, docker needs a local daemon, live_benchmark/live_eval/live need real
    # model weights or a logged-in `claude` CLI that a CI runner doesn't have — this
    # is the full local pre-tag gate, CI's job stays a subset of it.
    echo "Running every excluded pytest marker as a local pre-tag gate (this takes a while)..."
    _suite_start=$SECONDS
    rm -f .coverage

    echo "[1/11] Default suite (coverage, no fail-under yet)..."
    uv run pytest -o addopts= --strict-markers --strict-config --cov=archon_search --cov-report=term-missing --cov-append -n0 \
        -m "not live and not eval and not benchmark and not integration and not live_eval and not docling and not graph_real_artifact" \
        || bail "default suite failed — fix all failures before releasing"

    echo "[2/11] Eval slice (thresholds, runxfail)..."
    uv run pytest -o addopts= --strict-markers --strict-config --cov=archon_search --cov-report=term-missing --cov-append --runxfail \
        -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/ \
        || bail "eval slice failed — fix all failures before releasing"

    echo "[3/11] Integration suite (disk-backed basetemp)..."
    mkdir -p /var/tmp/archon-search-it
    uv run pytest --basetemp=/var/tmp/archon-search-it -o addopts= --strict-markers --strict-config --cov=archon_search --cov-append \
        -m "integration and not eval and not docling and not graph_real_artifact" tests/ \
        || bail "integration suite failed — fix all failures before releasing"

    echo "[4/11] Docling parser lane (real OCR — minutes)..."
    uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 -m docling --junitxml=docling-results.xml tests/ \
        || bail "docling lane failed — fix all failures before releasing"
    uv run python -c "
import xml.etree.ElementTree as ET
cases = {c.get('name'): c for c in ET.parse('docling-results.xml').getroot().iter('testcase')}
names = ['test_pdf_page_number_in_search_response', 'test_image_file_assigns_page_start_one']
missing = [n for n in names if n not in cases]
assert not missing, f'expected docling testcases not found - {missing} - the lane did not run'
skipped = [n for n in names if cases[n].find('skipped') is not None]
assert not skipped, f'docling testcases skipped, no real OCR exercised - {skipped}'
" || bail "docling lane ran but its non-vacuity check failed"
    rm -f docling-results.xml

    echo "[5/11] Graph real-artifact lane (real GLiNER checkpoint)..."
    _prefetch_ok=false
    for attempt in 1 2 3; do
        uv run python -c "
import asyncio
from archon_search.prose_extraction_backend import ProseExtractionBackend
backend = ProseExtractionBackend()
asyncio.run(backend.load())
assert backend.load_count == 1, backend.load_count
print('Graph NER artifact prefetched')
" && { _prefetch_ok=true; break; }
        echo "Attempt $attempt failed; retrying in $((attempt * 5))s..."
        sleep $((attempt * 5))
    done
    [ "$_prefetch_ok" = true ] || bail "graph NER artifact prefetch failed after 3 attempts"

    ARCHON_SEARCH_DATA_DIR="$HOME/.archon-search" uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 \
        -m graph_real_artifact --junitxml=graph-real-artifact-results.xml tests/ \
        || bail "graph real-artifact lane failed — fix all failures before releasing"
    uv run python -c "
import xml.etree.ElementTree as ET
c = [t for t in ET.parse('graph-real-artifact-results.xml').getroot().iter('testcase') if t.get('name') == 'test_graph_ner_lane_non_vacuity']
assert len(c) == 1, f'expected exactly one test_graph_ner_lane_non_vacuity testcase, found {len(c)} — the lane did not run'
assert c[0].find('skipped') is None, 'test_graph_ner_lane_non_vacuity was skipped — the real-artifact guard did not execute'
" || bail "graph real-artifact lane ran but its non-vacuity check failed"
    rm -f graph-real-artifact-results.xml

    echo "[6/11] Benchmark lane (routing latency + wildcard scope)..."
    _bench_owns_server=false
    if ! curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
        ARCHON_SEARCH_DATA_DIR="$HOME/.archon-search" uv run archon-search serve &
        _bench_pid=$!
        _bench_owns_server=true
        for _attempt in $(seq 1 60); do
            curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break
            kill -0 "$_bench_pid" 2>/dev/null || break  # server process died/exited — stop waiting
            sleep 1
        done
    fi
    _bench_rc=0
    uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 -m benchmark tests/ || _bench_rc=$?
    if [ "$_bench_owns_server" = true ]; then
        kill "$_bench_pid" 2>/dev/null || true
        wait "$_bench_pid" 2>/dev/null || true
    fi
    [ "$_bench_rc" -eq 0 ] || bail "benchmark lane failed — fix all failures before releasing"

    echo "[7/11] Docker image smoke lane (real CPU image build — ~5 min)..."
    ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 uv run pytest tests/test_docker_smoke.py \
        --no-cov -o addopts= --strict-markers --strict-config -n0 -m docker \
        || bail "docker smoke lane failed — fix all failures before releasing"

    echo "[8/11] Smoke suite (real archon-search serve subprocess per test)..."
    uv run pytest tests/smoke/ -o addopts= --strict-markers --strict-config --no-cov -n0 \
        || bail "smoke suite failed — fix all failures before releasing"

    echo "[9/11] Live benchmark lane (real fastembed weights)..."
    uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 \
        -m live_benchmark tests/eval/live_benchmark/ \
        || bail "live benchmark lane failed — fix all failures before releasing"

    echo "[10/11] Live eval lane (real fastembed + real LLM — Anthropic key, else 'claude -p' CLI)..."
    uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 \
        -m live_eval tests/eval/live/ \
        || bail "live eval lane failed — fix all failures before releasing"

    echo "[11/11] Live llama.cpp lane (skips gracefully without a local llama-server)..."
    uv run pytest -o addopts= --strict-markers --strict-config --no-cov -n0 \
        -m "live and not live_eval and not live_benchmark" tests/integration/test_llama_cpp_e2e.py \
        || bail "live llama.cpp lane failed — fix all failures before releasing"

    echo "Enforcing coverage >= 85% across default+eval+integration lanes..."
    uv run coverage report --fail-under=85 || bail "coverage below 85% — fix before releasing"

    echo "All release-gating test lanes passed in $(( SECONDS - _suite_start ))s."
fi

# 2. Compute provisional CalVer tag (count+1 accounts for the CHANGELOG.md commit added later).
[ -n "${EXPECTED_COUNT_OVERRIDE:-}" ] && [ -z "${RELEASE_SH_TEST_MODE:-}" ] && \
    bail "EXPECTED_COUNT_OVERRIDE is set — unset it before running a real release"
yy="$(date -u +%y)"
m="$(date -u +%-m 2>/dev/null || date -u +%m | sed 's/^0//')"
EXPECTED_COUNT="${EXPECTED_COUNT_OVERRIDE:-$(( $(git rev-list --count HEAD) + 1 ))}"
TAG="${yy}.${m}.${EXPECTED_COUNT}"

# 3. Tag must be new.
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    bail "tag '$TAG' already exists locally — bump HEAD or delete the tag first"
fi
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then
    bail "tag '$TAG' already exists on origin — bump HEAD or delete the tag first"
fi

cat <<EOF
About to release archon-search:
  branch : $branch
  head   : $local_head
  tag    : $TAG
  remote : origin ($(git remote get-url origin))

Effect: creates and pushes tag '$TAG' to origin, which triggers
archon-search-release.yml on GitHub Actions. That workflow runs the eval
gate, builds the wheel, and publishes to PyPI via OIDC.
EOF

if [ "$DRY_RUN" != 1 ]; then
    if [ "$ASSUME_YES" != 1 ]; then
        printf '\nProceed? [y/N] '
        read -r reply || reply=""
        case "$reply" in
            y|Y|yes|YES) : ;;
            *)
                echo "release.sh: aborted."
                exit 1
                ;;
        esac
    fi
fi

# 4. Generate release notes, then update CHANGELOG.md, commit, and push to main.

CLIFF_NOTES=$(git-cliff --unreleased --tag "$TAG") || bail "git-cliff failed — check cliff.toml and git history"

if [ -z "$(echo "$CLIFF_NOTES" | tr -d '[:space:]')" ]; then
    bail "No conventional commits found since last tag. Nothing to release."
fi

# Synthesize quality notes with Claude (API key → direct call; fallback → claude CLI).
# Falls back to git-cliff output silently if both paths fail.
# Set NO_SYNTHESIS=1 to skip (always uses git-cliff output directly).
NOTES="$CLIFF_NOTES"
if [ "${NO_SYNTHESIS:-0}" != "1" ]; then
    echo "Synthesizing release notes with Claude..."
    PREV_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
    if [ -n "$PREV_TAG" ]; then
        RAW_COMMITS=$(git log --format="  %s%n%b" "${PREV_TAG}..HEAD" | sed '/^[[:space:]]*$/d' | awk 'NR<=300')
    else
        RAW_COMMITS=$(git log --format="  %s%n%b" | sed '/^[[:space:]]*$/d' | awk 'NR<=300')
    fi
    # Keep the ## [version] - date header from cliff; replace only the body.
    CLIFF_HEADER=$(printf '%s\n' "$CLIFF_NOTES" | awk 'NR<=2')
    if BODY=$(printf '%s' "$RAW_COMMITS" | python3 .github/scripts/synthesize_release_notes.py "$TAG"); then
        NOTES=$(printf '%s\n\n%s' "$CLIFF_HEADER" "$BODY")
    else
        echo "release.sh: Claude synthesis failed — using git-cliff output" >&2
    fi
fi

if [ "$DRY_RUN" = 1 ]; then
    FIRST_SECTION=$(printf '%s\n' "$NOTES" | awk '/^## /{if(found) exit; found=1; next} found')
    REPO=$(git remote get-url origin | sed 's/\.git$//' | sed 's|.*github\.com[/:]||')
    if command -v jq >/dev/null 2>&1; then
        PAYLOAD=$(jq -n --arg tag "$TAG" --arg body "$FIRST_SECTION" \
            '{tag_name: $tag, name: $tag, body: $body}')
    else
        PAYLOAD="(install jq to see the full payload)"
    fi
    cat <<__ARCHON_DRY_RUN_EOF__
[dry-run] provisional tag : $TAG
[dry-run] cliff notes     :
$NOTES
[dry-run] CI would call   :
  curl -s -w "\\n%{http_code}" \\
    -X POST https://api.github.com/repos/$REPO/releases \\
    -H "Authorization: Bearer \$GITHUB_TOKEN" \\
    -H "Accept: application/vnd.github+json" \\
    -H "X-GitHub-Api-Version: 2022-11-28" \\
    -d '$PAYLOAD'
[dry-run] no writes, no pushes, no API calls.
__ARCHON_DRY_RUN_EOF__
    exit 0
fi

[ -f CHANGELOG.md ] || bail 'CHANGELOG.md not found — run git-cliff setup first'
grep -q '^# Changelog$' CHANGELOG.md || bail 'CHANGELOG.md is missing the exact # Changelog header — cannot prepend. Ensure the file starts with: # Changelog'
tmp=$(mktemp ./tmp.XXXXXX)
trap 'rm -f "$tmp"' EXIT
NOTES="$NOTES" awk '
  /^# Changelog$/ {
    print           # print "# Changelog"
    getline         # consume the blank line following the header
    print ""        # print one blank line before notes
    print ENVIRON["NOTES"]
    print ""        # print one blank line after notes
    next
  }
  { print }
' CHANGELOG.md > "$tmp" && mv "$tmp" CHANGELOG.md

git add CHANGELOG.md
git commit -m "chore(release): update CHANGELOG.md for $TAG"
git push origin main

# 5. Tag + push.

# Verify commit count matches the provisional tag before tagging.
# This guard fires only after the CHANGELOG.md commit (added in release step 3);
# without that commit the count is always off by one.
actual_count="$(git rev-list --count HEAD)"
[ "$actual_count" -eq "$EXPECTED_COUNT" ] || bail "Unexpected commit count ($actual_count vs $EXPECTED_COUNT) — if the CHANGELOG commit succeeded but the tag push failed on a prior run, tag manually: git tag $TAG && git push origin $TAG"

git tag "$TAG"
git push origin "$TAG"

cat <<EOF

Tag $TAG pushed.

Watch the release run:
  https://github.com/user538295/archon-search/actions/workflows/archon-search-release.yml

The wheel will appear on PyPI once the workflow's publish step succeeds:
  https://pypi.org/project/archon-search/
EOF
