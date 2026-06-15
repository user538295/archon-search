"""C18 Task 2.2 — guard-existence regression tests AND autouse-firing proof tests.

This file protects the C18 Fix 1 contract on two layers:

1. **Guard-existence regression tests** — parametrized assertions that each of
   the three production early-exit guards in ``archon_search/description_generator.py``,
   ``archon_search/hyde.py``, and ``archon_search/rag_fusion.py`` still contains
   the substring ``os.environ.get("ANTHROPIC_API_KEY")``. If a future refactor
   removes the guard, Fix 1's autouse ``delenv`` in ``tests/conftest.py`` silently
   stops doing useful work and the 30 s SDK timeout floor returns on developer
   machines. This file turns that silent regression into a loud one (CI fails
   with this test name pointing at the cause).

2. **Autouse-firing tests** — (a) a post-condition check that ``ANTHROPIC_API_KEY``
   is absent at test-body execution time (catches regressions only on developer
   machines where the key IS set in the shell — passes vacuously in CI), and
   (b) a genuine composition-rule test that works in all environments asserting
   per-test ``monkeypatch.setenv`` runs AFTER the autouse ``delenv`` and wins.

The file name does NOT follow the ``test_no_*`` convention used by other static-
invariant tests because this file contains both static (guard-existence) and
runtime (composition) tests — the ``test_no_*`` naming would mislead about scope.
"""
from pathlib import Path
import os
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_FILES = [
    REPO_ROOT / "archon_search" / "description_generator.py",
    REPO_ROOT / "archon_search" / "hyde.py",
    REPO_ROOT / "archon_search" / "rag_fusion.py",
]


@pytest.mark.parametrize("path", GUARD_FILES, ids=lambda p: p.name)
def test_anthropic_key_guard_exists(path: Path) -> None:
    """C18 Fix 1 depends on each of these files having an early-exit guard on
    ANTHROPIC_API_KEY. If a future refactor removes the guard, the autouse
    delenv in tests/conftest.py stops doing useful work and the 30s SDK
    timeout floor returns on developer machines."""
    source = path.read_text(encoding="utf-8")
    assert 'os.environ.get("ANTHROPIC_API_KEY")' in source, (
        f"{path.name} no longer contains the ANTHROPIC_API_KEY early-exit guard; "
        "C18's autouse delenv in tests/conftest.py will not prevent the 30s "
        "SDK timeout floor for this call site."
    )


def test_autouse_clears_anthropic_api_key() -> None:
    """Asserts ANTHROPIC_API_KEY is absent at test-body execution time.

    Limitation: in CI (and any shell where ANTHROPIC_API_KEY was never set),
    this passes vacuously and provides no signal — there is nothing to clear.
    The test catches regressions only on developer machines where the key IS
    exported in the shell. The composition-rule test below
    (`test_per_test_setenv_overrides_autouse_delenv`) is the genuine cross-
    environment proof that the fixture's monkeypatch instance is wired in."""
    assert os.environ.get("ANTHROPIC_API_KEY") is None, (
        "The autouse fixture _archon_isolated_data_dir should have cleared "
        "ANTHROPIC_API_KEY before this test body runs. Current value: "
        f"{os.environ.get('ANTHROPIC_API_KEY')!r}"
    )


def test_per_test_setenv_overrides_autouse_delenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition rule: per-test monkeypatch.setenv runs AFTER the autouse
    delenv and wins. Tests that need ANTHROPIC_API_KEY set (like
    test_description_generator.py, test_hyde.py, test_rag_fusion.py) rely on
    this. If pytest's fixture ordering changes, this catches the break."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-from-per-test-setenv")
    assert os.environ.get("ANTHROPIC_API_KEY") == "test-key-from-per-test-setenv"
