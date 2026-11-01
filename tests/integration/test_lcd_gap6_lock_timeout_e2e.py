"""S22 (collection-descriptions plan, with S20, S23, S74) — a collection that stays busy while a description is saved.

``make_real_app`` boots the real lifespan from a ``[description]`` TOML section (provider ``ollama``) with a real LanceDB
store; only the model host is faked (``tests/_fake_model_server.py``). The collection lock is held on the app's own event
loop (``client.portal``) past the shortened ``INGEST_LOCK_TIMEOUT_S``, exactly as a long ingest or reindex holds it, so the
generation's model call finishes, its guarded write times out, and so does the recording of the failure, which needs the
same lock. What a consumer reads is asserted, not the lifecycle's internals: the status is ``failed`` through REST, MCP and
``collection info`` while the store holds the old row, the earlier description is kept, the lifecycle logs one WARNING (the
store adds one lock-timeout line for each of the two refused writes) and no description text, nothing stays ``pending``,
and the next ingest or describe regenerates once the lock is free. A manual set and a clear in between end the memory of
the failure for good. The set route answers 503 ``store_busy`` and leaves the stored description alone.

Run with:
    uv run pytest tests/integration/test_lcd_gap6_lock_timeout_e2e.py --no-cov -q
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli import _helpers as cli_helpers
from archon_search.cli.collection import collection as collection_command
from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
)
from archon_search.description_lifecycle import DescriptionLifecycle
from archon_search.server.schemas import WarmupResult
from archon_search.types import JobStatus
from tests._cli_testclient_bridge import CliBridge, cli_against_app
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app
from tests.integration.test_mcp_roundtrip_t2 import (
    _extract_tool_text,
    _mcp_call_tool,
    _mcp_initialize,
)

pytestmark = pytest.mark.integration

_COLLECTION = "busy-e2e"
_MODEL = "description-model"
_CHAT_PATH = "/api/chat"
_CONFIGURED_WINDOW = 8192
_PROVIDER_TIMEOUT_SECONDS = 120  # far above any wait here: a slow runner must not turn a held call into another failure
_SHORT_LOCK_TIMEOUT_S = 0.3
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_CLI_POLL_SECONDS = 0.05
_SEEDED_DOCUMENTS = 3
_GROWN_DOCUMENTS = 6  # +100%: the 20% rule asks for a new description
_REGROWN_DOCUMENTS = 7  # the failed run never advanced the baseline (3), so this still qualifies
_FIRST_REPLY = "Notes written when the collection was first ingested."
_SECOND_REPLY = "Notes the busy collection could not save."
_THIRD_REPLY = "Notes written after the collection was free again."
_FOURTH_REPLY = "Notes written by the describe run after the collection was free."
_MANUAL_TEXT = "Hand written note set while the collection was busy."
_LIFECYCLE_LOGGER = "archon_search.description_lifecycle"
_SERVER_LOGGER_PREFIX = "archon_search."
_DESCRIBE_JOB_TYPE = "description"
_STORE_LOGGER = "archon_search.store"
_REFUSED_WRITES = 2  # the generated text, then the record of the failure: each waits out the lock and times out
_OUTCOME_FAILED = "failed"
_OUTCOME_GENERATED = "generated"
_ACTIVE_JOB_STATUSES = frozenset({JobStatus.PENDING.value, JobStatus.QUEUED.value, JobStatus.RUNNING.value})
_STORE_BUSY_CODE = "store_busy"
_PENDING = "pending"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture(autouse=True)
def _quick_cli_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_helpers, "_POLL_INTERVAL_SECONDS", _CLI_POLL_SECONDS)


def _description_toml(base_url: str) -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


@contextlib.contextmanager
def _booted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str) -> Iterator[tuple[TestClient, str]]:
    """The real app with MCP mounted, warm-up and window resolution over; yields the client and the admin key."""
    with make_real_app(tmp_path, monkeypatch, toml_content=toml, mcp_enabled=True) as (client, _config, api_key):
        _wait_for(
            lambda: getattr(client.app.state, "warmup_result", None) not in (None, WarmupResult.PENDING),
            "the lifespan warm-up",
        )
        assert client.app.state.warmup_result == WarmupResult.DONE
        _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")
        yield client, api_key


@contextlib.contextmanager
def _collection_busy(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hold the collection's lock on the app's event loop, as a long ingest does, with a lock wait short enough to outlast.

    The timeout is shortened only here, so the seeding and the recovery are not exposed to it.
    """
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    lock = client.portal.call(client.app.state.search_store.lock_for, _COLLECTION)  # type: ignore[union-attr]
    client.portal.call(lock.acquire)  # type: ignore[union-attr]
    try:
        yield
    finally:
        client.portal.call(lock.release)  # type: ignore[union-attr]


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _write_documents(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"survey-{index}.txt").write_text(
            f"Field notes about marker-{index} and how the survey was carried out across the valley.\n" * 3,
            encoding="utf-8",
        )


