"""A sentinel whose rename is published but not confirmed durable was written: the log must not say it was not."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from archon_search import sync_suppression
from archon_search._durable_io import DurabilityUnconfirmedError


def test_an_unconfirmed_rename_durability_is_not_reported_as_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    def written_but_unconfirmed(path: Path, data: object) -> None:
        path.write_text("{}")
        raise DurabilityUnconfirmedError(5, "Input/output error")

    monkeypatch.setattr(sync_suppression, "atomic_write_json", written_but_unconfirmed)

    with caplog.at_level(logging.WARNING, logger=sync_suppression.logger.name):
        sync_suppression.write_sync_suppressed_sentinel()

    messages = [record.getMessage() for record in caplog.records]
    assert not any("could not write" in message for message in messages)
    assert any("durability is unconfirmed" in message for message in messages)
