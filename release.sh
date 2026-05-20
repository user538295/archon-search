#!/usr/bin/env bash
# Cut a release of archon-search.
#
# What this script does:
#   1. Pre-flight: working tree clean, on `main`, in sync with origin/main.
#   2. Compute the next CalVer tag: YY.M.<git-rev-list-count-HEAD>.
#   3. Confirm the tag is new (locally + on origin).
#   4. Confirm with the operator (skippable with `--yes` / `-y`).
#   5. `git tag $TAG` + `git push origin $TAG`.
#
# After the push, GitHub Actions runs `archon-search-release.yml` which:
#   - runs the eval gate,
#   - builds the wheel with `hatch build` (hatch-vcs reads the tag),
#   - publishes to PyPI via OIDC.
#
# Plain pushes to main do NOT trigger publishing. Only this script (or an
# equivalent tag push) starts a release.
#
# Usage:
#   bash release.sh           # interactive: prints tag, asks to confirm
#   bash release.sh -y        # non-interactive: tag + push without prompting
#   bash release.sh --dry-run # show what would happen; do not tag or push

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

# 2. Compute next CalVer tag — same formula as the prior CI workflow.
count="$(git rev-list --count HEAD)"
yy="$(date -u +%y)"
m="$(date -u +%-m 2>/dev/null || date -u +%-m 2>/dev/null || date -u +%m | sed 's/^0//')"
TAG="${yy}.${m}.${count}"

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

if [ "$DRY_RUN" = 1 ]; then
    echo
    echo "[dry-run] no tag created, no push made."
    exit 0
fi

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

# 4. Tag + push.
git tag "$TAG"
git push origin "$TAG"

cat <<EOF

Tag $TAG pushed.

Watch the release run:
  https://github.com/user538295/archon-search/actions/workflows/archon-search-release.yml

The wheel will appear on PyPI once the workflow's publish step succeeds:
  https://pypi.org/project/archon-search/
EOF