def _ingest(client: TestClient, api_key: str, directory: Path) -> None:
    ingest_file_via_path(client, _COLLECTION, str(directory), api_key=api_key)


def _detail(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.get(f"/collections/{_COLLECTION}", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return response.json()


def _state(client: TestClient, api_key: str) -> tuple[str, str | None, str | None]:
    """The description, its source and its status, as the REST detail reports them."""
    detail = _detail(client, api_key)
    return detail["description"], detail["description_source"], detail["description_status"]


def _listed_status(client: TestClient, api_key: str) -> str | None:
    response = client.get("/collections/", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["name"] == _COLLECTION)["description_status"]


def _lifecycle(client: TestClient) -> DescriptionLifecycle:
    return client.app.state.description_lifecycle


def _await_settled(client: TestClient) -> None:
    """The generation of the collection has ended (settled, whatever the verdict): nothing is pending any more."""
    _wait_for(lambda: not _lifecycle(client).is_pending(_COLLECTION), "the generation to settle")


def _seed(client: TestClient, api_key: str, directory: Path) -> None:
    """Ingest the first documents and wait until the description the ingest asked for is stored."""
    _write_documents(directory, _SEEDED_DOCUMENTS)
    _ingest(client, api_key, directory)
    _wait_for(lambda: _state(client, api_key)[2] == DESCRIPTION_STATUS_OK, "the first description")
    assert _state(client, api_key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)


def _stored_row(client: TestClient) -> Any:
    """The row exactly as the store holds it (no overlay), read on the app's loop."""
    return client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]


def _mcp_status(client: TestClient, api_key: str) -> str | None:
    session = _mcp_initialize(client, api_key)
    meta = _extract_tool_text(
        _mcp_call_tool(client, api_key, session, "get_collection_meta", {"name": _COLLECTION}), "get_collection_meta"
    )
    return meta["description_status"]


def _cli_status(cli: CliBridge) -> str:
    info = cli.invoke(collection_command, ["info", _COLLECTION])
    assert info.exit_code == 0, info.output
    lines = [line for line in info.output.splitlines() if line.startswith("description_status: ")]
    assert len(lines) == 1, info.output
    return lines[0].removeprefix("description_status: ")


def _lifecycle_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING and record.name == _LIFECYCLE_LOGGER]


def _store_lock_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name == _STORE_LOGGER and "lock timeout" in record.getMessage()
    ]


def _description_fields(row: Any) -> tuple[Any, ...]:
    """Every stored field a description write may change."""
    return (
        row.description,
        row.description_source,
        row.description_status,
        row.last_described,
        row.described_at_doc_count,
        row.description_embedding,
    )


