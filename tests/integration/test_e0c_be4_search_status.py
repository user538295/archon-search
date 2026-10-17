"""E0c / BE-4 — SearchStatusDetail model and GET /status search sub-object.

Tests that:
- GET /status returns ``search: { max_fanout, top_k_max, context_window_max }``: the first two from
  config, ``context_window_max`` from the served pipeline (set from config)
- Defaults are max_fanout=8, top_k_max=100, context_window_max=10 when no TOML overrides present

Scenarios covered: S10, S13
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_CONTEXT_WINDOW_MAX
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
    """No TOML overrides → defaults: max_fanout=8, top_k_max=100, context_window_max=10."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "search" in data, f"'search' key missing from status response: {list(data.keys())}"
        search = data["search"]
        assert search is not None, "search sub-object is None with defaults"
        assert search["max_fanout"] == 8, f"Expected default max_fanout=8, got {search['max_fanout']}"
        assert search["top_k_max"] == 100, f"Expected default top_k_max=100, got {search['top_k_max']}"
        assert search["context_window_max"] == DEFAULT_CONTEXT_WINDOW_MAX, (
            f"Expected default context_window_max={DEFAULT_CONTEXT_WINDOW_MAX}, "
            f"got {search['context_window_max']}"
        )


def test_status_search_context_window_max_matches_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOML [search].context_window_max = 4 → status.search.context_window_max == 4."""
    toml_content = "[search]\ncontext_window_max = 4\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        assert resp.json()["search"]["context_window_max"] == 4


def test_status_search_context_window_max_reports_served_pipeline_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/status reports the bound the served pipeline enforces, not a separate config read."""
    pipeline_bound = DEFAULT_CONTEXT_WINDOW_MAX + 3
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        monkeypatch.setattr(client.app.state.pipeline, "_context_window_max", pipeline_bound)
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        assert cfg.context_window_max == DEFAULT_CONTEXT_WINDOW_MAX
        assert resp.json()["search"]["context_window_max"] == pipeline_bound
