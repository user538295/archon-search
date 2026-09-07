"""Container smoke test for the CPU image (Task 4.3).

These tests build the CPU image from the current source tree, start a
container, and verify it serves traffic. They are gated behind
``@pytest.mark.docker`` and the ``ARCHON_SEARCH_RUN_DOCKER_SMOKE`` opt-in
env var so the default ``uv run pytest`` invocation does not pay the
~5-minute build cost on dev machines that happen to have a docker daemon
installed. **No CI workflow runs them** — ``archon-search-release.yml:236-239``
records that omission as deliberate (no GPU runner; the image build step is the
gate). They are a manual, opt-in developer check::

    ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 uv run pytest tests/test_docker_smoke.py \\
        --no-cov -o addopts= --strict-markers --strict-config -n0 -m docker

The marker is registered both here and in ``pyproject.toml`` because
``addopts`` uses ``--strict-markers`` — without the ``pyproject.toml``
entry, pytest would reject the marker at collection time and silently
skip every test in this file. ``test_docker_marker_in_pyproject`` guards
that registration.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

IMAGE_TAG = "archon-search:smoke-test"
# Port 28765 avoids the docker-compose dev port (18765:8765) and the
# production port (8765) so the smoke test can coexist with a running
# compose stack on the same host. Each container-starting test takes its own
# offset, so 28765-28768 are reserved; the tests share ``xdist_group("docker")``
# and are serialised, but distinct ports keep a leaked container from failing
# the next test with a bind conflict.
HOST_PORT = 28765
CONTAINER_PORT = 8765
# MUST be a lowercase hex string: ``key_manager._validate_key`` (``_HEX_RE``,
# ``archon_search/key_manager.py:411`` / ``:556``) rejects anything else, and
# ``_load_from_env`` then logs a warning and falls through to an auto-generated
# key — so a non-hex value here silently yields 401 on every authenticated
# request. The pre-T-13 value ("smoketest") was never noticed because no smoke
# test authenticated; both S56 legs do.
SMOKE_API_KEY = "5c0e5ea11eb5"
# T-13 raised this from 30 s. ``ARCHON_EXTRAS`` defaults to
# ``graph,code,multilingual`` (scripts/docker-entrypoint.sh:10), so a container
# started from a freshly built image ALWAYS runs the cold
# ``pip install --target /pip-packages ".[...]"`` before it execs the server —
# the entrypoint's own log line calls that "network-bound … several minutes"
# (scripts/docker-entrypoint.sh:20) and gliner pulls torch + transformers.
# 30 s could only ever pass against an image whose /pip-packages volume was
# already seeded. 600 s matches the Dockerfile's own HEALTHCHECK
# ``--start-period=600s`` (Dockerfile:145), i.e. the budget the image itself
# declares for reaching a serving state. This is a ceiling, not a sleep:
# ``_wait_for_ready`` returns as soon as the endpoint answers.
READY_TIMEOUT_S = 600
# Budget for one ingest job to reach a terminal status. The first ingest in a
# fresh container downloads the fastembed embedder and the reranker onto the
# mounted volume before any chunk is written.
INGEST_TIMEOUT_S = 900
# The startup model-validation task is itself bounded by
# ``config.validation_timeout_seconds`` (default 60, ``archon_search/config.py:265``),
# so it needs its own modest ceiling rather than the 600 s readiness budget.
VALIDATION_TIMEOUT_S = 120

# scripts/docker-entrypoint.sh:32 — the last thing logged before ``exec "$@"``.
ENTRYPOINT_HANDOFF_PREFIX = "=== setup complete — handing off to:"
# scripts/docker-entrypoint.sh:7 — the EXIT trap's non-zero branch, i.e. what a
# `set -e` abort (i.e. the deleted spaCy-download block, K2) would leave behind.
ENTRYPOINT_ABORT_MARKER = "ERROR: entrypoint aborted"
# scripts/docker-entrypoint.sh:13 logs the resolved list verbatim.
ENTRYPOINT_DEFAULT_EXTRAS_LINE = "ARCHON_EXTRAS=graph,code,multilingual"


SMOKE_OPT_IN_ENV = "ARCHON_SEARCH_RUN_DOCKER_SMOKE"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _smoke_opted_in() -> bool:
    """Smoke tests are skipped unless the operator opts in.

    Without this gate every ``uv run pytest`` on a developer laptop with
    Docker Desktop installed would trigger the ~5-minute image build.
    The C9 plan called for the suite to run via ``-m docker``; this
    env var is the project-convention equivalent of marker exclusion,
    matching how ``live`` / ``live_eval`` skip when their infrastructure
    is absent.
    """
    return os.environ.get(SMOKE_OPT_IN_ENV) == "1"


def _container_running(container_id: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    ).stdout.strip() == "true"


def _wait_for_ready(url: str, timeout_s: int, container_id: str | None = None) -> int:
    """Poll ``url`` until it returns a status code or ``timeout_s`` expires.

    Returns the HTTP status code from the last successful response. Raises
    ``TimeoutError`` if the URL never responds within the budget. Uses a
    1-second sleep between attempts per the Task 4.3 spec.

    A 503 is retried rather than raised: ``/ready`` answers 503 while eager
    warm-up or the startup sync is still pending (``routes_ready.py:145``). But
    ``HTTPError`` is a SUBCLASS of ``URLError``, so it must be caught first to be
    recorded — otherwise a container stuck at 503 times out reporting a stale
    connection error and the real readiness body is never seen.

    When *container_id* is given, an exited container fails immediately with its
    logs. Without this, the exact regression S56 guards against — a ``set -e``
    entrypoint abort killing the container — would spin the full budget and then
    report only "connection refused", never ``ENTRYPOINT_ABORT_MARKER``.
    """
    deadline = time.monotonic() + timeout_s
    last_error: object = None
    while time.monotonic() < deadline:
        if container_id is not None and not _container_running(container_id):
            raise AssertionError(
                f"container exited before {url} became ready; "
                f"logs:\n{_docker_logs(container_id)}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.read(2048).decode('utf-8', 'replace')}"
            time.sleep(1)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(
        f"{url} did not become ready within {timeout_s}s; last error: {last_error!r}"
    )


def _run_detached(args: list[str]) -> str:
    """``docker run -d …`` and return the container id.

    ``--rm`` is deliberately never passed (see
    ``test_cpu_image_starts_and_serves_ready``): combined with ``-d`` the
    container would vanish before the teardown could read ``docker logs``.
    """
    container_id = subprocess.run(
        ["docker", "run", "-d", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    assert container_id, "docker run -d did not return a container id"
    return container_id


def _docker_logs(container_id: str) -> str:
    """Return the container's combined stdout+stderr.

    The entrypoint logs to stdout; application logs reach stderr because the
    image sets ``ARCHON_SEARCH_CONTAINER=1`` (Dockerfile:130).
    """
    proc = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    # Without this, a failed `docker logs` returns "" and every log assertion below
    # fails as "the entrypoint never logged X" rather than "docker logs itself broke".
    assert proc.returncode == 0, (
        f"`docker logs {container_id}` failed with exit {proc.returncode}: {proc.stderr}"
    )
    return proc.stdout + proc.stderr


def _api(url: str, payload: dict | None = None) -> dict:
    """Call a JSON endpoint with the smoke API key; POST when *payload* is given."""
    headers = {"Authorization": f"Bearer {SMOKE_API_KEY}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _wait_for_model_validation(base_url: str, timeout_s: int) -> dict:
    """Poll ``GET /status`` until the background model-validation task has run.

    ``model_validation`` is ``None`` until the lifespan's background task
    commits a result, so reading ``provider_warnings`` before that would assert
    on an empty list for the wrong reason. ``validate_models_async`` is itself
    bounded — ``server/app.py:616`` passes ``config.validation_timeout_seconds``
    (default 60, ``archon_search/config.py:265``) — so ``validated_at`` is
    always eventually set.
    """
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _api(f"{base_url}/status").get("model_validation")
        if last is not None and last.get("validated_at"):
            return last
        time.sleep(1)
    raise TimeoutError(
        f"model_validation did not complete within {timeout_s}s; last value: {last!r}"
    )


def _wait_for_job(base_url: str, job_id: str, timeout_s: int) -> dict:
    """Poll ``GET /jobs/{job_id}`` until the job reaches a terminal status.

    Reuses the server's own ``_TERMINAL_STATUSES`` rather than restating the
    membership list — ``JobStatus`` is a ``str`` Enum, so the wire-format status
    string matches an enum member directly, and ``tests/test_types_failed_expired.py``
    already guards every place that set is duplicated.
    """
    from archon_search.jobs.store import _TERMINAL_STATUSES  # noqa: PLC0415

    deadline = time.monotonic() + timeout_s
    job: dict = {}
    while time.monotonic() < deadline:
        job = _api(f"{base_url}/jobs/{job_id}")
        if job.get("status") in _TERMINAL_STATUSES:
            return job
        time.sleep(2)
    raise TimeoutError(
        f"job {job_id} did not reach a terminal status within {timeout_s}s; last: {job!r}"
    )


@pytest.fixture(scope="module")
def cpu_image() -> Iterator[str]:
    """Build the CPU image once per module; yield the tag.

    The image is intentionally NOT removed in teardown — repeat smoke
    runs on the same host reuse the build layer cache. CI runners are
    ephemeral so leftover images don't accumulate.
    """
    if not _smoke_opted_in():
        pytest.skip(
            f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke build"
        )
    if not _docker_available():
        pytest.skip("docker not available")
    # Build budget: 25 min. The Task 4.3 spec suggested 300s, but a cold
    # `pip install .` pulls torch + onnxruntime + fastembed model deps,
    # which on a clean BuildKit cache exceeds five minutes on both
    # GitHub-hosted runners and developer laptops. We still fail if the
    # build truly stalls, just with a budget that accommodates a fresh
    # transitive-dep download.
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, str(REPO_ROOT)],
        check=True,
        timeout=1500,
    )
    yield IMAGE_TAG


# ---------------------------------------------------------------------------
# Marker registration guard (always-on)
# ---------------------------------------------------------------------------


def test_docker_marker_in_pyproject() -> None:
    """``addopts`` uses ``--strict-markers`` — the docker marker must be
    registered in ``pyproject.toml`` or pytest rejects the marker at
    collection time and every smoke test silently disappears.
    """
    with PYPROJECT.open("rb") as fp:
        data = tomllib.load(fp)
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("docker:") or m.startswith("docker ") for m in markers), (
        "pyproject.toml [tool.pytest.ini_options].markers must register a "
        "'docker:' marker so --strict-markers does not reject @pytest.mark.docker"
    )


# ---------------------------------------------------------------------------
# Docker-gated smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_cpu_image_starts_and_serves_ready(cpu_image: str) -> None:
    """Build the CPU image, run it as the default user with an anonymous
    volume, and assert ``/ready`` returns HTTP 200 within ``READY_TIMEOUT_S``.

    The container runs detached (``-d``) so the test process can poll
    readiness. ``--rm`` is intentionally omitted: when combined with
    ``-d`` the container would be removed before the ``docker rm -f``
    teardown could log diagnostics on failure.
    """
    container_id = _run_detached(
        [
            "-e", f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
            "-p", f"{HOST_PORT}:{CONTAINER_PORT}",
            cpu_image,
        ]
    )

    try:
        status = _wait_for_ready(
            f"http://localhost:{HOST_PORT}/ready", READY_TIMEOUT_S, container_id
        )
        assert status == 200, f"/ready returned HTTP {status}, expected 200"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "-v", container_id],
            check=False,
            timeout=30,
        )


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_uid_1000_can_write_data_dir(cpu_image: str) -> None:
    """Mount a host directory at ``/data`` with UID 1000 ownership and
    assert that the key file lands on the volume.

    ``tempfile.mkdtemp()`` creates the dir owned by the current process
    UID; ``os.chmod(..., 0o777)`` is required so UID 1000 inside the
    container can write to it. Without the chmod, ``load_or_generate_key()``
    silently fails and no ``.search.env`` file appears.

    The write comes from ``persist_key`` on the env-var branch
    (``key_manager.py:467-470``), not from the auto-generate branch: T-13 made
    ``SMOKE_API_KEY`` a valid lowercase-hex value, so ``_load_from_env`` now
    returns it instead of rejecting it. Either branch writes the same file, so
    what this test proves — UID 1000 can write ``/data`` — is unchanged.
    """
    tmp_dir = tempfile.mkdtemp(prefix="archon-smoke-")
    os.chmod(tmp_dir, 0o777)

    container_id: str | None = None
    try:
        container_id = _run_detached(
            [
                "--user", "1000",
                "-v", f"{tmp_dir}:/data",
                "-e", f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
                "-p", f"{HOST_PORT + 1}:{CONTAINER_PORT}",
                cpu_image,
            ]
        )

        status = _wait_for_ready(
            f"http://localhost:{HOST_PORT + 1}/ready", READY_TIMEOUT_S, container_id
        )
        assert status == 200, f"/ready returned HTTP {status}, expected 200"

        key_file = Path(tmp_dir) / ".search.env"
        assert key_file.exists(), (
            f"Expected {key_file} to be created by UID 1000 on the mounted "
            f"volume; container could not write to /data"
        )
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", "-v", container_id],
                check=False,
                timeout=30,
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# T-13 / S56: the two ARCHON_EXTRAS=graph container legs
# ---------------------------------------------------------------------------


_SAMPLE_PY = '''"""Billing helpers used by the container smoke corpus."""


class InvoiceLedger:
    """Accumulates invoice totals for a single billing period."""

    def __init__(self, currency: str) -> None:
        self.currency = currency
        self.total = 0.0

    def add_invoice(self, amount: float) -> float:
        """Add one invoice amount to the running total and return it."""
        self.total += amount
        return self.total


def settle_period(ledger: InvoiceLedger, discount: float) -> float:
    """Apply a discount to the ledger total and return the settled amount."""
    return ledger.total * (1.0 - discount)
'''

_SAMPLE_MD = """# Quarterly billing review

