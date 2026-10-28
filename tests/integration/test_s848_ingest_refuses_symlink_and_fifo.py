"""S848: path ingest reads regular files only — a FIFO or a symlink given as ``path`` is refused.

UserManual/50_ingestion_and_collections.md:59 — "Only regular files are read: a symlink, FIFO or device is
refused"; SecurityGuide/06_hardening_checklist.md:100 — "the parser reads only regular files (no symlinks, FIFOs
or devices)". Drives the real ASGI app (real store, pipeline and job runner; stub embedder) with real files.

Refused means a 4xx, or a job that ends ``FAILED`` (never ``DONE``) leaving the collection absent or empty.
"""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"})
_POLL_INTERVAL_S = 0.05
_JOB_TIMEOUT_S = 15.0
#: Whole-test wall-clock bound: a FIFO opened for reading blocks until a writer appears, which must fail, not hang.
_TEST_DEADLINE_S = 60
_NOT_FOUND = 404


@pytest.fixture(autouse=True)
def _bounded_test() -> Iterator[None]:
    """Fail the test (instead of hanging the suite) when it exceeds ``_TEST_DEADLINE_S``."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _on_deadline(_signum: int, _frame: object) -> None:
        pytest.fail(f"S848 test exceeded {_TEST_DEADLINE_S}s: something blocked, e.g. opened a FIFO for reading")

    previous = signal.signal(signal.SIGALRM, _on_deadline)
    signal.alarm(_TEST_DEADLINE_S)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, str]]]:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        yield client, {"Authorization": f"Bearer {api_key}"}


def _job_outcome(client: TestClient, headers: dict[str, str], response) -> str:
    """The job status the 202 *response* settles on, polled to a terminal state within a bounded time."""
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        poll = client.get(f"/jobs/{job_id}", headers=headers)
        assert poll.status_code == 200, poll.text
        last = poll.json()
        if last["status"] in _TERMINAL_STATUSES:
            return last["status"]
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"ingest job {job_id} did not reach a terminal status in {_JOB_TIMEOUT_S}s: {last}")


def _doc_count(client: TestClient, headers: dict[str, str], collection: str) -> int | None:
    """``doc_count`` of *collection*, or ``None`` when it does not exist."""
    response = client.get(f"/collections/{collection}", headers=headers)
    if response.status_code == _NOT_FOUND:
        return None
    assert response.status_code == 200, response.text
    return response.json()["doc_count"]


def _assert_refused(client: TestClient, headers: dict[str, str], path: Path, collection: str) -> None:
    response = client.post("/ingest", json={"path": str(path), "collection": collection}, headers=headers)
    if 400 <= response.status_code < 500:
        refusal = response.status_code
    else:
        outcome = _job_outcome(client, headers, response)
        refusal = None
        assert outcome == "FAILED", f"{path.name} must be refused, but its ingest job ended {outcome}"
    count = _doc_count(client, headers, collection)
    assert count in (None, 0), f"refused {path.name} (HTTP {refusal}) left {count} document(s) in {collection!r}"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_path_is_refused(app_client: tuple[TestClient, dict[str, str]], tmp_path: Path) -> None:
    client, headers = app_client
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    try:
        _assert_refused(client, headers, fifo, "c-fifo")
    finally:
        # Release a reader the server may have left blocked on the FIFO: O_RDWR never blocks.
        os.close(os.open(fifo, os.O_RDWR | getattr(os, "O_NONBLOCK", 0)))


def test_a_symlink_path_is_refused(app_client: tuple[TestClient, dict[str, str]], tmp_path: Path) -> None:
    client, headers = app_client
    target = tmp_path / "target.md"
    target.write_text("# Target\n\nContent behind the link.\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    response = client.post("/ingest", json={"path": str(link), "collection": "c-link"}, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "path is unsafe: symlink_path"
    assert _doc_count(client, headers, "c-link") is None


def test_a_symlink_to_a_directory_is_ingested(app_client: tuple[TestClient, dict[str, str]], tmp_path: Path) -> None:
    client, headers = app_client
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("# A\n\nA regular file.\n", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    response = client.post("/ingest", json={"path": str(link), "collection": "c-linkdir"}, headers=headers)

    assert _job_outcome(client, headers, response) == "DONE"
    assert _doc_count(client, headers, "c-linkdir") == 1


def test_a_directory_ingest_does_not_read_a_symlink(
    app_client: tuple[TestClient, dict[str, str]], tmp_path: Path
) -> None:
    client, headers = app_client
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nReachable only through the link.\n", encoding="utf-8")
    directory = tmp_path / "d"
    directory.mkdir()
    (directory / "real.md").write_text("# Real\n\nA regular file.\n", encoding="utf-8")
    (directory / "link.md").symlink_to(outside)

    response = client.post("/ingest", json={"path": str(directory), "collection": "c-dir"}, headers=headers)

    assert _job_outcome(client, headers, response) == "DONE"
    assert _doc_count(client, headers, "c-dir") == 1