def _logged_by_the_server(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the server's own loggers wrote (the MCP library's DEBUG dump of a tool answer is not the server's)."""
    return " ".join(
        f"{record.getMessage()} {record.args} {record.exc_text}"
        for record in caplog.records
        if record.name.startswith(_SERVER_LOGGER_PREFIX)
    )


def _terminal_job(client: TestClient, api_key: str, job_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def terminal() -> bool:
        response = client.get(f"/jobs/{job_id}", headers=_headers(api_key))
        assert response.status_code == 200, response.text
        latest.update(response.json())
        return latest["status"] not in _ACTIVE_JOB_STATUSES

    _wait_for(terminal, f"job {job_id} to end")
    return latest


def _start_describe(client: TestClient, api_key: str) -> str:
    response = client.post(f"/collections/{_COLLECTION}/describe", headers=_headers(api_key))
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["job_type"], job["collection"]) == (_DESCRIBE_JOB_TYPE, _COLLECTION)
    return job["job_id"]


def test_e2e_generation_that_meets_a_busy_collection_reads_failed_and_the_next_ingest_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S22, S20: the model answered, but the collection stayed busy past the lock wait: REST detail and list, MCP and
    ``collection info`` all say ``failed`` (not ``ok`` and not ``pending``), the earlier description is kept in the store
    and in every answer, the log holds one WARNING and no description text. Once the lock is free the next ingest asks again
    (the failed run left the baseline where it was) and the new text lands as ``ok``."""
    caplog.set_level(logging.DEBUG)
    corpus = tmp_path / "corpus"
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        _seed(client, key, corpus)

        server.hold()  # the model call of the next generation stays in flight until the collection is busy
        server.reply_text = _SECOND_REPLY
        _write_documents(corpus, _GROWN_DOCUMENTS)
        _ingest(client, key, corpus)
        _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= 2, "the second model call")
        assert _state(client, key)[2] == _PENDING
        before = _description_fields(_stored_row(client))

        with _collection_busy(client, monkeypatch):
            server.release()
            _await_settled(client)

            assert _state(client, key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_FAILED)
            assert _listed_status(client, key) == DESCRIPTION_STATUS_FAILED
            assert _mcp_status(client, key) == DESCRIPTION_STATUS_FAILED
            assert _cli_status(cli) == DESCRIPTION_STATUS_FAILED

        assert _description_fields(_stored_row(client)) == before  # the write was refused: the store holds what it held
        assert _state(client, key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_FAILED)

        server.reply_text = _THIRD_REPLY
        _write_documents(corpus, _REGROWN_DOCUMENTS)
        _ingest(client, key, corpus)
        _wait_for(lambda: _state(client, key)[0] == _THIRD_REPLY, "the regenerated description")
        assert _state(client, key) == (_THIRD_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert _cli_status(cli) == DESCRIPTION_STATUS_OK
        assert len(server.requests_to(_CHAT_PATH)) == 3

    [warning] = _lifecycle_warnings(caplog)
    assert _COLLECTION in warning.getMessage()
    assert len(_store_lock_warnings(caplog)) == _REFUSED_WRITES  # the generated text, then the failure record
    logged = _logged_by_the_server(caplog)
    for text in (_SECOND_REPLY, _FIRST_REPLY, _THIRD_REPLY):
        assert text not in logged, "no description text reaches the log"


def test_e2e_a_manual_set_and_a_clear_after_an_unrecorded_failure_leave_no_failed_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection that has no description yet and whose first generation failed on the busy collection reads ``failed``.
    The operator sets a description and clears it again: the cleared row is as new (no status), and the failure the
    set and the clear made obsolete does not come back, whichever surface reads."""
    corpus = tmp_path / "corpus"
    with (
        FakeModelServer(reply_text=_SECOND_REPLY, hold_chat=True) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        _write_documents(corpus, _SEEDED_DOCUMENTS)
        _ingest(client, key, corpus)
        _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= 1, "the first model call")
        with _collection_busy(client, monkeypatch):
            server.release()
            _await_settled(client)
            assert _state(client, key) == ("", None, DESCRIPTION_STATUS_FAILED)

        put = client.put(f"/collections/{_COLLECTION}/description", json={"description": _MANUAL_TEXT}, headers=_headers(key))
        assert put.status_code == 200, put.text
        assert _state(client, key) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)
        cleared = client.delete(f"/collections/{_COLLECTION}/description", headers=_headers(key))
        assert cleared.status_code == 200, cleared.text

        assert _state(client, key) == ("", None, None)
        assert _listed_status(client, key) is None
        assert _mcp_status(client, key) is None
        info = cli.invoke(collection_command, ["info", _COLLECTION])
        assert info.exit_code == 0, info.output
        assert "description_status" not in info.output  # an empty status is left out


