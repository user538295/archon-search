"""Smoke tests for CLI commands (subprocess-level).

These tests spawn real ``archon-search`` subprocesses and assert that
commands complete within timing budgets and produce human-readable output
(no raw ``CollectionMeta(`` repr, no embedding vectors, etc.).

Server-dependent tests (added in BE-3 and later) require the session-scoped
``smoke_server`` fixture from ``conftest.py`` (added in BE-2). The only test
currently in this file — ``test_smoke_marker_in_pyproject`` — is a
configuration guard that does not require the server fixture.

Module-level ``pytestmark`` serialises this file on one xdist worker so that
all smoke tests share the single session-scoped server subprocess.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.xdist_group("smoke_e2e")

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# Marker registration guard (always-on — no @pytest.mark.smoke gate)
# ---------------------------------------------------------------------------


def test_smoke_marker_in_pyproject() -> None:
    """``addopts`` uses ``--strict-markers`` — the smoke marker must be
    registered in ``pyproject.toml`` or pytest rejects the marker at
    collection time and every smoke test silently disappears.

    This test also verifies that ``tests/smoke`` is in ``norecursedirs``
    (preventing the default ``uv run pytest`` from collecting smoke tests and
    spawning the server subprocess) and that the ``-m`` addopts filter
    excludes ``smoke`` (dual guard matching the ``live_benchmark`` pattern).
    """
    with PYPROJECT.open("rb") as fp:
        data = tomllib.load(fp)

    ini = data["tool"]["pytest"]["ini_options"]
    markers: list[str] = ini["markers"]
    norecursedirs: list[str] = ini["norecursedirs"]
    addopts: str = ini["addopts"]

    assert any(m.startswith("smoke:") or m.startswith("smoke ") for m in markers), (
        "pyproject.toml [tool.pytest.ini_options].markers must register a "
        "'smoke:' marker so --strict-markers does not reject @pytest.mark.smoke"
    )

    assert "tests/smoke" in norecursedirs, (
        "pyproject.toml [tool.pytest.ini_options].norecursedirs must include "
        "'tests/smoke' to prevent the default suite from auto-collecting smoke "
        "tests and spawning the server subprocess"
    )

    assert "not smoke" in addopts, (
        "pyproject.toml [tool.pytest.ini_options].addopts must contain "
        "'not smoke' in its -m filter (dual guard: norecursedirs + -m filter)"
    )


# ---------------------------------------------------------------------------
# Walking-skeleton CLI test (S2) — no server dependency
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("SMOKE_NO_TIMING") == "1", reason="timing disabled"
)
def test_help_completes_within_2s() -> None:
    """``archon-search --help`` is a pure CLI invocation (no server, no
    LanceDB, no fastembed model load) and must complete within 2 seconds.

    Also asserts the output is human-readable: no ``CollectionMeta(`` repr
    and no raw embedding-vector reprs (heuristic ``"[0."`` guard, S16 partial).
    """
    start = time.monotonic()
    result = subprocess.run(
        ["uv", "run", "archon-search", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"archon-search --help took {elapsed:.2f}s, expected < 2.0s"
    assert result.returncode == 0
    assert "CollectionMeta(" not in result.stdout
    # Low-value heuristic guard: --help output is click's static option
    # listing, so it structurally cannot contain a leaked embedding-vector
    # repr. Kept per spec / cross-command consistency with other CLI smoke
    # tests (S16 partial), not because it can meaningfully fail here.
    assert "[0." not in result.stdout


# ---------------------------------------------------------------------------
# Direct-LanceDB CLI commands (S3, S4) — require the smoke_server fixture for
# the pre-seeded "smoke" collection and its data dir, but talk to LanceDB
# in-process (not via HTTP), per the plan's "Note on CLI command architecture".
# ---------------------------------------------------------------------------


def _direct_store_env(data_dir: Path) -> dict[str, str]:
    """Env for CLI commands that open LanceDB directly via ``create_pipeline``.

    Points ``ARCHON_SEARCH_CONFIG`` at a non-existent file under the isolated
    data dir (mirrors ``conftest._subprocess_env``) so ``load_config()`` never
    reads the developer's real ``~/.archon-search/archon-search.toml`` — an
    operator TOML enabling e.g. multilingual support with fasttext-wheel
    uninstalled would otherwise break this subprocess on that machine.
    """
    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        "ARCHON_SEARCH_CONFIG": str(data_dir / "archon-search.toml"),
    }
    # Mirror conftest._subprocess_env's defensive pop: an operator's exported
    # ARCHON_SEARCH_HOST would otherwise leak in via **os.environ above.
    env.pop("ARCHON_SEARCH_HOST", None)
    return env


@pytest.mark.skipif(
    os.environ.get("SMOKE_NO_TIMING") == "1", reason="timing disabled"
)
def test_collection_list_no_repr(smoke_server) -> None:
    """``archon-search collection list`` connects to LanceDB directly via
    ``create_pipeline`` (not through the server's HTTP API) and must print
    plain ``name  docs=N  chunks=N`` lines, not a ``CollectionMeta(`` repr (S3,
    S16 partial).
    """
    start = time.monotonic()
    result = subprocess.run(
        ["uv", "run", "archon-search", "collection", "list"],
        env=_direct_store_env(smoke_server.data_dir),
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"collection list took {elapsed:.2f}s, expected < 5.0s"
    assert result.returncode == 0, (
        f"collection list failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "CollectionMeta(" not in result.stdout
    assert "[0." not in result.stdout
    # Positive assertion: the pre-seeded "smoke" collection must actually
    # appear in the output — otherwise a broken data-dir isolation (CLI
    # reading an empty/wrong LanceDB) would also pass this test.
    assert "smoke" in result.stdout


@pytest.mark.xfail(strict=False, reason="bug-007: collection info prints raw repr")
def test_collection_info_no_repr(smoke_server) -> None:
    """``archon-search collection info smoke`` should print human-readable
    detail, not the raw ``CollectionMeta(...)`` dataclass repr.

    Written as ``xfail(strict=False)`` (S4): ``archon_search/cli/collection.py``
    ``info()`` currently does ``click.echo(str(meta))``, which prints the raw
    repr (bug-007). ``strict=False`` keeps CI green while the bug exists and
    surfaces the fix as ``xpass`` (prompting removal of this marker) — never
    use ``strict=True`` here.
    """
    result = subprocess.run(
        ["uv", "run", "archon-search", "collection", "info", "smoke"],
        env=_direct_store_env(smoke_server.data_dir),
        capture_output=True,
        text=True,
        timeout=20,
    )

    # NOTE: pytest's xfail granularity is per-test-function, not per-assertion —
    # if EITHER assertion below fails, the whole test is reported as the
    # "expected" bug-007 failure. A non-zero returncode would in fact be a
    # DIFFERENT bug (not bug-007) that this xfail marker would incorrectly
    # mask as expected. This is a known, accepted limitation of function-level
    # xfail; there is no clean way to scope xfail to only the second assertion
    # within a single test function, so it is documented here rather than
    # worked around.
    assert result.returncode == 0
    assert "CollectionMeta(" not in result.stdout


# ---------------------------------------------------------------------------
# config show (S5) — no server dependency; reads/generates a TOML file only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("SMOKE_NO_TIMING") == "1", reason="timing disabled"
)
def test_config_show_timing_and_format() -> None:
    """``archon-search config show`` must complete within 2 seconds and print
    a ``[server]`` section header — whether it echoes an existing TOML file or
    generates the default one (S5).

    Uses an isolated, non-existent ``ARCHON_SEARCH_CONFIG`` path so the command
    prints the generated default TOML rather than the developer's real config.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "archon-search.toml"
        env = {**os.environ, "ARCHON_SEARCH_CONFIG": str(config_path)}

        start = time.monotonic()
        result = subprocess.run(
            ["uv", "run", "archon-search", "config", "show"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"config show took {elapsed:.2f}s, expected < 2.0s"
    assert result.returncode == 0
    assert "[server]" in result.stdout
    assert "CollectionMeta(" not in result.stdout


# ---------------------------------------------------------------------------
# maintenance run error path (S6) — no server running on a closed port
# ---------------------------------------------------------------------------


def test_maintenance_run_without_server(smoke_server) -> None:
    """``archon-search maintenance run --api-url <closed port>`` must exit 1
    and surface "Error contacting server" on stderr (S6) — asserts today's
    behaviour (bug-006 not yet fixed); update when briefs 250/260 land.

    The closed port is obtained by binding then immediately releasing a
    socket — guaranteed unused at the moment the subprocess starts, and
    distinct from ``smoke_server.port`` (which is live).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()

    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(smoke_server.data_dir),
        "ARCHON_SEARCH_API_KEY": smoke_server.api_key,
    }

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "maintenance",
            "run",
            "--api-url",
            f"http://127.0.0.1:{closed_port}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "Error contacting server" in result.stderr


# ---------------------------------------------------------------------------
# key list (S7) — HTTP-client CLI command against the live smoke server
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("SMOKE_NO_TIMING") == "1", reason="timing disabled"
)
def test_key_list_no_repr(smoke_server) -> None:
    """``archon-search key list`` talks to the running server over HTTP and
    must print plain ``id: ...  namespace: ...`` lines, not Python reprs (S7,
    S16 partial).

    The smoke server's managed-key store starts empty (the bootstrap
    ``ARCHON_SEARCH_API_KEY`` used to authenticate requests is never itself
    written as a managed key), so without seeding a real key, ``key list``
    would print "No keys found." and never exercise the per-record
    formatting loop this test is meant to prove is repr-free. Create one
    real managed key via ``POST /keys`` first.
    """
    create_resp = httpx.post(
        f"{smoke_server.base_url}/keys",
        json={"namespace": "default"},
        headers={"Authorization": f"Bearer {smoke_server.api_key}"},
    )
    assert create_resp.status_code == 201, (
        f"fixture sanity: POST /keys failed: {create_resp.status_code} {create_resp.text}"
    )

    start = time.monotonic()
    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "key",
            "list",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"key list took {elapsed:.2f}s, expected < 5.0s"
    assert result.returncode == 0, (
        f"key list failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "CollectionMeta(" not in result.stdout
    assert "[0." not in result.stdout
    # Positive assertion: the output must contain a real formatted record
    # line, not just the absence of a repr substring (which would also pass
    # on a vacuous "No keys found." output).
    assert "id: " in result.stdout
    assert "namespace: " in result.stdout
