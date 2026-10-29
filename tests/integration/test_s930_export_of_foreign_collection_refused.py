"""S930: export of, and import over, another namespace's collection is refused with the fixed ownership text.

SecurityGuide/03_authorization_and_acl.md:15 and :110 — a namespaced client cannot import over or export another
namespace's collection, admin key included; the refusal is always ``collection name is not available to this
namespace`` (``store.COLLECTION_UNAVAILABLE_DETAIL``, :33), in the HTTP body or the failed job's ``error`` (the docs
pin no status code). Drives the real ASGI app and store.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.jobs.export_archive import exports_dir
from archon_search.paths import get_data_dir
from archon_search.server.routes_jobs import _TERMINAL_STATUSES
from archon_search.store import COLLECTION_UNAVAILABLE_DETAIL
from tests.integration.conftest import ingest_doc, make_real_app, wait_for_job_done

pytestmark = pytest.mark.integration

_NS_A = "team-a"
_NS_B = "team-b"
_NS_ADMIN = "default"
_FOREIGN = "owned-a"
_A_TEXT = "alpha roadmap: ship the quarterly launch plan"
_JOB_TIMEOUT_S = 30.0
_POLL_S = 0.05
_ACCEPTED = 202


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, str]]]:
    """Real app: ``team-a`` owns ``owned-a`` (1 chunk); ``team-b`` and the admin key each own a collection of their own."""
    key_a, key_b = secrets.token_hex(32), secrets.token_hex(32)
    with make_real_app(tmp_path, monkeypatch, namespaces={key_a: _NS_A, key_b: _NS_B}) as (client, _cfg, admin_key):
        keys = {"a": key_a, "b": key_b, "admin": admin_key}
        ingest_doc(client, _FOREIGN, _A_TEXT, "/notes/a/plan.md", api_key=key_a)
        ingest_doc(client, "b-own", "bravo own document", "/notes/b/own.md", api_key=key_b)
        ingest_doc(client, "admin-own", "admin own document", "/notes/admin/own.md", api_key=admin_key)
        yield client, keys


def _own_archive(client: TestClient, key: str, collection: str) -> Path:
    response = client.post(f"/collections/{collection}/export", json={}, headers=_bearer(key))
    assert response.status_code == _ACCEPTED, response.text
    job = wait_for_job_done(client, response.json()["job_id"], what="export")
    return Path(job.result["archive_path"])


def _refusal_text(client: TestClient, response) -> str:
    """The refusal text of *response*: the HTTP body's ``detail`` for a 4xx, the failed job's ``error`` for a 202."""
    if response.status_code != _ACCEPTED:
        assert 400 <= response.status_code < 500, response.text
        return response.json().get("detail", "")
    store = client.app.state.job_store
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job = store.get(response.json()["job_id"])
        if job is not None and job.status in _TERMINAL_STATUSES:
            assert job.status.value == "FAILED", f"a foreign collection must not {job.status}: {job.result}"
            return job.error or ""
        time.sleep(_POLL_S)
    pytest.fail("job did not finish")


def _assert_nothing_written(client: TestClient, keys: dict[str, str]) -> None:
    for namespace in (_NS_B, _NS_ADMIN):
        folder = exports_dir(get_data_dir(), namespace)
        assert not list(folder.glob(f"{_FOREIGN}-*.tar.gz")) if folder.exists() else True, namespace
    detail = client.get(f"/collections/{_FOREIGN}", headers=_bearer(keys["a"]))
    assert detail.status_code == 200
    assert detail.json()["chunk_count"] == 1


@pytest.mark.parametrize("who", ["b", "admin"])
def test_export_of_another_namespaces_collection_is_refused_with_the_ownership_text(world, who: str) -> None:
    client, keys = world

    response = client.post(f"/collections/{_FOREIGN}/export", json={}, headers=_bearer(keys[who]))

    assert _refusal_text(client, response) == COLLECTION_UNAVAILABLE_DETAIL, (response.status_code, response.text)
    _assert_nothing_written(client, keys)


@pytest.mark.parametrize(("who", "own"), [("b", "b-own"), ("admin", "admin-own")])
def test_import_over_another_namespaces_collection_is_refused_with_the_ownership_text(
    world, who: str, own: str
) -> None:
    client, keys = world
    archive = _own_archive(client, keys[who], own)

    response = client.post(
        f"/collections/{_FOREIGN}/import", json={"path": str(archive)}, headers=_bearer(keys[who])
    )

    assert _refusal_text(client, response) == COLLECTION_UNAVAILABLE_DETAIL, (response.status_code, response.text)
    _assert_nothing_written(client, keys)