def test_e2e_describe_job_on_a_busy_collection_ends_done_with_outcome_failed_and_a_later_describe_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S22, S20: a describe job that meets the busy collection ends DONE with outcome ``failed`` and a ``failed`` status (a
    failed generation is an outcome, not a broken job); the earlier description is kept; one WARNING, no text. The command
    run once the collection is free generates and stores the new text (exit 0, ``ok``)."""
    caplog.set_level(logging.DEBUG)
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        _seed(client, key, tmp_path / "corpus")
        server.reply_text = _SECOND_REPLY

        with _collection_busy(client, monkeypatch):
            job = _terminal_job(client, key, _start_describe(client, key))

            assert job["status"] == JobStatus.DONE.value, job
            assert job["result"] == {"outcome": _OUTCOME_FAILED, "description_status": DESCRIPTION_STATUS_FAILED}
            assert not _lifecycle(client).is_pending(_COLLECTION)
            assert _state(client, key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_FAILED)
            assert _cli_status(cli) == DESCRIPTION_STATUS_FAILED

        server.reply_text = _FOURTH_REPLY
        described = cli.invoke(collection_command, ["describe", _COLLECTION, "--wait"])
        assert described.exit_code == 0, described.output
        assert f"outcome {_OUTCOME_GENERATED}" in described.output
        assert _state(client, key) == (_FOURTH_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)
        assert len(server.requests_to(_CHAT_PATH)) == 3  # the seed, the busy run, the recovery

    [warning] = _lifecycle_warnings(caplog)
    assert _COLLECTION in warning.getMessage()
    logged = _logged_by_the_server(caplog)
    for text in (_SECOND_REPLY, _FIRST_REPLY, _FOURTH_REPLY):
        assert text not in logged, "no description text reaches the log"


def test_e2e_manual_set_on_a_busy_collection_is_503_store_busy_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set route has no failed state to report: a lock that is not free in time is 503 ``store_busy`` with a
    ``Retry-After``, and the stored description, source and status are exactly what they were; the same call lands once
    the collection is free."""
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, key),
    ):
        _seed(client, key, tmp_path / "corpus")
        before = _stored_row(client)

        with _collection_busy(client, monkeypatch):
            refused = client.put(
                f"/collections/{_COLLECTION}/description", json={"description": _MANUAL_TEXT}, headers=_headers(key)
            )

            assert refused.status_code == 503, refused.text
            assert refused.json()["code"] == _STORE_BUSY_CODE
            assert refused.headers["Retry-After"]
            assert _state(client, key) == (_FIRST_REPLY, DESCRIPTION_SOURCE_GENERATED, DESCRIPTION_STATUS_OK)

        after = _stored_row(client)
        assert (after.description, after.description_source, after.description_status, after.last_described) == (
            before.description,
            before.description_source,
            before.description_status,
            before.last_described,
        )

        landed = client.put(
            f"/collections/{_COLLECTION}/description", json={"description": _MANUAL_TEXT}, headers=_headers(key)
        )
        assert landed.status_code == 200, landed.text
        assert _state(client, key) == (_MANUAL_TEXT, DESCRIPTION_SOURCE_MANUAL, DESCRIPTION_STATUS_OK)

