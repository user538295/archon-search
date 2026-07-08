"""Unit tests for DefRefExtractor — E2g BE-2.

Tests cover the BE-2 Tests block scenarios (S1, S1b, S2, S3, S8):
- test_sameFileCall_producesCallsEdge — S1
- test_sameFileDefinition_producesDefinesEdge — S1b
- test_explicitImport_producesImportsEdge — S2
- test_sameFileInheritance_producesInheritsEdge — S3
- test_typeScriptSameFileCall_producesCallsEdge — S8 (calls)
- test_typeScriptImportAndInherits_produceEdges — S8 (imports + inherits, not calls-only)
- test_sameNameDifferentFiles_produceDistinctNodes — Critical #2/#3 groundwork for S4b

The real graph store round-trip test (test_defrefExtractor_writesEdgesToGraphStore)
lives in tests/integration/test_defref_extractor_integration.py.

All tests are skipped gracefully if the tree-sitter [code] extras are absent,
following the skip-guard pattern used elsewhere for optional graph/code extras.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter_typescript")

from archon_search.defref_extractor import DefRefExtractor  # noqa: E402
from archon_search.graph_types import (  # noqa: E402
    EntityType,
    RelationshipType,
    make_stable_entity_id,
)


class _FakeGraphStore:
    """Minimal GraphStoreProtocol stand-in — BE-2's same-file scope never calls it."""


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


def _node_id(name: str, file_path: str) -> str:
    return make_stable_entity_id(EntityType.code_symbol.value, f"{name}::{file_path}")


def _module_node_id(file_path: str) -> str:
    return make_stable_entity_id(EntityType.code_symbol.value, f"<module>::{file_path}")


# ---------------------------------------------------------------------------
# S1 — same-file Python function call
# ---------------------------------------------------------------------------


def test_sameFileCall_producesCallsEdge() -> None:
    src = "def bar():\n    return 1\n\ndef foo():\n    return bar()\n"
    result = _extract(src, "/repo/mod.py")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.py")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.py")


# ---------------------------------------------------------------------------
# S1b — same-file Python function/class definition
# ---------------------------------------------------------------------------


def test_sameFileDefinition_producesDefinesEdge() -> None:
    src = "def foo():\n    pass\n"
    result = _extract(src, "/repo/mod.py")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert len(defines) == 1, f"Expected exactly 1 defines edge, got {len(defines)}"
    edge = defines[0]
    assert edge.extraction_method == "extracted"
    # module-level function → module symbol (basename/stem) defines it.
    assert edge.source_node_id == _module_node_id("/repo/mod.py")
    assert edge.target_node_id == _node_id("foo", "/repo/mod.py")

    # Deterministic: re-extracting the same file produces the exact same edge id.
    result2 = _extract(src, "/repo/mod.py")
    assert _edges_of_type(result2, RelationshipType.defines)[0].id == edge.id


def test_sameFileDefinition_methodDefinedByEnclosingClass() -> None:
    """A method's `defines` edge comes from the enclosing class, not the module."""
    src = "class Handler:\n    def process(self):\n        pass\n"
    result = _extract(src, "/repo/mod.py")

    defines = _edges_of_type(result, RelationshipType.defines)
    # class Handler defined by module; process defined by Handler.
    assert len(defines) == 2
    by_target = {e.target_node_id: e.source_node_id for e in defines}
    assert by_target[_node_id("Handler", "/repo/mod.py")] == _module_node_id("/repo/mod.py")
    assert by_target[_node_id("process", "/repo/mod.py")] == _node_id("Handler", "/repo/mod.py")


# ---------------------------------------------------------------------------
# S2 — explicit Python import statement
# ---------------------------------------------------------------------------


def test_explicitImport_producesImportsEdge() -> None:
    src = "import os\nfrom collections import defaultdict\n"
    result = _extract(src, "/repo/mod.py")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 2, f"Expected 2 imports edges, got {len(imports)}"
    for edge in imports:
        assert edge.extraction_method == "extracted"
        assert edge.source_node_id == _module_node_id("/repo/mod.py")

    target_names = {n.entity_name for n in result.nodes if n.entity_name in {"os", "defaultdict"}}
    assert target_names == {"os", "defaultdict"}


# ---------------------------------------------------------------------------
# S3 — same-file Python inheritance
# ---------------------------------------------------------------------------


