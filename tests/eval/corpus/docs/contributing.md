# Contributing

## Development Setup

```bash
git clone <repo>
cd archon-search
uv sync --group dev
```

## Making Changes

1. Create a branch: `git checkout -b feat/short-description`
2. Write tests first (TDD is mandatory).
3. Implement the change.
4. Ensure all tests pass: `uv run pytest`
5. Ensure type checks pass: `uv run mypy archon_search/`
6. Open a pull request targeting `main`.

## Commit Messages

Follow Conventional Commits: `type(scope): description`

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.

## Code Review Checklist

- [ ] Tests cover the happy path and at least one error case
- [ ] No print statements (use `logging.getLogger(__name__)`)
- [ ] No new `platform.system()` calls — use the platform abstraction layer
- [ ] Docstrings on public functions and classes
- [ ] Coverage does not drop below 85%

## Release Process

Releases are automated via `release.sh`. Maintainers tag releases — contributors do not need to worry about this.
