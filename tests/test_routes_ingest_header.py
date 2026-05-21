"""Tests for the ``X-Ingested-By`` header normalization helper.

Implements Task 3.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.

These pin the boundary contract directly on the helper rather than via a
TestClient round-trip — the route wiring is exercised by other route tests,
and the contract that matters for A1 is the normalization rules themselves.
"""
from __future__ import annotations

import logging

import pytest

from archon_search.constants import INGESTED_BY_VALUES
from archon_search.server._ingested_by import parse_ingested_by_header


def test_x_ingested_by_default_http() -> None:
    assert parse_ingested_by_header(None) == "http"
    assert parse_ingested_by_header("") == "http"


def test_x_ingested_by_legacy_normalized_to_cli() -> None:
    assert parse_ingested_by_header("archon-search-cli") == "cli"


def test_x_ingested_by_unknown_coerced_to_http(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="archon")
    assert parse_ingested_by_header("rogue-script") == "http"
    assert "rogue-script" in caplog.text
    assert "unknown X-Ingested-By" in caplog.text


def test_x_ingested_by_unknown_value_truncated_in_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="archon")
    long_value = "a" * 200
    assert parse_ingested_by_header(long_value) == "http"
    # The truncated value (32 chars) must appear; the full 200-char string must NOT.
    assert ("a" * 32) in caplog.text
    assert ("a" * 33) not in caplog.text


@pytest.mark.parametrize("value", INGESTED_BY_VALUES)
def test_x_ingested_by_accepts_each_known_value(value: str) -> None:
    assert parse_ingested_by_header(value) == value
