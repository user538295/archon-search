"""S750: an unsupported embedding/reranker model must fail the models check, not abort startup.

Documented (UserManual/30_configuration.md, OperatorGuide/20_monitoring_and_alerts.md:51):
the process keeps running, /health is 200, /ready is 503 with checks.models == "fail", and the
log carries ``unsupported model '<name>'; supported: ...``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests.test_app import wait_for_models_check_to_settle

_BAD_EMBEDDER = "nonexistent-org/no-such-embedder-s750"
_BAD_RERANKER = "nonexistent-org/no-such-reranker-s750"


def _config(tmp_path: Path, **overrides: str) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.mark.parametrize(
    ("field", "bad_name"),
    [("embedding_model", _BAD_EMBEDDER), ("reranker_model", _BAD_RERANKER)],
)
def test_unsupported_model_keeps_server_up_with_models_fail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    field: str,
    bad_name: str,
) -> None:
    cfg = _config(tmp_path, **{field: bad_name})
    caplog.set_level(logging.INFO)

    # Bug: create_app() itself raises UnsupportedModelError (eager ModelEmbedder/ModelReranker).
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        wait_for_models_check_to_settle(app)
        ready = client.get("/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert body["checks"]["models"] == "fail"

    assert f"unsupported model '{bad_name}'; supported: " in caplog.text


def _traceback_depth(exc: BaseException) -> int:
    depth, tb = 0, exc.__traceback__
    while tb is not None:
        depth, tb = depth + 1, tb.tb_next
    return depth


def test_unsupported_backends_raise_a_fresh_error_each_call() -> None:
    """A re-raised shared exception would grow its traceback (and pin frames) on every call."""
    from archon_search.embedder import UnsupportedModelEmbedder
    from archon_search.model_specs import UnsupportedModelError
    from archon_search.reranker import UnsupportedModelReranker

    error = UnsupportedModelError("bad/x", ("a", "b"))
    embedder = UnsupportedModelEmbedder(error)
    reranker = UnsupportedModelReranker(error)
    for call in (lambda: embedder.encode(["x"]), lambda: reranker.predict([("q", "d")])):
        depths = []
        for _ in range(3):
            with pytest.raises(UnsupportedModelError, match="unsupported model 'bad/x'; supported: a, b") as info:
                call()
            depths.append(_traceback_depth(info.value))
        assert len(set(depths)) == 1


def test_add_collection_with_unsupported_default_model_is_422_and_leaves_no_stub(tmp_path: Path) -> None:
    from tests.conftest import TEST_API_KEY

    cfg = _config(tmp_path, embedding_model=_BAD_EMBEDDER)
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("hello world")

    with TestClient(app) as client:
        response = client.post(
            "/collections/", json={"path": str(docs)}, headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )

    assert response.status_code == 422
    assert f"unsupported model '{_BAD_EMBEDDER}'; supported: " in response.json()["detail"]
    assert cfg.collections == []
