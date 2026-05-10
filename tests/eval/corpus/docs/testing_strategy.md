# Testing Strategy

## Philosophy

Tests are the primary design tool. Write tests before implementation. A test that fails clearly is more valuable than one that passes silently.

## Test Pyramid

**Unit tests (70%)** — Test one class or function in isolation. Use dependency injection to replace infrastructure with in-memory fakes. Run in milliseconds, never hit the network.

**Integration tests (20%)** — Test multiple components together. Use a real database (test-scoped transaction that rolls back). Marked `@pytest.mark.integration` and excluded from the default run.

**End-to-end tests (10%)** — Drive the full HTTP stack. Run against a locally started server. Marked `@pytest.mark.e2e`.

## Coverage Requirements

Minimum 85% line coverage enforced in CI. Branches that are intentionally unreachable (type narrowing, `assert False`) may be excluded with `# pragma: no cover`.

## Naming Convention

- File: `test_{module}.py`
- Function: `test_{scenario}_{expected_outcome}`
- Fixture: noun describing the object provided (e.g., `db_session`, `http_client`)

## Mocking Guidelines

Mock at the boundary, not the internals. If you find yourself patching a private method, the design needs refactoring. Use `unittest.mock.AsyncMock` for coroutines.