Alice Johnson met Bob Carter in Berlin to review the quarterly billing report
for Contoso Ltd. The review covered the invoice ledger, the settlement window,
and the outstanding balance carried over from the previous quarter.

Contoso Ltd asked Alice Johnson to prepare a summary for the board meeting in
Munich, where the finance committee will decide whether to extend the payment
terms offered to Northwind Traders.
"""


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_cpu_image_starts_and_serves_ready_without_spacy(cpu_image: str) -> None:
    """S56 default leg: the entrypoint reaches ``exec "$@"`` with the DEFAULT
    ``ARCHON_EXTRAS`` (which includes ``graph``) and no spaCy in the image.

    K2 deleted the entrypoint's ``python -m spacy download en_core_web_sm``
    block. Because the script runs under ``set -e``, a leftover download step
    with no spaCy installed would abort the entrypoint before ``exec "$@"``
    and the port would never open. This asserts, in order:

    1. the image itself carries neither the ``spacy`` package nor the
       ``en_core_web_sm`` model package (the probe runs ``python3`` directly,
       bypassing the entrypoint, so it inspects the baked image);
    2. the entrypoint resolved the default extras list, reached its handoff
       log line, and left no ``set -e`` abort behind;
    3. ``/ready`` answers 200;
    4. with ``[graph].enabled`` at its default (``False``,
       ``archon_search/config.py:137``) there is no graph-related warning of
       any kind — ``graph_ner_status`` returns ``[]`` when graph is disabled
       (``archon_search/model_validation.py``), and ``provider_warnings`` is
       the only channel it has.

    Steps 1 and 2 are deliberately two different instruments. The probe covers
    the BAKED image only, because ``--entrypoint python3`` bypasses the
    entrypoint's runtime ``pip install --target /pip-packages``; the log scan in
    step 2 covers that install, since pip names every package it resolves and
    the entrypoint pipes the output through (``scripts/docker-entrypoint.sh:23``).
    Together they are S56's "no spaCy anywhere in the image" — neither alone is.
    """
    probe = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "python3", cpu_image, "-c",
            "import importlib.util as u; "
            "print(u.find_spec('spacy') is None, u.find_spec('en_core_web_sm') is None, "
            "u.find_spec('archon_search') is None)",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    # The third value is the liveness anchor: archon_search IS in the image, so a
    # find_spec that answered "None" for everything (broken probe, wrong interpreter)
    # would print True there and fail here rather than passing the two absences.
    assert probe.stdout.split() == ["True", "True", "False"], (
        "the image must contain neither the spacy package nor the en_core_web_sm "
        f"model package, and find_spec must still resolve a package that IS present; "
        f"probe printed {probe.stdout!r}"
    )

    host_port = HOST_PORT + 2
    base_url = f"http://localhost:{host_port}"
    container_id = _run_detached(
        [
            "-e", f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
            "-p", f"{host_port}:{CONTAINER_PORT}",
            cpu_image,
        ]
    )
    try:
        status = _wait_for_ready(f"{base_url}/ready", READY_TIMEOUT_S, container_id)
        assert status == 200, f"/ready returned HTTP {status}, expected 200"

        logs = _docker_logs(container_id)
        # Presence anchors for the two absence assertions below.
        assert ENTRYPOINT_DEFAULT_EXTRAS_LINE in logs, (
            f"expected the entrypoint to log {ENTRYPOINT_DEFAULT_EXTRAS_LINE!r} "
            f"(ARCHON_EXTRAS left at its default); logs:\n{logs}"
        )
        assert ENTRYPOINT_HANDOFF_PREFIX in logs, (
            f"expected the entrypoint to reach its `exec \"$@\"` handoff line "
            f"{ENTRYPOINT_HANDOFF_PREFIX!r}; logs:\n{logs}"
        )
        assert ENTRYPOINT_ABORT_MARKER not in logs, (
            f"the entrypoint aborted under `set -e`; logs:\n{logs}"
        )
        lowered = logs.lower()
        assert "spacy" not in lowered, (
            f"spaCy is still referenced — either the deleted download step (K2) "
            f"is back, or an extra now resolves it transitively; logs:\n{logs}"
        )
        assert "en_core_web_sm" not in lowered, (
            f"the deleted en_core_web_sm download step (K2) is still referenced; "
            f"logs:\n{logs}"
        )

        # `_wait_for_model_validation` is the presence anchor: it returns only once
        # `validated_at` is set, so the empty list below is a real validation pass
        # that found nothing to warn about, not the field's unpopulated default.
        model_validation = _wait_for_model_validation(base_url, VALIDATION_TIMEOUT_S)
        graph_warnings = [
            w for w in model_validation["provider_warnings"] if "graph" in w.lower()
        ]
        assert graph_warnings == [], (
            "a default container runs no graph extraction and must emit no "
            f"graph-related warning; got {graph_warnings!r}"
        )
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", container_id], check=False, timeout=30)


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_graph_enabled_image_degrades_to_code_symbols_only(
    cpu_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S56 enabled leg: ``[graph].enabled = true`` with no artifact provisioned.

    The server must still reach ready, and an ingest must still succeed with
    prose extraction degraded to code-symbols-only, carrying exactly one
    sanitized warning.

    No GLiNER checkpoint is provisioned into the container **and none is
    fetched**: the checkpoint cache directory
    (``paths.get_graph_models_dir()``, under the mounted ``/data``) is occupied
    by a regular file, so ``GLiNER.from_pretrained(cache_dir=…)`` fails
    immediately instead of pulling the ~1.2 GB artifact over the network. This
    leg proves the degrade path, not NER quality — the real-artifact lane
    (``-m graph_real_artifact``) covers the latter.

    The corpus is two files so the single ingest exercises both branches of
    ``GraphExtractor.extract``'s code/prose partition: ``sample.py`` yields
    only code chunks (``code_enricher`` stamps ``_symbol_type`` on every chunk
    of a supported source file, falling back to ``"module"``), and
    ``notes.md`` yields only prose chunks — so the sanitized backend-load
    warning fires exactly once across the flattened per-file results.
    """
    from archon_search.graph_extractor import _BACKEND_UNAVAILABLE_DETAIL  # noqa: PLC0415

    tmp_dir = Path(tempfile.mkdtemp(prefix="archon-smoke-graph-"))
    container_id: str | None = None
    try:
        (tmp_dir / "archon-search.toml").write_text("[graph]\nenabled = true\n", encoding="utf-8")
        corpus = tmp_dir / "corpus"
        corpus.mkdir()
        (corpus / "sample.py").write_text(_SAMPLE_PY, encoding="utf-8")
        (corpus / "notes.md").write_text(_SAMPLE_MD, encoding="utf-8")

        # Resolve the checkpoint cache path exactly the way the server will,
        # then occupy it with a regular file (see the docstring).
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_dir))
        from archon_search.paths import get_graph_models_dir  # noqa: PLC0415

        cache_dir = get_graph_models_dir()
        monkeypatch.undo()
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        cache_dir.write_text("", encoding="utf-8")

        # UID 1000 inside the container must be able to write everywhere on the
        # mount except the occupied cache path itself.
        for path in (tmp_dir, corpus, tmp_dir / "models", cache_dir.parent):
            os.chmod(path, 0o777)

        host_port = HOST_PORT + 3
        base_url = f"http://localhost:{host_port}"
        container_id = _run_detached(
            [
                "-v", f"{tmp_dir}:/data",
                "-e", f"ARCHON_SEARCH_API_KEY={SMOKE_API_KEY}",
                "-e", "ARCHON_SEARCH_CONFIG=/data/archon-search.toml",
                # Makes "no artifact is fetched" ENFORCED rather than incidental: without
                # it huggingface_hub may reach the network before it ever fails on the
                # occupied cache path, turning a fast degrade into a long stall.
                "-e", "HF_HUB_OFFLINE=1",
                "-p", f"{host_port}:{CONTAINER_PORT}",
                cpu_image,
            ]
        )

        status = _wait_for_ready(f"{base_url}/ready", READY_TIMEOUT_S, container_id)
        assert status == 200, f"/ready returned HTTP {status}, expected 200"

        submitted = _api(
            f"{base_url}/ingest", {"collection": "smoke", "path": "/data/corpus"}
        )
        job = _wait_for_job(base_url, submitted["job_id"], INGEST_TIMEOUT_S)
        assert job["status"] == "DONE", (
            f"ingest must survive a missing graph artifact; job={job!r}\n"
            f"logs:\n{_docker_logs(container_id)}"
        )

        # This assertion doubles as the presence anchor for the code-symbols-only
        # check below: GraphExtractor only appends this notice inside `if text_chunks:`
        # (graph_extractor.py:363), so exactly one occurrence proves notes.md really
        # did produce prose chunks and the prose path really was attempted and refused.
        warnings = job["result"]["warnings"]
        assert warnings == [_BACKEND_UNAVAILABLE_DETAIL], (
            "expected exactly one sanitized backend-unavailable notice and no "
            f"leaked exception text; got {warnings!r}"
        )

        # The degrade must be caused by a MISSING artifact, not by one that got
        # fetched and then failed: the occupied cache path is still the regular
        # file planted above, so nothing was ever provisioned into /data.
        assert cache_dir.is_file(), (
            f"{cache_dir} is no longer the planted regular file — an artifact was "
            f"provisioned, so this run did not exercise the no-artifact degrade"
        )

        graph = _api(f"{base_url}/graph/smoke")
        # Presence anchor: sample.py's code symbols really were extracted, so
        # the entity-type assertion below is not vacuously true on an empty graph.
        assert graph["node_count"] >= 1, (
            f"code-symbol extraction must be unaffected by the degrade; graph={graph!r}"
        )
        # Without this, the node cap firing (graph_inspector._truncate_graph)
        # could hide prose nodes and make the set equality below pass vacuously.
        assert graph["truncated"] is False, (
            f"the response is truncated, so `nodes` is not the whole graph and the "
            f"code-symbols-only assertion below would be vacuous; graph={graph!r}"
        )
        assert {node["entity_type"] for node in graph["nodes"]} == {"code_symbol"}, (
            "prose entities must be absent — the graph is code-symbols only; "
            f"nodes={graph['nodes']!r}"
        )
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "-f", "-v", container_id], check=False, timeout=30)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# T-2: whole-feature Docker compose smoke suite (DCS)
# ---------------------------------------------------------------------------


