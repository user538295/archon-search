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

import ast
import os
import re
import socket
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

import httpx
import pytest

from archon_search.cli._helpers import _TERMINAL_STATUSES

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


# ---------------------------------------------------------------------------
# graph build-communities --wait (S3) — real CLI-to-server HTTP round trip
# against the graph-enabled smoke server, with real Leiden clustering compute.
# ---------------------------------------------------------------------------


def test_e2e_graph_build_communities_wait_against_server(smoke_server_graph_enabled) -> None:
    """``archon-search graph build-communities smoke_graph --wait`` against the
    graph-enabled smoke server (BE-9) must exit 0 and print the CLI's own
    "Community rebuild complete" message once the job reaches DONE (S3).

    TEST TRAP (repo convention): ``importorskip("leidenalg")`` must be the
    FIRST statement inside the test body, not module-level — module scope
    would skip every other test in this file, not just this one.

    Modeled on ``test_key_list_no_repr``: a real ``uv run archon-search``
    subprocess against the smoke server's ``base_url``/``api_key``, asserting
    on the CLI's own stdout as the primary success signal rather than
    independently re-polling the server.
    """
    pytest.importorskip("leidenalg")

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "graph",
            "build-communities",
            "smoke_graph",
            "--wait",
            "--api-url",
            smoke_server_graph_enabled.base_url,
            "--api-key",
            smoke_server_graph_enabled.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"build-communities --wait failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Community rebuild job submitted:" in result.stdout
    # Assert "(namespace: " appears in the submission line (brief 2026-07-15-130).
    assert "(namespace: " in result.stdout
    # "Community rebuild complete" alone is a prefix both the count-bearing and
    # count-less DONE branches print (graph_cmd.py's _poll_rebuild_job) — assert
    # "communities built" so this test proves Leiden actually produced a count on
    # the fixture's real >=2-node/>=1-edge graph, not just that the job reached
    # DONE. The completion line now ends with "(namespace: <ns>)." so the period
    # no longer immediately follows "built"; assert the token without the period.
    assert "Community rebuild complete: " in result.stdout
    assert "communities built" in result.stdout


# ---------------------------------------------------------------------------
# collection reindex --wait + jobs status (S4, S24) — HTTP-proxy CLI commands
# against the live smoke server.
# ---------------------------------------------------------------------------


