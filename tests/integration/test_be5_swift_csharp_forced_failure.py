"""E2g BE-5 — Swift/C# real-edge extraction and forced-failure isolation.

Swift and C# are new tree-sitter grammars (per Q7, "may slip to fast-follow");
these tests are marked ``integration`` per the plan's ``#integration_test``
tag and live here rather than in ``tests/test_defref_extractor.py``:

- test_swiftGrammar_installs_producesRealEdges / test_cSharpGrammar_installs_producesRealEdges:
  if the grammar is genuinely installed, a sample file produces REAL
  calls/imports/defines/inherits edges (not just a clean parse) — skips
  gracefully via ``pytest.skip`` if the grammar package is absent, mirroring
  ``tests/test_be0_swift_csharp_spike.py``'s guard.
- test_swiftGrammar_forcedFailure_excludesLanguageOnly / test_cSharpGrammar_forcedFailure_excludesLanguageOnly:
  monkeypatches ``DefRefExtractor``'s grammar lookup so ONLY the target
  language's grammar is forced unavailable, and asserts (a) that language
  degrades to an empty result + warning (mirroring
  ``test_missingGrammar_returnsEmptyResultWithWarning`` in the unit suite),
  and (b) a different, always-available language (Python) still extracts
  real edges in the SAME test run — proving the failure is isolated to one
  language, not a global regression. This does not require the real Swift/C#
  grammar to be installed at all: the failure is simulated by monkeypatching
  ``_get_grammar`` directly.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

import archon_search.defref_extractor as defref_extractor_module  # noqa: E402
from archon_search.defref_extractor import DefRefExtractor  # noqa: E402
from archon_search.graph_types import RelationshipType  # noqa: E402

pytestmark = pytest.mark.integration


class _FakeGraphStore:
    """No cross-file candidates — same-file-only extraction, mirroring the unit suite."""

    async def find_nodes_by_name(self, collection: str, names: list[str], ns: str) -> list:
        del collection, names, ns
        return []


def _extract(file_text: str, file_path: str) -> object:
    extractor = DefRefExtractor(graph_store=_FakeGraphStore())  # type: ignore[arg-type]

    async def _run():
        return await extractor.extract(
            file_text=file_text,
            file_path=file_path,
            doc_id="doc-1",
            collection="col",
            ns="default",
        )

    return asyncio.run(_run())


def _edges_of_type(result, rel: RelationshipType) -> list:
    return [e for e in result.edges if e.relationship_type == rel]


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------

_SWIFT_SAMPLE = (
    "import Foundation\n\n"
    "class Base {\n"
    "}\n\n"
    "class Foo: Base {\n"
    "    func bar() -> Int {\n"
    "        return baz()\n"
    "    }\n\n"
    "    func baz() -> Int {\n"
    "        return 1\n"
    "    }\n"
    "}\n"
)


def test_swiftGrammar_installs_producesRealEdges() -> None:
    try:
        import tree_sitter_swift  # noqa: F401
    except ImportError:
        pytest.skip("tree-sitter-swift not installed (requires the 'code' extra)")

    result = _extract(_SWIFT_SAMPLE, "/repo/Foo.swift")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected 1 calls edge, got {len(calls)}"

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected 1 imports edge, got {len(imports)}"

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected 1 inherits edge, got {len(inherits)}"

    defines = _edges_of_type(result, RelationshipType.defines)
    assert len(defines) >= 3, f"Expected at least 3 defines edges, got {len(defines)}"

    names = {n.entity_name for n in result.nodes}
    assert {"Base", "Foo", "bar", "baz", "Foundation"}.issubset(names)


_SWIFT_MEMBER_CALL_SAMPLE = (
    "class Foo {\n"
    "    func bar() -> Int {\n"
    "        return self.baz()\n"
    "    }\n\n"
    "    func baz() -> Int {\n"
    "        return 1\n"
    "    }\n"
    "}\n"
)


def test_swiftGrammar_memberCall_producesCallsEdge() -> None:
    """A `self.baz()` navigation call must resolve the same as a bare `baz()` call.

    Regression test: the call_expression walker originally only handled a
    direct `simple_identifier` as the callee (bare calls); a receiver-qualified
    call like `self.baz()` parses to a `navigation_expression` wrapping a
    `navigation_suffix` that holds the callee name, which the bare-call check
    silently missed, dropping the edge instead of raising.
    """
    try:
        import tree_sitter_swift  # noqa: F401
    except ImportError:
        pytest.skip("tree-sitter-swift not installed (requires the 'code' extra)")

    result = _extract(_SWIFT_MEMBER_CALL_SAMPLE, "/repo/Foo.swift")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected 1 calls edge for a self.baz() member call, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"


def test_swiftGrammar_forcedFailure_excludesLanguageOnly(monkeypatch) -> None:
    real_get_grammar = defref_extractor_module._get_grammar

    def _forced(ext: str):
        if ext == ".swift":
            return None
        return real_get_grammar(ext)

    monkeypatch.setattr(defref_extractor_module, "_get_grammar", _forced)

    swift_result = _extract(_SWIFT_SAMPLE, "/repo/Foo.swift")
    assert swift_result.nodes == []
    assert swift_result.edges == []
    assert swift_result.warnings, "Expected a non-empty warnings list for a forced-missing grammar"
    assert swift_result.fatal_error is None

    # A different language, in the SAME test run, must still extract real edges.
    python_result = _extract("def bar():\n    pass\n\ndef foo():\n    return bar()\n", "/repo/mod.py")
    python_calls = _edges_of_type(python_result, RelationshipType.calls)
    assert len(python_calls) == 1, "Python extraction must be unaffected by Swift's forced failure"


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------

_CSHARP_SAMPLE = (
    "using System;\n\n"
    "class Base {\n"
    "}\n\n"
    "class Foo : Base {\n"
    "    int Bar() {\n"
    "        return Baz();\n"
    "    }\n\n"
    "    int Baz() {\n"
    "        return 1;\n"
    "    }\n"
    "}\n"
)


def test_cSharpGrammar_installs_producesRealEdges() -> None:
    try:
        import tree_sitter_c_sharp  # noqa: F401
    except ImportError:
        pytest.skip("tree-sitter-c-sharp not installed (requires the 'code' extra)")

    result = _extract(_CSHARP_SAMPLE, "/repo/Foo.cs")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected 1 calls edge, got {len(calls)}"

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected 1 imports edge, got {len(imports)}"

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected 1 inherits edge, got {len(inherits)}"

    defines = _edges_of_type(result, RelationshipType.defines)
    assert len(defines) >= 3, f"Expected at least 3 defines edges, got {len(defines)}"

    names = {n.entity_name for n in result.nodes}
    assert {"Base", "Foo", "Bar", "Baz", "System"}.issubset(names)


def test_cSharpGrammar_forcedFailure_excludesLanguageOnly(monkeypatch) -> None:
    real_get_grammar = defref_extractor_module._get_grammar

    def _forced(ext: str):
        if ext == ".cs":
            return None
        return real_get_grammar(ext)

    monkeypatch.setattr(defref_extractor_module, "_get_grammar", _forced)

    csharp_result = _extract(_CSHARP_SAMPLE, "/repo/Foo.cs")
    assert csharp_result.nodes == []
    assert csharp_result.edges == []
    assert csharp_result.warnings, "Expected a non-empty warnings list for a forced-missing grammar"
    assert csharp_result.fatal_error is None

    # A different language, in the SAME test run, must still extract real edges.
    python_result = _extract("def bar():\n    pass\n\ndef foo():\n    return bar()\n", "/repo/mod.py")
    python_calls = _edges_of_type(python_result, RelationshipType.calls)
    assert len(python_calls) == 1, "Python extraction must be unaffected by C#'s forced failure"
