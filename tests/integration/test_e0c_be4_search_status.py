"""E0c / BE-4 — SearchStatusDetail model and GET /status search sub-object.

Tests that:
- GET /status returns ``search: { max_fanout, top_k_max }`` reflecting config
- Defaults are max_fanout=8, top_k_max=100 when no TOML overrides present

Scenarios covered: S10, S13
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_status_search_max_fanout_matches_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config max_fanout=12 → status.search.max_fanout == 12 (S10)."""
    with make_real_app(tmp_path, monkeypatch, max_fanout=12) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        assert data["search"] is not None, "search sub-object is None"
        assert data["search"]["max_fanout"] == 12, (
            f"Expected max_fanout=12, got {data['search']['max_fanout']}"
        )
        assert data["search"]["top_k_max"] == 100, (
            f"Expected default top_k_max=100, got {data['search']['top_k_max']}"
        )


def test_status_search_top_k_max_matches_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config top_k_max=200 → status.search.top_k_max == 200 (S13)."""
    with make_real_app(tmp_path, monkeypatch, top_k_max=200) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        assert data["search"] is not None, "search sub-object is None"
        assert data["search"]["top_k_max"] == 200, (
            f"Expected top_k_max=200, got {data['search']['top_k_max']}"
        )
        assert data["search"]["max_fanout"] == 8, (
            f"Expected default max_fanout=8, got {data['search']['max_fanout']}"
        )


def test_status_search_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No TOML overrides → defaults: max_fanout=8, top_k_max=100."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        search = data["search"]
        assert search is not None, "search sub-object is None with defaults"
        assert search["max_fanout"] == 8, f"Expected default max_fanout=8, got {search['max_fanout']}"
        assert search["top_k_max"] == 100, f"Expected default top_k_max=100, got {search['top_k_max']}"