def test_e2e_collection_reindex_wait_against_server(smoke_server) -> None:
    """``archon-search collection reindex smoke --wait`` against the smoke server
    must exit 0, print "Reindex job submitted:", and print the completion marker
    "Reindex complete for 'smoke'." once the job reaches DONE (S4).

    Modeled on ``test_e2e_graph_build_communities_wait_against_server``: a real
    ``uv run archon-search`` subprocess against the smoke server's
    ``base_url``/``api_key``, asserting on the CLI's own stdout as the primary
    success signal rather than independently re-polling the server.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "collection",
            "reindex",
            "smoke",
            "--wait",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"collection reindex --wait failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Reindex job submitted:" in result.stdout, (
        f"expected 'Reindex job submitted:' in stdout; got: {result.stdout!r}"
    )
    assert "Reindex complete for 'smoke'." in result.stdout, (
        f"expected completion marker in stdout; got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# collection add --wait (S1, S2) and ingest --wait (S6) — HTTP-proxy CLI commands
# against the live smoke server, exercising the full async-job round trip.
# ---------------------------------------------------------------------------


def test_e2e_collection_add_wait_against_server(smoke_server, tmp_path) -> None:
    """``archon-search collection add <dir> --wait`` against the smoke server
    must exit 0, print "Add collection job submitted:", and print the completion
    marker "ingested successfully." once the job reaches DONE (S1).

    Creates a temp directory (function-scoped ``tmp_path``) containing a single
    text document, invokes the CLI as a subprocess, and asserts:
    1. ``returncode == 0``
    2. The job-submitted line appears in stdout.
    3. The completion marker appears in stdout.
    4. The collection name extracted from stdout appears in ``GET /collections/``.

    Uses ``tmp_path`` (function-scoped) alongside the session-scoped
    ``smoke_server`` — pytest allows this scope mix.
    """
    # "smokeadd" is the collection name derived by the server (path_to_collection_name uses the basename).
    col_dir = tmp_path / "smokeadd"
    col_dir.mkdir()
    (col_dir / "doc.txt").write_text(
        "archon-search collection add smoke test document with enough content to produce chunks."
    )

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "collection",
            "add",
            str(col_dir),
            "--wait",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"collection add --wait failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Add collection job submitted:" in result.stdout, (
        f"expected 'Add collection job submitted:' in stdout; got: {result.stdout!r}"
    )
    assert "ingested successfully." in result.stdout, (
        f"expected completion marker in stdout; got: {result.stdout!r}"
    )

    # Parse the collection name from stdout: "Collection: '{collection_name}'"
    match = re.search(r"Collection: '([^']+)'", result.stdout)
    assert match, f"could not parse collection name from stdout: {result.stdout!r}"
    collection_name = match.group(1)

    collections_resp = httpx.get(
        f"{smoke_server.base_url}/collections/",
        headers={"Authorization": f"Bearer {smoke_server.api_key}"},
        timeout=10,
    )
    assert collections_resp.status_code == 200, (
        f"GET /collections/ failed: {collections_resp.status_code} {collections_resp.text}"
    )
    collection_names = [c["name"] for c in collections_resp.json()]
    assert collection_name in collection_names, (
        f"collection '{collection_name}' not found in GET /collections/: {collection_names}"
    )


def test_e2e_collection_add_progress_hint(smoke_server, tmp_path) -> None:
    """``archon-search collection add <dir>`` must print the progress hint
    "Track progress with: archon-search jobs status {id}" to stdout (string 2,
    collection.py:122 — the only approved string not yet asserted by the
    existing ``test_e2e_collection_add_wait_against_server`` test).
    """
    col_dir = tmp_path / "smokehint"
    col_dir.mkdir()
    (col_dir / "doc.txt").write_text(
        "Progress hint smoke test document with enough content to produce chunks."
    )

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "collection",
            "add",
            str(col_dir),
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"collection add failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Track progress with: archon-search jobs status" in result.stdout, (
        f"expected progress hint in stdout; got: {result.stdout!r}"
    )


def test_add_without_server(smoke_server) -> None:
    """``archon-search collection add`` against a closed port must exit 1 and
    print the approved connect-error string to stderr (S4).

    Binds a socket to a random port and holds it bound while the subprocess
    runs — connection is refused without binding a server (socket-hold pattern).
    Takes ``smoke_server`` to inherit ``xdist_group("smoke_e2e")`` serialisation.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        result = subprocess.run(
            [
                "uv",
                "run",
                "archon-search",
                "collection",
                "add",
                "/some/path",
                "--api-url",
                f"http://127.0.0.1:{dead_port}",
                "--api-key",
                smoke_server.api_key,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 1, (
        f"expected exit 1; got {result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "archon-search serve is not running. Start it first." in result.stderr, (
        f"expected connect-error string in stderr; got: {result.stderr!r}"
    )


def test_e2e_ingest_wait_against_server(smoke_server, tmp_path) -> None:
    """``archon-search ingest --path <file> --collection smoke --wait`` against
    the smoke server must exit 0 and print the completion marker
    "Ingest complete for 'smoke'." once the job reaches DONE (S6).

    Creates a temporary file and ingests it into the pre-existing ``smoke``
    collection, asserting on the CLI's own stdout as the primary success signal.
    """
    ingest_file = tmp_path / "extra.txt"
    ingest_file.write_text(
        "Additional document ingested into the smoke collection via the CLI ingest command."
    )

    # Capture pre-ingest chunk count so we can verify ingestion had an effect.
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    before_resp = httpx.get(
        f"{smoke_server.base_url}/collections/smoke",
        headers=headers,
        timeout=10,
    )
    assert before_resp.status_code == 200, (
        f"fixture: GET /collections/smoke failed before ingest: {before_resp.status_code}"
    )
    chunk_count_before = before_resp.json()["chunk_count"]

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "ingest",
            "--path",
            str(ingest_file),
            "--collection",
            "smoke",
            "--wait",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"ingest --wait failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Ingest job submitted:" in result.stdout, (
        f"expected 'Ingest job submitted:' in stdout; got: {result.stdout!r}"
    )
    assert "Ingest complete for 'smoke'." in result.stdout, (
        f"expected completion marker in stdout; got: {result.stdout!r}"
    )

    # Verify the ingest actually produced indexed content.
    after_resp = httpx.get(
        f"{smoke_server.base_url}/collections/smoke",
        headers=headers,
        timeout=10,
    )
    assert after_resp.status_code == 200, (
        f"GET /collections/smoke failed after ingest: {after_resp.status_code}"
    )
    chunk_count_after = after_resp.json()["chunk_count"]
    assert chunk_count_after > chunk_count_before, (
        f"ingest completed but chunk_count did not increase: before={chunk_count_before}, after={chunk_count_after}"
    )


def test_e2e_jobs_status_after_reindex(smoke_server) -> None:
    """``archon-search jobs status <job_id>`` for a reindex job that has
    reached DONE must exit 0 and print job_id, status, collection, and
    created_at fields (S24).

    Submits its own reindex job (no --wait) to obtain a fresh job_id, then
    waits for it to reach a terminal state via REST before running
    ``jobs status``.  Using a fresh job avoids inter-test state coupling and
    gives a predictable job_id to query.
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}

    # Submit a reindex job without --wait to capture the job_id.
    resp = httpx.post(
        f"{smoke_server.base_url}/collections/smoke/reindex",
        headers=headers,
        timeout=10,
    )
    assert resp.status_code == 202, (
        f"fixture: POST /collections/smoke/reindex failed: {resp.status_code} {resp.text}"
    )
    job_id = resp.json()["job_id"]

    # Poll until the job reaches a terminal state so ``jobs status`` exits 0.
    _TERMINAL = _TERMINAL_STATUSES
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        poll_resp = httpx.get(
            f"{smoke_server.base_url}/jobs/{job_id}",
            headers=headers,
            timeout=5,
        )
        if poll_resp.status_code == 200 and poll_resp.json().get("status") in _TERMINAL:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"reindex job {job_id} did not reach a terminal state within 60 s")

    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "jobs",
            "status",
            job_id,
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, (
        f"jobs status failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "status:     DONE" in result.stdout, (
        f"expected 'status:     DONE' in stdout; got: {result.stdout!r}"
    )
    assert job_id in result.stdout, (
        f"expected job_id {job_id!r} in stdout; got: {result.stdout!r}"
    )
    assert "collection:" in result.stdout, (
        f"expected 'collection:' in stdout; got: {result.stdout!r}"
    )
    assert "created_at:" in result.stdout, (
        f"expected 'created_at:' in stdout; got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# sync --wait (S7) and collection reindex-metadata --wait (S5) — HTTP-proxy
# CLI commands against the live smoke server; verify all SyncResult / ReindexResult
# fields are present in the completed job's result payload.
# ---------------------------------------------------------------------------


def test_e2e_sync_wait_all_result_fields_against_server(smoke_server) -> None:
    """``archon-search sync --wait`` against the smoke server must exit 0,
    print "Sync job submitted:", and "Sync complete."; the completed job
    result (fetched via ``GET /jobs/{id}``) must contain all 6 ``SyncResult``
    fields: ``added``, ``removed``, ``unchanged``, ``errors``, ``skipped``,
    ``updated`` (S7, C2).

    Note: the smoke server's ``ARCHON_SEARCH_CONFIG`` points at a non-existent
    file so ``load_config()`` uses defaults, where both ``pinned_collections``
    and ``collections`` are empty lists. ``POST /sync`` therefore calls
    ``SearchCollectionSync.sync([])`` which produces an all-empty
    ``SyncResult`` (nothing to add/remove/update). This test proves the
    CLI-to-server wiring and the 6-field result shape — not the sync
    algorithm itself (covered by unit tests).

    Modeled on the other e2e tests in this file: subprocess CLI against the
    live smoke server, with job_id parsed from stdout and result verified
    via a direct REST call.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "sync",
            "--wait",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"sync --wait failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Sync job submitted:" in result.stdout, (
        f"expected 'Sync job submitted:' in stdout; got: {result.stdout!r}"
    )
    assert "Sync complete." in result.stdout, (
        f"expected 'Sync complete.' in stdout; got: {result.stdout!r}"
    )

    # Parse job_id from stdout: "Sync job submitted: <job_id>. Track progress..."
    match = re.search(r"Sync job submitted: ([^\s.]+)", result.stdout)
    assert match, f"could not parse job_id from stdout: {result.stdout!r}"
    job_id = match.group(1)

    # Fetch completed job via REST and assert all 6 SyncResult fields are present.
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    job_resp = httpx.get(
        f"{smoke_server.base_url}/jobs/{job_id}",
        headers=headers,
        timeout=10,
    )
    assert job_resp.status_code == 200, (
        f"GET /jobs/{job_id} failed: {job_resp.status_code} {job_resp.text}"
    )
    job_data = job_resp.json()
    assert job_data["status"] == "DONE", (
        f"expected job status DONE; got: {job_data['status']!r}"
    )
    # A DONE sync job always carries a result dict — None here would be a server bug.
    job_result = job_data.get("result")
    assert job_result is not None, (
        f"DONE sync job has no result payload (server returned null); job={job_data!r}"
    )
    assert "added" in job_result, f"SyncResult missing 'added'; result={job_result!r}"
    assert "removed" in job_result, f"SyncResult missing 'removed'; result={job_result!r}"
    assert "unchanged" in job_result, f"SyncResult missing 'unchanged'; result={job_result!r}"
    assert "errors" in job_result, f"SyncResult missing 'errors'; result={job_result!r}"
    assert "skipped" in job_result, f"SyncResult missing 'skipped'; result={job_result!r}"
    assert "updated" in job_result, f"SyncResult missing 'updated'; result={job_result!r}"


def test_e2e_collection_reindex_metadata_wait_against_server(smoke_server) -> None:
    """``archon-search collection reindex-metadata smoke --wait`` against the
    smoke server must exit 0, print "Reindex-metadata job submitted:", and
    the completion marker; the completed job result (fetched via
    ``GET /jobs/{id}``) must contain all 5 ``ReindexResult`` fields:
    ``processed``, ``updated``, ``skipped``, ``ts_normalized``, ``warnings``
    (S5, C3).

    Uses the pre-seeded ``smoke`` collection so the metadata reindex operates
    on real indexed chunks.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "collection",
            "reindex-metadata",
            "smoke",
            "--wait",
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"collection reindex-metadata --wait failed: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Reindex-metadata job submitted:" in result.stdout, (
        f"expected 'Reindex-metadata job submitted:' in stdout; got: {result.stdout!r}"
    )
    assert "Reindex-metadata complete for 'smoke'." in result.stdout, (
        f"expected completion marker in stdout; got: {result.stdout!r}"
    )

    # Parse job_id from stdout: "Reindex-metadata job submitted: <job_id>. Track progress..."
    match = re.search(r"Reindex-metadata job submitted: ([^\s.]+)", result.stdout)
    assert match, f"could not parse job_id from stdout: {result.stdout!r}"
    job_id = match.group(1)

    # Fetch completed job via REST and assert all 5 ReindexResult fields are present.
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    job_resp = httpx.get(
        f"{smoke_server.base_url}/jobs/{job_id}",
        headers=headers,
        timeout=10,
    )
    assert job_resp.status_code == 200, (
        f"GET /jobs/{job_id} failed: {job_resp.status_code} {job_resp.text}"
    )
    job_data = job_resp.json()
    assert job_data["status"] == "DONE", (
        f"expected job status DONE; got: {job_data['status']!r}"
    )
    # A DONE reindex-metadata job always carries a result dict — None here would be a server bug.
    job_result = job_data.get("result")
    assert job_result is not None, (
        f"DONE reindex-metadata job has no result payload (server returned null); job={job_data!r}"
    )
    assert "processed" in job_result, f"ReindexResult missing 'processed'; result={job_result!r}"
    assert "updated" in job_result, f"ReindexResult missing 'updated'; result={job_result!r}"
    assert "skipped" in job_result, f"ReindexResult missing 'skipped'; result={job_result!r}"
    assert "ts_normalized" in job_result, f"ReindexResult missing 'ts_normalized'; result={job_result!r}"
    assert "warnings" in job_result, f"ReindexResult missing 'warnings'; result={job_result!r}"


# ---------------------------------------------------------------------------
# collection remove (S3, T-4) — HTTP-proxy CLI command against the live smoke
# server; synchronous 200 (no --wait), collection absent in GET /collections/
# afterwards.
# ---------------------------------------------------------------------------


def test_e2e_collection_remove_against_server(smoke_server, tmp_path) -> None:
    """``archon-search collection remove <name>`` against the smoke server
    must exit 0 and print "Removed collection '<name>'." (S3).

    Test design:
    1. Create a temp directory (function-scoped ``tmp_path``) with a real text
       document so the server can ingest it.
    2. Add the collection via ``POST /collections/`` REST call; poll until the
       ingest job reaches DONE (so the collection is fully registered and the
       meta row exists — the DELETE guard requires this).
    3. Run ``archon-search collection remove <name>`` as a subprocess; assert
       ``returncode == 0`` and the success message in stdout.
    4. Assert the collection IS present in ``GET /collections/`` before removal (discriminating pre-condition).
    5. Assert collection absent from ``GET /collections/`` and ``GET /collections/{name}`` returns 404.

    Uses ``tmp_path`` (function-scoped) alongside the session-scoped
    ``smoke_server`` — pytest allows this scope mix.
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}

    # Create a temp directory with a real document so ingest produces > 0 chunks.
    col_dir = tmp_path / "smokeremove"
    col_dir.mkdir()
    (col_dir / "doc.txt").write_text(
        "archon-search collection remove smoke test document with enough content to produce chunks."
    )
    collection_name = "smokeremove"  # path_to_collection_name uses the basename

    # Add the collection via REST (not via CLI) to seed it cleanly.
    add_resp = httpx.post(
        f"{smoke_server.base_url}/collections/",
        json={"path": str(col_dir)},
        headers=headers,
        timeout=10,
    )
    assert add_resp.status_code == 202, (
        f"fixture: POST /collections/ failed: {add_resp.status_code} {add_resp.text}"
    )
    job_id = add_resp.json()["job_id"]

    # Poll until the ingest job reaches DONE so the meta row exists.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        poll_resp = httpx.get(
            f"{smoke_server.base_url}/jobs/{job_id}",
            headers=headers,
            timeout=5,
        )
        if poll_resp.status_code == 200 and poll_resp.json().get("status") in _TERMINAL_STATUSES:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"add-collection job {job_id} did not reach a terminal state within 60 s")

    job_status = poll_resp.json()
    assert job_status.get("status") == "DONE", (
        f"add-collection job ended in non-DONE terminal state: {job_status!r}"
    )

    # Pre-removal: assert the collection IS present before we remove it (discriminating assertion).
    pre_remove_resp = httpx.get(
        f"{smoke_server.base_url}/collections/",
        headers=headers,
        timeout=10,
    )
    assert pre_remove_resp.status_code == 200, (
        f"pre-removal GET /collections/ failed: {pre_remove_resp.status_code} {pre_remove_resp.text}"
    )
    pre_remove_names = [c["name"] for c in pre_remove_resp.json()]
    assert collection_name in pre_remove_names, (
        f"collection '{collection_name}' not found in GET /collections/ before remove: {pre_remove_names}"
    )

    # Run the CLI remove command (synchronous — no --wait needed for DELETE).
    result = subprocess.run(
        [
            "uv",
            "run",
            "archon-search",
            "collection",
            "remove",
            collection_name,
            "--api-url",
            smoke_server.base_url,
            "--api-key",
            smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"collection remove failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert f"Removed collection '{collection_name}'." in result.stdout, (
        f"expected success message in stdout; got: {result.stdout!r}"
    )

    # Verify the collection is no longer listed.
    collections_resp = httpx.get(
        f"{smoke_server.base_url}/collections/",
        headers=headers,
        timeout=10,
    )
    assert collections_resp.status_code == 200, (
        f"GET /collections/ failed: {collections_resp.status_code} {collections_resp.text}"
    )
    collection_names = [c["name"] for c in collections_resp.json()]
    assert collection_name not in collection_names, (
        f"collection '{collection_name}' still present in GET /collections/ after remove: {collection_names}"
    )

    # Also verify the single-item detail endpoint returns 404 (separate code path from the list).
    detail_resp = httpx.get(
        f"{smoke_server.base_url}/collections/{collection_name}",
        headers=headers,
        timeout=10,
    )
    assert detail_resp.status_code == 404, (
        f"GET /collections/{collection_name} should return 404 after remove; got: {detail_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# T-5 structural single-writer check (S16) — no server required
# ---------------------------------------------------------------------------

# Read commands in collection.py that legitimately retain asyncio.run() +
# create_pipeline because they open LanceDB in-process.  All other @collection.command
# functions are write commands (HTTP proxies) and are checked by the guard.
# Fail-closed design: new commands are guarded by default; only explicitly
# blessed read commands are excluded.
_COLLECTION_READ_CMDS = frozenset({"list_cmd", "info"})


def _has_asyncio_run_call(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if ``node``'s body (and nested defs) contains ``asyncio.run(...)``."""
    for subnode in ast.walk(node):
        if isinstance(subnode, ast.Call):
            func = subnode.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            ):
                return True
    return False


def test_cli_write_commands_contain_no_direct_store_imports() -> None:
    """Structural guard (S16): CLI write-command modules must not import
    ``SearchPipeline``, ``SearchStore``, or ``create_pipeline`` directly,
    and write-command function bodies must not call ``asyncio.run()``.

    ``collection.py`` is mixed: read commands (``list_cmd``, ``info``) use
    ``asyncio.run()`` + ``_make_store()`` (in-process LanceDB reads via
    ``SearchStore``). Write commands are derived by excluding the known read
    commands from all ``@collection.command``-decorated functions (fail-closed:
    new commands are guarded by default).  Both ``async def`` and plain ``def``
    functions are checked for ``asyncio.run()`` calls.  A separate AST guard
    (3c) ensures write-command bodies do not call ``_make_store()``.

    ``ingest.py`` and ``sync.py`` are purely write-command modules; they are
    checked for ``asyncio.run()`` and ``SearchStore`` as whole-file text
    assertions (both currently have no ``asyncio`` or store import at all,
    so aliasing is not a practical concern).

    Non-automatable companion (S17): stop the server, run
    ``archon-search collection list`` and ``archon-search collection info <name>``,
    verify both succeed.  The server is not needed — those commands open LanceDB
    in-process — but stopping the session-scoped ``smoke_server`` mid-session
    would break sibling smoke tests in this file.
    """
    cli_dir = REPO_ROOT / "archon_search" / "cli"
    collection_py = cli_dir / "collection.py"
    ingest_py = cli_dir / "ingest.py"
    sync_py = cli_dir / "sync.py"

    # ------------------------------------------------------------------
    # 1. Forbidden import strings — purely write-command files only.
    # Covers the canonical import forms (plan spec S16). The real in-process
    # gateway is create_pipeline; SearchPipeline/SearchStore are checked
    # per spec and as an extra future-regression guard.
    #
    # collection.py is a mixed file: read commands (list_cmd, info) use
    # _make_store() which imports SearchStore at function scope (deferred,
    # startup-deferral policy). The whole-file text check is therefore
    # scoped to the purely write-command files (ingest.py, sync.py); the
    # write-command body guard for collection.py is in section 3c below.
    # ------------------------------------------------------------------
    for fpath in (collection_py, ingest_py, sync_py):
        src = fpath.read_text()
        assert "from archon_search.pipeline import SearchPipeline" not in src, (
            f"{fpath.name}: 'from archon_search.pipeline import SearchPipeline' must be absent "
            f"from CLI write-command modules (S16)"
        )

    for fpath in (ingest_py, sync_py):
        src = fpath.read_text()
        assert "from archon_search.store import SearchStore" not in src, (
            f"{fpath.name}: 'from archon_search.store import SearchStore' must be absent "
            f"from a purely write-command module (S16)"
        )

    # create_pipeline is the actual in-process LanceDB gateway; it must not
    # be imported in the purely write-command files.
    for fpath in (ingest_py, sync_py):
        src = fpath.read_text()
        assert "from archon_search.pipeline import create_pipeline" not in src, (
            f"{fpath.name}: 'from archon_search.pipeline import create_pipeline' must be absent "
            f"from a purely write-command module (S16) — use the server HTTP API instead"
        )

    # ------------------------------------------------------------------
    # 2. asyncio.run() absent from purely write-command files.
    # ------------------------------------------------------------------
    for fpath in (ingest_py, sync_py):
        src = fpath.read_text()
        assert "asyncio.run(" not in src, (
            f"{fpath.name}: asyncio.run() must not appear in a purely write-command module (S16)"
        )

    # ------------------------------------------------------------------
    # 3. collection.py AST checks: write-command bodies must not call
    #    asyncio.run(); read commands must still do so (positive control).
    # ------------------------------------------------------------------
    collection_src = collection_py.read_text()
    tree = ast.parse(collection_src)

    # Collect all top-level Click command functions (both def and async def).
    all_cmd_fns: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_cmd_fns[node.name] = node

    write_cmds = {name: node for name, node in all_cmd_fns.items() if name not in _COLLECTION_READ_CMDS}

    # 3a. No asyncio.run() in any write-command body.
    offending: list[str] = [
        f"{name} (line {node.lineno})"
        for name, node in write_cmds.items()
        if _has_asyncio_run_call(node)
    ]
    assert not offending, (
        f"collection.py write-command bodies must not call asyncio.run(); "
        f"found in: {offending} (S16)"
    )

    # 3b. Positive control: read commands must still call asyncio.run()
    # (confirms the read/write partition is real, not vacuously satisfied).
    for read_name in _COLLECTION_READ_CMDS:
        read_node = all_cmd_fns.get(read_name)
        assert read_node is not None, (
            f"collection.py: expected read command '{read_name}' to exist — "
            f"update _COLLECTION_READ_CMDS if it was renamed or removed"
        )
        assert _has_asyncio_run_call(read_node), (
            f"collection.py: read command '{read_name}' no longer calls asyncio.run() — "
            f"if it was converted to an HTTP proxy, move it to the write-command set "
            f"by removing it from _COLLECTION_READ_CMDS"
        )

    # 3c. Write-command bodies must not call _make_store().
    # _make_store imports SearchStore and is the read-only LanceDB gateway for
    # list_cmd/info; calling it from a write command would bypass the server proxy.
    def _has_make_store_call(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Call):
                func = subnode.func
                if isinstance(func, ast.Name) and func.id == "_make_store":
                    return True
        return False

    offending_store: list[str] = [
        f"{name} (line {node.lineno})"
        for name, node in write_cmds.items()
        if _has_make_store_call(node)
    ]
    assert not offending_store, (
        f"collection.py write-command bodies must not call _make_store() "
        f"(SearchStore gateway reserved for read commands); found in: {offending_store} (S16)"
    )
