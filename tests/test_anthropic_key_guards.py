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

# Matches an if-statement that guards on ANTHROPIC_API_KEY.
# Accepted forms:
#   - ``if ... os.environ.get("ANTHROPIC_API_KEY"`` — direct env-var check (original C18 form)
#   - ``if ... self.is_key_available()`` — delegation to the canonical method (E0b BE-8 form;
#     ``is_key_available()`` calls ``os.environ.get("ANTHROPIC_API_KEY")`` internally)
# Rules:
# - ``^\s*if\b`` — line must start with ``if`` (possibly indented); rules out ``#``-prefixed comments.
# - ``[^#\n]*`` — no ``#`` before the guard expression.
# - Only ``os.environ.get()`` or ``self.is_key_available()`` are recognised by design;
#   ``os.getenv()``, bracket access, or other helpers would cause this guard test to fail,
#   which is the correct signal to review the change.
# install.py and cli/install_cmd.py also reference ANTHROPIC_API_KEY but for install-wizard
# UI validation, not for runtime SDK timeout prevention — they are intentionally excluded.
_GUARD_PATTERN = re.compile(
    r"^\s*if\b[^#\n]*(?:os\.environ\.get\(['\"]ANTHROPIC_API_KEY['\"]"
    r"|self\.is_key_available\(\))",
    re.MULTILINE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_FILES = [
    REPO_ROOT / "archon_search" / "description_generator.py",
    # G10 BE-1: Anthropic API-key guard moved to the provider adapter (Root-3).
    # hyde.py and rag_fusion.py no longer contain the runtime if-guard in their
    # generate() / generate_variants() methods — it lives in
    # AnthropicQueryExpansionProvider.is_key_available() + generate_hypothetical_doc()
    # / decompose_query() instead. hyde.py retains is_key_available() for the
    # status endpoint but has no early-exit if-guard in generate().
    REPO_ROOT / "archon_search" / "providers" / "anthropic_provider.py",
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


def test_guard_pattern_matches_is_key_available_delegation() -> None:
    """_GUARD_PATTERN must fire on the is_key_available() delegation form (E0b BE-8).

    When ``generate()`` delegates the key check to ``self.is_key_available()``,
    the early-exit guard still exists — it is just behind a method call.  The
    pattern must accept this form so that refactoring the inline guard to use
    the canonical method does not falsely signal a C18 regression.
    """
    line = "        if not self.is_key_available():\n"
    assert _GUARD_PATTERN.search(line) is not None, (
        "_GUARD_PATTERN failed to match the self.is_key_available() delegation form"
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


# ---------------------------------------------------------------------------
# Session-level hardening tests (C18 Fix 2)
# ---------------------------------------------------------------------------


def test_session_key_block_fires_before_test_body() -> None:
    """ANTHROPIC_API_KEY is absent at test-body time, proven by both the session
    fixture (_block_anthropic_key_at_session) and the function-scoped autouse.

    In CI (key never set) this passes vacuously. On a developer machine with the
    key exported in the shell, both fixtures must co-operate to clear it before
    this assertion runs — the session fixture clears it before any session-scoped
    fixture can fire, and the function-scoped autouse clears it again before
    every test body. Either one missing on a developer machine would let the key
    leak into session fixtures that call ingest_directory."""
    assert os.environ.get("ANTHROPIC_API_KEY") is None, (
        "_block_anthropic_key_at_session or _archon_isolated_data_dir failed to "
        f"clear ANTHROPIC_API_KEY; current value: {os.environ.get('ANTHROPIC_API_KEY')!r}"
    )


def test_anthropic_client_instantiation_raises_in_test_context() -> None:
    """anthropic.Anthropic() raises RuntimeError in the test suite.

    Proves the session fixture _block_anthropic_client is active and that no
    code path can reach the Anthropic network during tests, even if the env-var
    guard is bypassed or the API key leaks in via some other route.

    Skipped when the `anthropic` package is not installed (hyde/rag_fusion extras
    not active) — the mock is only needed when the package is present."""
    anthropic = pytest.importorskip("anthropic", reason="anthropic extra not installed; mock not needed")

    with pytest.raises(RuntimeError, match="Test suite attempted to instantiate the Anthropic client"):
        anthropic.Anthropic()


def test_async_anthropic_client_instantiation_raises_in_test_context() -> None:
    """anthropic.AsyncAnthropic() raises RuntimeError in the test suite.

    Same guarantee as the sync variant — proves the session mock covers both
    client constructors. Skipped when `anthropic` is not installed."""
    anthropic = pytest.importorskip("anthropic", reason="anthropic extra not installed; mock not needed")

    with pytest.raises(RuntimeError, match="Test suite attempted to instantiate the Anthropic client"):
        anthropic.AsyncAnthropic()
