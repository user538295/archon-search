"""``--dist=loadgroup`` keeps the tests of one ``xdist_group`` on one worker.

Tests that share a resource (the install lock, the benchmark group, a real model stack) rely on it. pytest 9.2 made the
node ID read-only, and pytest-xdist 3.8.0 passes the group to its scheduler by writing it into the node ID, so under
pytest 9.2 every group is dropped without a word and each test is scheduled on its own (pytest-xdist #1386). These
tests fail when a group is ignored.
"""

from __future__ import annotations

import os

import pytest

_GROUP = "loadgroup_canary"
_TESTS = 16  # twice the default worker count: an ignored group is spread over several workers


@pytest.mark.xdist_group(_GROUP)
@pytest.mark.parametrize("position", range(_TESTS))
def test_the_tests_of_one_group_run_on_one_worker(
    position: int, request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    # ``loadgroup`` is the option pytest-xdist sets on a worker's config when the run uses ``--dist=loadgroup``
    # (``xdist/remote.py`` ``setup_config``); the worker's own ``dist`` option is always "no".
    if worker is None or not request.config.getoption("loadgroup", default=False):
        pytest.skip("only xdist under --dist=loadgroup schedules by group")
    # Every worker's base temp sits in the one directory of this session.
    record = tmp_path_factory.getbasetemp().parent / f"{_GROUP}.workers"
    with record.open("a", encoding="utf-8") as appended:
        appended.write(f"{position} {worker}\n")
    workers = {line.split()[1] for line in record.read_text(encoding="utf-8").splitlines()}
    assert workers == {worker}, f"the {_GROUP!r} group ran on {sorted(workers)}"
