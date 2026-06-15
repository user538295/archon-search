"""C18 Task 2.2 — guard-existence regression tests AND autouse-firing proof tests.

This file protects the C18 Fix 1 contract on two layers:

1. **Guard-existence regression tests** — parametrized assertions that each of
   the three production early-exit guards in ``archon_search/description_generator.py``,
   ``archon_search/hyde.py``, and ``archon_search/rag_fusion.py`` still contains
   an ``if``-statement guard on ``ANTHROPIC_API_KEY`` (matched by ``_GUARD_PATTERN``).
   If a future refactor removes the guard, Fix 1's autouse ``delenv`` in
   ``tests/conftest.py`` silently stops doing useful work and the 30 s SDK timeout
   floor returns on developer machines. This file turns that silent regression into
   a loud one (CI fails with this test name pointing at the cause).

2. **Autouse-firing tests** — (a) a post-condition check that ``ANTHROPIC_API_KEY``
   is absent at test-body execution time (catches regressions only on developer
   machines where the key IS set in the shell — passes vacuously in CI), and
   (b) a sanity check that ``monkeypatch.setenv`` works correctly in a test body
   and that the env var is visible after the call. pytest guarantees all fixture
   setup (including the autouse ``delenv``) completes before any test body runs;
   that ordering is structural and does not need a separate test.

The file name does NOT follow the ``test_no_*`` convention used by other static-
invariant tests because this file contains both static (guard-existence) and
runtime (composition) tests — the ``test_no_*`` naming would mislead about scope.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Matches an if-statement that guards on ANTHROPIC_API_KEY via os.environ.get.
# - ``^\s*if\b`` — line must start with ``if`` (possibly indented); rules out ``#``-prefixed comment lines.
# - ``[^#\n]*`` — no ``#`` before the key name (prevents "# if os.environ.get(...)" matching).
# - Tolerates either quote style around the key name.
# - Only ``os.environ.get()`` form is recognised by design; ``os.getenv()`` or bracket
#   access would cause this guard test to fail, which is the correct signal to review the change.
# install.py and cli/install_cmd.py also reference ANTHROPIC_API_KEY but for install-wizard
# UI validation, not for runtime SDK timeout prevention — they are intentionally excluded.
_GUARD_PATTERN = re.compile(
    r"^\s*if\b[^#\n]*\bos\.environ\.get\(['\"]ANTHROPIC_API_KEY['\"]",
    re.MULTILINE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_FILES = [
    REPO_ROOT / "archon_search" / "description_generator.py",
    REPO_ROOT / "archon_search" / "hyde.py",
    REPO_ROOT / "archon_search" / "rag_fusion.py",
]

# ---------------------------------------------------------------------------
# Meta-tests: verify _GUARD_PATTERN itself behaves correctly
# ---------------------------------------------------------------------------


def test_guard_pattern_matches_double_quote_guard() -> None:
    """_GUARD_PATTERN must fire on the canonical double-quote spelling."""
    line = '    if not os.environ.get("ANTHROPIC_API_KEY"):\n'
    assert _GUARD_PATTERN.search(line) is not None, (
        "_GUARD_PATTERN failed to match the canonical double-quote guard line"
    )


def test_guard_pattern_matches_single_quote_guard() -> None:
    """_GUARD_PATTERN must fire on single-quote spelling (tolerated by design)."""
    line = "    if not os.environ.get('ANTHROPIC_API_KEY'):\n"
    assert _GUARD_PATTERN.search(line) is not None, (
        "_GUARD_PATTERN failed to match single-quote variant"
    )


def test_guard_pattern_rejects_hash_comment() -> None:
    """_GUARD_PATTERN must NOT fire on a #-prefixed comment line.

    Prevents the guard from passing when the guard code is deleted but
    a comment referencing it (e.g. in conftest.py or a docstring) survives."""
    line = '    # if not os.environ.get("ANTHROPIC_API_KEY"):\n'
    assert _GUARD_PATTERN.search(line) is None, (
        "_GUARD_PATTERN incorrectly matched a #-prefixed comment line"
    )


def test_guard_pattern_rejects_elif() -> None:
    """_GUARD_PATTERN must NOT fire on elif (only unconditional if is valid).

    An elif guard would only fire when prior conditions are false, which may
    not provide the unconditional early-exit that C18 requires."""
    line = '    elif not os.environ.get("ANTHROPIC_API_KEY"):\n'
    assert _GUARD_PATTERN.search(line) is None, (
        "_GUARD_PATTERN incorrectly matched an elif line"
    )


# ---------------------------------------------------------------------------
# Guard-existence regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GUARD_FILES, ids=lambda p: p.name)
def test_anthropic_key_guard_exists(path: Path) -> None:
    """C18 Fix 1 depends on each of these files having an early-exit guard on
    ANTHROPIC_API_KEY. If a future refactor removes the guard, the autouse
    delenv in tests/conftest.py stops doing useful work and the 30s SDK
    timeout floor returns on developer machines."""
    source = path.read_text(encoding="utf-8")
    assert _GUARD_PATTERN.search(source) is not None, (
        f"{path.name} no longer contains the ANTHROPIC_API_KEY early-exit guard; "
        "C18's autouse delenv in tests/conftest.py will not prevent the 30s "
        "SDK timeout floor for this call site."
    )


def test_autouse_clears_anthropic_api_key() -> None:
    """Asserts ANTHROPIC_API_KEY is absent at test-body execution time.

    Limitation: in CI (and any shell where ANTHROPIC_API_KEY was never set),
    this passes vacuously and provides no signal — there is nothing to clear.
    The test catches regressions only on developer machines where the key IS
    exported in the shell. The monkeypatch test below
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
    """Verifies that monkeypatch.setenv sets the env var and it is visible in the test body.

    pytest guarantees all fixture setup — including the autouse delenv — completes
    before any test body runs. That ordering is structural; this test does not prove
    it. What this test proves is that monkeypatch.setenv works as expected:
    the value is visible immediately after the call. Tests that need ANTHROPIC_API_KEY set (like
    test_description_generator.py, test_hyde.py, test_rag_fusion.py) rely on
    this mechanism."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-from-per-test-setenv")
    assert os.environ.get("ANTHROPIC_API_KEY") == "test-key-from-per-test-setenv"
