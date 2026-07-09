"""E2g BE-11 — e2e: server starts with graph.enabled=True and [code] extras missing.

Scenario: S9 — Given graph.enabled=true but [code] extras (tree-sitter parsers) are
missing, When the server starts, Then it starts successfully, prose graphing works,
code graphing is skipped, and a one-time WARNING plus a health/status field name
the fix.

This is a TestClient-based e2e test exercising the full application stack:
- graph enabled in config
- real LanceDB store + GraphStore
- stubbed spaCy returning a named entity for any text (prose graphing)
- tree-sitter's Python grammar simulated absent (code graphing degrades softly)
- ingest of both a code file and a prose file
- GET /status reflects the degraded `code_parsers` field
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy package returning one named entity for any text.

    Must be installed before make_real_app because create_app calls
    _check_graph_deps which does ``import spacy``.
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = [_FakeEnt("Alice", "PERSON")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def test_serverStarts_whenCodeParsersMissing_graphEnabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server starts, /status reports the degraded code_parsers field, and prose
    graphing still works — even with graph.enabled=True and tree-sitter's Python
    grammar unavailable.
    """
    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from any other test/process
    # activity on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    # Simulate tree-sitter's Python grammar package being absent (established
    # stub pattern — see test_grammar_warning_logged_once in test_code_enricher.py).
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

    _install_spacy_stub(monkeypatch)

    col = "be11-code-parsers-missing"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )
    prose_doc = tmp_path / "prose.txt"
    prose_doc.write_text(
        "Alice is a software engineer.\n" * 10,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Server started; /health confirms it before any ingest.
        health = client.get("/health")
        assert health.status_code == 200, f"/health failed: {health.status_code} {health.text}"

        # Ingest the code file — this trips CodeEnricher.prepare(".py") →
        # _get_grammar(".py") → grammar missing → WARNING logged, ingest still
        # completes (code graphing is skipped, not a hard failure).
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        # Ingest a plain prose file — proves prose graphing still works via the
        # spaCy stub, independent of the code-parser degradation.
        ingest_file_via_path(client, col, str(prose_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is True
        assert ".py" in code_parsers["missing_extensions"]
        assert code_parsers["message"], "message should name the fix when degraded"

        # Prose graphing still works: the ingested collection has a graph entry
        # with node_count > 0 from the spaCy-stub-extracted entity.
        assert "graph" in data
        graph = data["graph"]
        assert graph is not None
        col_entries = [c for c in graph["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r} in graph.collections, "
            f"got: {graph['collections']}"
        )
        assert col_entries[0]["node_count"] > 0, (
            "Expected node_count > 0 from prose graphing (spaCy stub entity), "
            f"got: {col_entries[0]['node_count']}"
        )


def test_codeParsersStatus_isNull_whenGraphDisabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=False → /status.code_parsers is null, even with a missing grammar (C1-T-1).

    The code_parsers soft-degrade check is only meaningful alongside code
    graphing; it must not report anything when graph is off, regardless of
    whether tree-sitter grammars are actually missing.
    """
    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from other tests/process activity
    # on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    # Simulate tree-sitter's Python grammar package being absent.
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

    col = "be11-graph-disabled"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text("def foo():\n    return 1\n", encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        # Trip the missing-grammar branch directly to prove even a "dirty" registry
        # state does not leak into the response when graph is disabled.
        ce._get_grammar(".py")
        assert ce.has_missing_code_parsers() is True

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        assert data["code_parsers"] is None, (
            f"code_parsers should be null when graph.enabled=False, got: {data['code_parsers']}"
        )


def test_codeParsersStatus_healthyPath_notDegraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=True and no missing-grammar event → /status reports a healthy code_parsers (C1-T-2a).

    Also directly asserts has_missing_code_parsers() is False on fresh/isolated
    state, matching the isolation pattern used in
    tests/test_code_enricher.py's TestGrammarRegistry fixture.
    """
    import archon_search.code_enricher as ce

    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    assert ce.has_missing_code_parsers() is False

    _install_spacy_stub(monkeypatch)

    col = "be11-healthy"
    prose_doc = tmp_path / "prose.txt"
    prose_doc.write_text("Alice is a software engineer.\n" * 10, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(prose_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is False
        assert code_parsers["missing_extensions"] == []
        assert not code_parsers["message"]


def test_codeParsersStatus_wizardSuccessCase_notDegraded_withRealCodeParser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wizard's intended end-state (both extras present) doesn't hit the degraded path (C1-A-5).

    Proves the missing half of the happy-path story: `test_codeParsersStatus_healthyPath_notDegraded`
    only ingests prose and never exercises the tree-sitter grammar at all, so it
    can't distinguish "healthy because untested" from "healthy because the grammar
    genuinely parses". This test ingests a REAL code file with tree-sitter's
    Python grammar genuinely available (not stubbed absent) — the state a guided
    wizard install (BE-11 `graph.enabled=true` + `[code]`+`[graph]` extras both
    installed) is meant to reach — and asserts `/status`'s `code_parsers` stays
    healthy (`degraded=False`).

    Skips gracefully when tree-sitter-python isn't installed locally (optional
    `[code]` extra; CI's bare `uv sync --dev` never installs it — see
    tests/integration/test_http_enrichment_metadata.py for the same guard).
    """
    try:
        import tree_sitter_python  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("tree-sitter-python not installed — code enrichment skipped")

    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from other tests/process activity
    # on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    _install_spacy_stub(monkeypatch)

    col = "be11-wizard-success"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Real tree-sitter grammar in use — no absent-package simulation.
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is False, (
            "wizard success state (both extras present, graph enabled) must not "
            f"report a degraded code_parsers status, got: {code_parsers}"
        )
        assert code_parsers["missing_extensions"] == []
