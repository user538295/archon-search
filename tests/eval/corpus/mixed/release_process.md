# Release Process

## Version Scheme

`YY.M.N` where:
- `YY` = two-digit year
- `M` = month (no leading zero)
- `N` = commit count (`git rev-list --count HEAD`)

## Steps

1. **Run tests**: `uv run pytest --no-cov -q --tb=no` — all must pass.
2. **Calculate version**: `COUNT=$(git rev-list --count HEAD); echo "v$(date +%y).$(date +%-m).$((COUNT+2))"`
3. **Update RELEASE.md** with the new version and changelog summary.
4. **Commit**: `git add RELEASE.md && git commit -m "chore(release): update RELEASE.md for vYY.M.N"`
5. **Verify version** still matches (the commit just added 1 to the count).
6. **Run release script**: `bash release.sh` — this creates the tag and publishes to PyPI.

## Release Gate

`release.sh` runs these guards before any mutation:
- Branch must be `main`
- Working tree must be clean
- `RELEASE.md` must contain the computed version string

If any guard fails, the script exits before creating a tag or making a commit.

## Rollback

Tags are immutable. If a bad release goes out, publish a patch release (`+1` to N). Do not delete published tags.
