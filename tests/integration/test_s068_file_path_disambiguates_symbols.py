"""S68 regression: GET /graph/{col}/impact/{symbol}?file_path=... must actually
disambiguate same-named code symbols in different files.

Bug (S68-file_path_query_param_is_ignored): the ``file_path`` query parameter was
ignored in practice. Two files each defining ``helper`` produce two distinct
file-qualified ``code_symbol`` nodes, but a ``?file_path=helpers_a.py`` request
could not select between them — the resolver only matched a node whose hashed ID
was rebuilt from the *exact* ingest-time source path (an absolute path), so a
basename never matched and resolution silently fell back to an arbitrary node.

These tests ingest two real ``.py`` files through the real ingest pipeline (real
tree-sitter def/ref extraction) and assert the basename ``file_path`` resolves to
the correct definition's callers, and that a path matching no definition returns an
empty result rather than another symbol's blast radius.

Either member of the ``helpers_a`` / ``helpers_b`` pair failing is the SAME defect
(the resolver picking the wrong node). Do not "fix" one by relaxing its assertion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import (
    ingest_file_via_path,
    install_spacy_stub,
    make_real_app,
)

pytestmark = [pytest.mark.integration]

_HELPERS_A = '''\
def helper():
    return "a"


def caller_a():
    return helper()
'''

_HELPERS_B = '''\
def helper():
    return "b"


def caller_b():
    return helper()
'''


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _direct_caller_names(client, collection: str, api_key: str, file_path: str) -> set[str]:
    resp = client.get(
        f"/graph/{collection}/impact/helper",
        params={"direction": "callers", "file_path": file_path},
        headers=_auth(api_key),
    )
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    return {e["entity_name"] for e in resp.json()["callers"]["direct"]}


def _ingest_dup_corpus(tmp_path: Path, monkeypatch):
    """Yield (cm, client, api_key, collection) with the two-file dup corpus ingested."""
    install_spacy_stub(monkeypatch)
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    (code_dir / "helpers_a.py").write_text(_HELPERS_A, encoding="utf-8")
    (code_dir / "helpers_b.py").write_text(_HELPERS_B, encoding="utf-8")

    cm = make_real_app(tmp_path, monkeypatch, graph_enabled=True)
    client, cfg, api_key = cm.__enter__()
    collection = "code-dup"
    ingest_file_via_path(client, collection, str(code_dir / "helpers_a.py"), api_key=api_key)
    ingest_file_via_path(client, collection, str(code_dir / "helpers_b.py"), api_key=api_key)
    return cm, client, api_key, collection


def test_helpers_a_resolves_caller_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cm, client, api_key, collection = _ingest_dup_corpus(tmp_path, monkeypatch)
    try:
        names = _direct_caller_names(client, collection, api_key, "helpers_a.py")
        assert "caller_a" in names, f"file_path=helpers_a.py should resolve caller_a; got {names}"
        assert "caller_b" not in names, f"file_path=helpers_a.py leaked helpers_b's callers: {names}"
    finally:
        cm.__exit__(None, None, None)


def test_helpers_b_resolves_caller_b(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cm, client, api_key, collection = _ingest_dup_corpus(tmp_path, monkeypatch)
    try:
        names = _direct_caller_names(client, collection, api_key, "helpers_b.py")
        assert "caller_b" in names, f"file_path=helpers_b.py should resolve caller_b; got {names}"
        assert "caller_a" not in names, f"file_path=helpers_b.py leaked helpers_a's callers: {names}"
    finally:
        cm.__exit__(None, None, None)


def test_nonexistent_file_path_returns_empty_not_wrong_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cm, client, api_key, collection = _ingest_dup_corpus(tmp_path, monkeypatch)
    try:
        names = _direct_caller_names(client, collection, api_key, "nope_does_not_exist.py")
        assert names == set(), (
            "a file_path matching no definition must not return another file's "
            f"blast radius; got {names}"
        )
    finally:
        cm.__exit__(None, None, None)
