"""E0c / T-1 — E2e tests: TOML config flows end-to-end into GET /status search.*.

Tests that the full TOML loading path (write TOML → load_config → create app → GET /status)
correctly surfaces max_fanout and top_k_max in the search sub-object.

This is distinct from BE-4 tests which set config programmatically. These tests verify
the TOML loading path end-to-end (scenarios S10, S13 from the e2e level).

Scenarios covered: S10, S13
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import SearchConfig
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# S10: max_fanout in TOML → GET /status search.max_fanout reflects it
# ---------------------------------------------------------------------------


def test_e2e_status_search_max_fanout_reflects_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write TOML with max_fanout=12, assert GET /status search.max_fanout == 12 (S10).

    Exercises the full TOML loading path:
    write TOML → load_config(path) → create_app → GET /status.
    """
    toml_content = "[search]\nmax_fanout = 12\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        # Sanity-check that load_config picked up the TOML value.
        assert cfg.max_fanout == 12, (
            f"load_config must set max_fanout=12 from TOML, got {cfg.max_fanout}"
        )

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        search = data["search"]
        assert search is not None, "status.search must not be null"
        assert search["max_fanout"] == 12, (
            f"Expected search.max_fanout=12 from TOML config, got {search['max_fanout']}"
        )
        # top_k_max should be the SearchConfig default since TOML does not override it.
        assert search["top_k_max"] == SearchConfig().top_k_max, (
            f"Expected default search.top_k_max={SearchConfig().top_k_max}, got {search['top_k_max']}"
        )


# ---------------------------------------------------------------------------
# S13: top_k_max in TOML → GET /status search.top_k_max reflects it
# ---------------------------------------------------------------------------


def test_e2e_status_search_top_k_max_reflects_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write TOML with top_k_max=300, assert GET /status search.top_k_max == 300 (S13).

    Exercises the full TOML loading path:
    write TOML → load_config(path) → create_app → GET /status.
    """
    toml_content = "[search]\ntop_k_max = 300\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        # Sanity-check that load_config picked up the TOML value.
        assert cfg.top_k_max == 300, (
            f"load_config must set top_k_max=300 from TOML, got {cfg.top_k_max}"
        )

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        search = data["search"]
        assert search is not None, "status.search must not be null"
        assert search["top_k_max"] == 300, (
            f"Expected search.top_k_max=300 from TOML config, got {search['top_k_max']}"
        )
        # max_fanout should be the SearchConfig default since TOML does not override it.
        assert search["max_fanout"] == SearchConfig().max_fanout, (
            f"Expected default search.max_fanout={SearchConfig().max_fanout}, got {search['max_fanout']}"
        )


# ---------------------------------------------------------------------------
# Both fields in same TOML → both surface in GET /status
# ---------------------------------------------------------------------------


def test_e2e_status_search_both_fields_reflect_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both max_fanout and top_k_max in same TOML → both surface in GET /status."""
    toml_content = "[search]\nmax_fanout = 4\ntop_k_max = 50\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        assert cfg.max_fanout == 4
        assert cfg.top_k_max == 50
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200
        search = resp.json()["search"]
        assert search is not None
        assert search["max_fanout"] == 4
        assert search["top_k_max"] == 50