def test_sameFileInheritance_producesInheritsEdge() -> None:
    src = "class Base:\n    pass\n\nclass Foo(Base):\n    pass\n"
    result = _extract(src, "/repo/mod.py")

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected exactly 1 inherits edge, got {len(inherits)}"
    edge = inherits[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("Foo", "/repo/mod.py")
    assert edge.target_node_id == _node_id("Base", "/repo/mod.py")


# ---------------------------------------------------------------------------
# S8 — TypeScript same-file calls
# ---------------------------------------------------------------------------


def test_typeScriptSameFileCall_producesCallsEdge() -> None:
    src = "function bar() {\n  return 1;\n}\n\nfunction foo() {\n  return bar();\n}\n"
    result = _extract(src, "/repo/mod.ts")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.ts")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.ts")


# ---------------------------------------------------------------------------
# S8 — TypeScript imports + inherits (not calls-only)
# ---------------------------------------------------------------------------


def test_typeScriptImportAndInherits_produceEdges() -> None:
    src = (
        'import { Base } from "./base";\n\n'
        "class Foo extends Base {\n"
        "  bar() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    result = _extract(src, "/repo/mod.ts")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected exactly 1 imports edge, got {len(imports)}"
    assert imports[0].extraction_method == "extracted"
    assert imports[0].source_node_id == _module_node_id("/repo/mod.ts")
    assert imports[0].target_node_id == _node_id("Base", "/repo/mod.ts")

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected exactly 1 inherits edge, got {len(inherits)}"
    assert inherits[0].extraction_method == "extracted"
    assert inherits[0].source_node_id == _node_id("Foo", "/repo/mod.ts")
    assert inherits[0].target_node_id == _node_id("Base", "/repo/mod.ts")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert any(
        e.source_node_id == _module_node_id("/repo/mod.ts")
        and e.target_node_id == _node_id("Foo", "/repo/mod.ts")
        for e in defines
    )
    assert any(
        e.source_node_id == _node_id("Foo", "/repo/mod.ts")
        and e.target_node_id == _node_id("bar", "/repo/mod.ts")
        for e in defines
    )


# ---------------------------------------------------------------------------
# Critical #2/#3 — file-qualified node identity, bare entity_name
# ---------------------------------------------------------------------------


def test_sameNameDifferentFiles_produceDistinctNodes() -> None:
    """Two unrelated same-named `run` functions in different files get distinct node IDs.

    ``entity_name`` stays the bare "run" in both cases — only the hashed ID differs.
    """
    src = "def run():\n    pass\n"
    result_a = _extract(src, "/repo/a.py")
    result_b = _extract(src, "/repo/b.py")

    node_a = next(n for n in result_a.nodes if n.entity_name == "run")
    node_b = next(n for n in result_b.nodes if n.entity_name == "run")

    assert node_a.id != node_b.id, "Same-named symbols in different files must get distinct IDs"
    assert node_a.entity_name == "run"
    assert node_b.entity_name == "run"
    assert node_a.id == _node_id("run", "/repo/a.py")
    assert node_b.id == _node_id("run", "/repo/b.py")


# ---------------------------------------------------------------------------
# Out-of-scope extensions and missing grammar are non-fatal
# ---------------------------------------------------------------------------


def test_unsupportedExtension_returnsEmptyResultWithoutError() -> None:
    result = _extract("package main\n", "/repo/main.go")
    assert result.nodes == []
    assert result.edges == []
    assert result.fatal_error is None


# ---------------------------------------------------------------------------
# Fix #1 — module node identity never collides with a same-named symbol
# ---------------------------------------------------------------------------


def test_symbolNameMatchesModuleStem_staysDistinctFromModuleNode() -> None:
    """A top-level `def main()` in `main.py` must not collapse into the module node.

    Before the fix, both the module pseudo-node and the `main` function node
    were computed via the same ``_symbol_id(name, file_path)`` formula (since
    the module's stem is also "main"), colliding into one node and turning the
    `defines` edge into a self-loop.
    """
    src = "def main():\n    pass\n"
    result = _extract(src, "/repo/main.py")

    module_node = next(n for n in result.nodes if n.entity_subtype == "python-defref-module")
    func_node = next(n for n in result.nodes if n.entity_subtype == "python-function")

    assert module_node.id != func_node.id, "Module node and same-named symbol node must differ"
    assert module_node.entity_name == "main"
    assert func_node.entity_name == "main"

    defines = _edges_of_type(result, RelationshipType.defines)
    assert len(defines) == 1
    edge = defines[0]
    assert edge.source_node_id == module_node.id
    assert edge.target_node_id == func_node.id
    assert edge.source_node_id != edge.target_node_id, "defines edge must not be a self-loop"


# ---------------------------------------------------------------------------
# Fix #3 — calls to undefined/external names produce no calls edge
# ---------------------------------------------------------------------------


def test_callToUndefinedSymbol_producesNoCallsEdge() -> None:
    """A call to a name not defined anywhere in this file is dropped (cross-file is BE-4's job)."""
    src = "def foo():\n    return helper()\n"
    result = _extract(src, "/repo/mod.py")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert calls == [], f"Expected 0 calls edges for a call to an undefined name, got {len(calls)}"


# ---------------------------------------------------------------------------
# Fix #6 — repeated identical calls dedup to one edge
# ---------------------------------------------------------------------------


def test_repeatedSameCallee_producesOneCallsEdge() -> None:
    src = "def bar():\n    pass\n\ndef foo():\n    bar()\n    bar()\n"
    result = _extract(src, "/repo/mod.py")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 deduped calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.source_node_id == _node_id("foo", "/repo/mod.py")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.py")


# ---------------------------------------------------------------------------
# Fix #7 — missing tree-sitter grammar degrades to a non-fatal warning
# ---------------------------------------------------------------------------


def test_missingGrammar_returnsEmptyResultWithWarning(monkeypatch) -> None:
    import archon_search.defref_extractor as defref_extractor_module

    monkeypatch.setattr(defref_extractor_module, "_get_grammar", lambda ext: None)

    result = _extract("def foo():\n    pass\n", "/repo/mod.py")

    assert result.nodes == []
    assert result.edges == []
    assert result.warnings, "Expected a non-empty warnings list for a missing grammar"
    assert result.fatal_error is None


# ---------------------------------------------------------------------------
# Fix #8 — a trivial file with no defs/calls/imports/inherits still produces
# the module node and no edges.
# ---------------------------------------------------------------------------


def test_emptyFile_producesOnlyModuleNodeAndNoEdges() -> None:
    result = _extract("", "/repo/mod.py")

    assert len(result.nodes) == 1
    assert result.nodes[0].entity_subtype == "python-defref-module"
    assert result.nodes[0].entity_name == "mod"
    assert result.edges == []


# ---------------------------------------------------------------------------
# Fix #9 — aliased Python imports resolve to the ORIGINAL name, not the alias
# ---------------------------------------------------------------------------


def test_aliasedImport_resolvesToOriginalName() -> None:
    src = "import numpy as np\nfrom collections import defaultdict as dd\n"
    result = _extract(src, "/repo/mod.py")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 2, f"Expected 2 imports edges, got {len(imports)}"

    imported_names = {n.entity_name for n in result.nodes if n.entity_subtype == "python-import"}
    assert imported_names == {"numpy", "defaultdict"}, (
        "Aliased import must resolve to the original name, not the local alias "
        f"(got {imported_names})"
    )


# ---------------------------------------------------------------------------
# Fix #10 — known limitation: case-differing same-file symbols collapse
# ---------------------------------------------------------------------------


def test_caseDifferingSameFileSymbols_collapseToOneNode_knownLimitation() -> None:
    """Documents a KNOWN, ACCEPTED limitation inherited from ``make_stable_entity_id``.

    ``make_stable_entity_id`` lowercases its entire canonical input before
    hashing (see ``graph_types.py``), so within one file `class Foo` and
    `def foo` (differing only by case) collapse onto the same `code_symbol`
    node ID. This is NOT introduced by, nor fixed by, BE-2's DefRefExtractor —
    it is inherited shared-helper behavior and out of scope to change here
    (changing `make_stable_entity_id`'s case-sensitivity would affect every
    other consumer of the shared hashing helper).
    """
    src = "class Foo:\n    pass\n\ndef foo():\n    pass\n"
    result = _extract(src, "/repo/mod.py")

    foo_class_id = _node_id("Foo", "/repo/mod.py")
    foo_func_id = _node_id("foo", "/repo/mod.py")
    assert foo_class_id == foo_func_id, (
        "Known limitation: case-differing same-file symbols share one node ID "
        "because make_stable_entity_id lowercases its canonical input"
    )

    matching_nodes = [n for n in result.nodes if n.id == foo_class_id]
    assert len(matching_nodes) == 1, "Both symbols collapse onto exactly one node"
