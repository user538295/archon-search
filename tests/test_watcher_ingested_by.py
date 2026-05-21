"""Pin that ``SearchCollectionSync`` passes ``ingested_by="watcher"`` to the
pipeline ingest call sites.

Implements the watcher-side wiring of Task 3.3
(Documentation/Backlog/A1-metadata-schema-v1-plan.md).

The plan asks for an end-to-end watcher re-ingest test. Driving watchdog from
a unit test is flaky on macOS/Linux CI; instead this file pins the contract
at the seam where the watcher/sync layer calls into the pipeline: every
ingest call site in ``archon_search/sync.py`` must thread the literal
``ingested_by="watcher"``. Task 7.1 (watcher-replace integration pin) will
verify the end-to-end propagation against a real store.
"""
from __future__ import annotations

import re
from pathlib import Path


_SYNC_PATH = Path(__file__).resolve().parents[1] / "archon_search" / "sync.py"


def test_sync_ingest_file_callsite_passes_watcher() -> None:
    src = _SYNC_PATH.read_text()
    # Every ``self._pipeline.ingest_file(...)`` call in sync.py must include
    # the ``ingested_by="watcher"`` kwarg.
    calls = re.findall(r"self\._pipeline\.ingest_file\([^)]*\)", src)
    assert calls, "expected ingest_file call sites in sync.py"
    for call in calls:
        assert 'ingested_by="watcher"' in call, (
            f"sync.py ingest_file call missing ingested_by='watcher': {call!r}"
        )


def test_sync_ingest_directory_callsite_passes_watcher() -> None:
    src = _SYNC_PATH.read_text()
    # Match across newlines — ingest_directory is called with multi-line args.
    match = re.search(
        r"self\._pipeline\.ingest_directory\((?:[^()]|\([^()]*\))*\)",
        src,
        re.DOTALL,
    )
    assert match is not None, "expected ingest_directory call site in sync.py"
    assert 'ingested_by="watcher"' in match.group(0), (
        f"sync.py ingest_directory call missing ingested_by='watcher': {match.group(0)!r}"
    )