# 20 test functions in tests/smoke/docker/test_docker_cli.py; 1 is xfail(strict=False)
# for the advisory timing test. xfail counts as "1 xfailed", not "1 passed", so a
# fully-green run reports 19 passed. Floor is 19 to catch any silent skip regression.
_DOCKER_SMOKE_MIN_PASSED = 19


@pytest.fixture(scope="module")
def test_runner_image() -> str:
    """Build the Dockerfile.test image once per module; return a sentinel.

    Uses ``docker compose build archon-test-runner`` so the compose service
    definition (volumes, env vars) is fully honoured.  Image is not removed on
    teardown — the named volumes (``archon-docker-venv``, ``archon-uv-cache``)
    persist and speed up subsequent runs.
    """
    if not _smoke_opted_in():
        pytest.skip(f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker compose smoke suite")
    if not _docker_available():
        pytest.skip("docker not available")
    subprocess.run(
        ["docker", "compose", "build", "archon-test-runner"],
        check=True,
        cwd=str(REPO_ROOT),
        timeout=1500,
    )
    return "archon-test-runner"


@pytest.mark.docker
@pytest.mark.xdist_group("docker")
@pytest.mark.skipif(
    not _smoke_opted_in(),
    reason=f"{SMOKE_OPT_IN_ENV} not set; opt in to run the docker compose smoke suite",
)
@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_docker_smoke_suite_exits_0(test_runner_image: str) -> None:
    """Run ``tests/smoke/docker/`` inside the compose test-runner; assert exit 0.

    Builds the ``archon-test-runner`` image from ``Dockerfile.test``, then
    runs::

        docker compose run --rm archon-test-runner \\
            sh -c "uv sync ... && uv run pytest tests/smoke/docker/ ..."

    inside the container.  The ``-o addopts=`` flag strips the ini-level
    ``addopts`` (which contains ``-m "not smoke"`` and ``-n 8``) so the
    smoke-marked tests are collected and run serially on a single worker.

    Asserts:
    - ``returncode == 0`` (the whole suite passed)
    - at least ``_DOCKER_SMOKE_MIN_PASSED`` tests reported as passed (guards
      against silent collection-failure where pytest exits 0 with 0 tests)

    ``--extra graph`` and the extraction model download are intentionally omitted:
    the docker smoke tests spawn ``archon-search serve`` without graph enabled
    (the default), so gliner is never imported during these tests.
    """
    result = subprocess.run(
        [
            "docker", "compose",
            "run", "--rm", "archon-test-runner",
            "sh", "-c",
            (
                "uv sync --dev --extra hyde --extra rag-fusion --quiet && "
                "uv run pytest tests/smoke/docker/ --no-cov -o addopts= -v"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=600,
    )
    combined = result.stdout + result.stderr

    # Parse pytest summary counts for diagnostic output in the failure message.
    # returncode is the authoritative pass/fail signal; parsed counts add context.
    passed_match = re.search(r"(\d+) passed", combined)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    failed_match = re.search(r"(\d+) failed\b", combined)
    error_match = re.search(r"(\d+) error[s]?\b", combined)
    failed_count = int(failed_match.group(1)) if failed_match else 0
    error_count = int(error_match.group(1)) if error_match else 0

    assert result.returncode == 0, (
        f"docker compose smoke suite exited {result.returncode} "
        f"(passed={passed_count}, failed={failed_count}, errors={error_count})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert passed_count >= _DOCKER_SMOKE_MIN_PASSED, (
        f"Expected >= {_DOCKER_SMOKE_MIN_PASSED} passed tests; got {passed_count}. "
        f"If passed_count=0, the pytest summary line was not found in the output.\n"
        f"combined output:\n{combined}"
    )
