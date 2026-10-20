"""Fault injection for ``JobStore`` persists: make chosen ``atomic_write_json`` calls raise."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import archon_search.jobs.store as store_module

SIMULATED_WRITE_FAILURE = "simulated disk full"
#: Persist failures to parametrize a caller's failure path with: the non-OSError proves the caller catches any
#: persist failure, not only the OSError ``atomic_write_json`` raises.
JOB_WRITE_FAILURES = [
    pytest.param(OSError(SIMULATED_WRITE_FAILURE), id="oserror"),
    pytest.param(TypeError("simulated serialization failure"), id="non_oserror"),
]


@dataclass
class WriteCounter:
    """The ``JobStore`` persists attempted inside a ``failing_job_writes`` block, failed ones included."""

    attempts: int = 0
    payloads: list[Any] = field(default_factory=list)

    def statuses_of(self, job_id: str) -> list[str]:
        """Every status *job_id* was written with, in write order."""
        return [item["status"] for payload in self.payloads for item in payload if item["job_id"] == job_id]


def persisted_statuses(jobs_path: Path) -> dict[str, str]:
    """Job id -> status as written in the jobs file (raw JSON: loading a JobStore would rewrite RUNNING rows)."""
    return {item["job_id"]: item["status"] for item in json.loads(jobs_path.read_text())}


@contextmanager
def failing_job_writes(
    *,
    count: int | None = None,
    exc: BaseException | Sequence[BaseException] | None = None,
    fail_after_write: int = 0,
) -> Iterator[WriteCounter]:
    """Make the first *count* ``JobStore`` persists raise *exc*; the later ones run the real ``atomic_write_json``.

    ``count=None`` fails every persist (``count=0`` fails none: use ``recording_job_writes``).
    *exc* defaults to ``OSError(SIMULATED_WRITE_FAILURE)``; a sequence gives the n-th failing persist its
    n-th exception (the last one repeats). The first *fail_after_write* failing persists
    perform the real write before raising — the post-rename failure ``atomic_write_json`` can produce (its
    directory fsync); the other failing persists raise before writing anything.
    """
    real_write = store_module.atomic_write_json
    errors = [OSError(SIMULATED_WRITE_FAILURE)] if exc is None else [exc] if isinstance(exc, BaseException) else list(exc)
    counter = WriteCounter()

    def _write(path: Path, data: Any) -> None:
        counter.attempts += 1
        counter.payloads.append(data)
        if count is not None and counter.attempts > count:
            return real_write(path, data)
        if counter.attempts <= fail_after_write:
            real_write(path, data)
        raise errors[min(counter.attempts, len(errors)) - 1]

    with patch.object(store_module, "atomic_write_json", _write):
        yield counter


@contextmanager
def recording_job_writes() -> Iterator[WriteCounter]:
    """A pure write spy: every ``JobStore`` persist succeeds and is recorded."""
    with failing_job_writes(count=0) as counter:
        yield counter


def assert_error_logged_with_traceback(caplog: pytest.LogCaptureFixture, logger_name: str) -> None:
    """*logger_name* logged an ERROR carrying ``exc_info``: a sanitized failure answer still leaves the cause in the log."""
    errors = [r for r in caplog.records if r.name == logger_name and r.levelno == logging.ERROR]
    assert errors, f"no ERROR record from {logger_name}"
    assert any(r.exc_info for r in errors), f"no ERROR record from {logger_name} carries a traceback"
