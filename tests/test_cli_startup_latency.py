"""Import-boundary regression guard for CLI startup latency (BE-4).

Spawns a fresh interpreter subprocess running ``archon-search config show``
(a lightweight command) and asserts that the ML/agent packages that would
cause startup latency are NOT in ``sys.modules`` after the command runs.

These tests are subprocess-only.  In-process CliRunner shares ``sys.modules``
with the test runner (the xdist worker already imported archon_search and its
transitive deps), so CliRunner cannot reliably prove absence — see plan Q4.

Packages guarded:
- ``claude_agent_sdk`` — deferred into ``_call_haiku()`` by BE-1 (S1, S6)
- ``fastembed`` — deferred into ``serve()`` via ``run_server`` by BE-2 (S1)

NOT guarded:
- ``mcp`` — enters only via ``server/mcp.py`` (lazy FastMCP mount at serve
  time), never at CLI import-time; was never in sys.modules for lightweight
  commands before or after this feature — see plan Q4 and learnings
  2026-07-18.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# xdist_group serialises the two tests in this file onto one worker so the
# module-scoped ``loaded_modules`` fixture is never split across workers.
# The subprocess spawned here is intentionally lightweight — ``config show``
# must NOT load fastembed or claude_agent_sdk (that is what the tests prove),
# so this is not the ~2 GB model-loading subprocess that caused the 2026-07-05
# OOM crash.  A unique group name is therefore correct: there is no genuinely
# heavy-subprocess family to join, and joining a mock-based in-process group
# (e.g. "install") would misrepresent what those tests do.
pytestmark = pytest.mark.xdist_group("startup_latency")

# ---------------------------------------------------------------------------
# Internal script injected into the subprocess
# ---------------------------------------------------------------------------

_INSPECT_SCRIPT = """\
import sys
from archon_search.cli.main import main
from click.testing import CliRunner

runner = CliRunner()
result = runner.invoke(main, ["config", "show"])

# Delimiter must be on its own line so parsing is unambiguous even if the
# command's stdout contains commas.
print("---SYS_MODULES---")
print(",".join(sorted(sys.modules.keys())))

sys.exit(result.exit_code)
"""


def _run_lightweight_cmd() -> set[str]:
    """Spawn a fresh Python interpreter, run ``config show``, return sys.modules keys."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        env = {
            **os.environ,
            # Isolate the subprocess from the developer's real config so the
            # command never reads a real TOML that might pull in optional deps.
            "ARCHON_SEARCH_DATA_DIR": str(tmp),
            "ARCHON_SEARCH_CONFIG": str(tmp / "archon-search.toml"),
            # Prevent ARCHON_SEARCH_API_KEY leaking from the test environment
            # and keep it deterministic.
            "ARCHON_SEARCH_API_KEY": "0" * 64,
        }
        # Remove ANTHROPIC_API_KEY so description generation never fires.
        env.pop("ANTHROPIC_API_KEY", None)

        result = subprocess.run(
            [sys.executable, "-c", _INSPECT_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, (
        f"Subprocess exited with code {result.returncode}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )

    # Parse the sys.modules block from the output.
    marker = "---SYS_MODULES---"
    assert marker in result.stdout, (
        f"Subprocess stdout did not contain the '{marker}' delimiter.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    modules_line = result.stdout.split(marker, 1)[1].strip().split("\n")[0]
    loaded = set(modules_line.split(",")) if modules_line else set()
    # Positive control: the subprocess must have loaded at least the CLI entry
    # point and its eagerly-imported serve subcommand — if this fails, the
    # parsing logic is broken or the subprocess crashed silently before printing
    # the delimiter, making every absence assertion below vacuously true.
    #
    # Note on serve.py dependency: main.py eagerly imports all subcommand
    # modules at group-build time (``from archon_search.cli.serve import
    # serve``), so ``archon_search.cli.serve`` is always in sys.modules
    # whenever main.py is imported — even for lightweight commands like
    # ``config show``.  If main.py is ever changed to lazy-load subcommands,
    # this guard will catch if serve.py is no longer transitively imported,
    # preventing the fastembed absence test below from passing vacuously.
    assert "archon_search.cli.main" in loaded, (
        "Positive control failed: 'archon_search.cli.main' not in sys.modules "
        f"output — the subprocess output may be malformed.\nmodules_line={modules_line!r}"
    )
    assert "archon_search.cli.serve" in loaded, (
        "Positive control failed: 'archon_search.cli.serve' not in sys.modules "
        "output — main.py no longer eagerly imports serve.py, so the fastembed "
        "absence guard below would pass vacuously.  Either restore the eager "
        "import or update this test to reflect the new import structure."
    )
    return loaded


# ---------------------------------------------------------------------------
# Module-scoped fixture — subprocess is spawned once and shared across tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def loaded_modules() -> set[str]:
    """Spawn the subprocess once and return the set of loaded module names.

    Module scope ensures the subprocess is not spawned redundantly for every
    test in this file.
    """
    return _run_lightweight_cmd()


# ---------------------------------------------------------------------------
# Regression guard: lightweight command must NOT load the ML/agent stack
# ---------------------------------------------------------------------------


def test_lightweight_cmd_no_claude_agent_sdk(loaded_modules: set[str]) -> None:
    """S1, S6: ``claude_agent_sdk`` must be absent from sys.modules after
    ``archon-search config show``.

    Before BE-1, description_generator.py imported the SDK at module level,
    meaning every pipeline importer (CLI included) dragged the SDK in at
    startup.  After BE-1, the import lives inside ``_call_haiku()`` and fires
    only when description generation actually runs.  This test is the
    automated proof of that invariant — it will fail immediately if anyone
    moves the import back to module scope.
    """
    assert "claude_agent_sdk" not in loaded_modules, (
        "'claude_agent_sdk' appeared in sys.modules after 'archon-search config show'. "
        "This means the SDK import has leaked back to module scope — check "
        "archon_search/description_generator.py and ensure the import stays inside "
        "_call_haiku()."
    )


def test_lightweight_cmd_no_fastembed(loaded_modules: set[str]) -> None:
    """S1: ``fastembed`` must be absent from sys.modules after
    ``archon-search config show``.

    Before BE-2, cli/serve.py imported ``run_server`` at module level.
    ``server/app.py`` imports ``model_validation``, which imports ``fastembed``
    at module level — so every ``serve.py`` consumer paid the fastembed import
    cost at startup.  After BE-2 the import is deferred into ``serve()``.
    This test guards that invariant.

    Note: ``mcp`` is NOT guarded here — it enters only via ``server/mcp.py``
    (a lazy FastMCP mount at serve time), never at CLI import-time; it was
    absent before and after this feature — see plan Q4.
    """
    assert "fastembed" not in loaded_modules, (
        "'fastembed' appeared in sys.modules after 'archon-search config show'. "
        "This means the fastembed import has leaked back to module scope — check "
        "archon_search/cli/serve.py and ensure 'from archon_search.server.app import "
        "run_server' stays inside serve()."
    )
