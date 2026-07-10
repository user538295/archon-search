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
import importlib

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter_typescript")


def _has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


# Per-language skip guards (not module-level importorskip) — a missing grammar
# for one BE-5 language must only skip that language's own tests, never the
# whole module (which would silently drop the always-required BE-2 python/TS
# coverage above too).
_HAS_JAVASCRIPT = _has_module("tree_sitter_javascript")
_HAS_GO = _has_module("tree_sitter_go")
_HAS_RUST = _has_module("tree_sitter_rust")
_HAS_JAVA = _has_module("tree_sitter_java")
_HAS_BASH = _has_module("tree_sitter_bash")

from archon_search.defref_extractor import DefRefExtractor  # noqa: E402
from archon_search.graph_types import (  # noqa: E402
    EntityType,
    GraphNode,
    RelationshipType,
    make_stable_entity_id,
)


class _FakeGraphStore:
    """Minimal GraphStoreProtocol stand-in — no cross-file matches by default.

    ``find_nodes_by_name`` returns ``[]`` so BE-4's cross-file resolution is a
    guaranteed no-op for tests that don't care about it (same-file-only
    scenarios), mirroring a graph store with nothing cross-file to find yet.
    """

    async def find_nodes_by_name(self, collection: str, names: list[str], ns: str) -> list:
        del collection, names, ns
        return []


class _FakeGraphStoreWithNodes:
    """GraphStoreProtocol stand-in with a preset name -> candidate nodes mapping.

    Used by BE-4 cross-file resolution tests to control exactly what
    ``find_nodes_by_name`` returns, independent of any real GraphStore.
    """

    def __init__(self, nodes_by_lower_name: dict[str, list]) -> None:
        self._nodes_by_lower_name = nodes_by_lower_name
        self.calls: list[tuple[str, list[str], str]] = []

    async def find_nodes_by_name(self, collection: str, names: list[str], ns: str) -> list:
        self.calls.append((collection, list(names), ns))
        result = []
        for name in names:
            result.extend(self._nodes_by_lower_name.get(name.lower(), []))
        return result


def _extract(file_text: str, file_path: str, graph_store: object | None = None) -> object:
    extractor = DefRefExtractor(graph_store=graph_store or _FakeGraphStore())  # type: ignore[arg-type]

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
# BE-5 — JavaScript same-file call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_JAVASCRIPT, reason="tree-sitter-javascript not installed")
def test_javaScriptSameFileCall_producesCallsEdge() -> None:
    src = "function bar() {\n  return 1;\n}\n\nfunction foo() {\n  return bar();\n}\n"
    result = _extract(src, "/repo/mod.js")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.js")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.js")


# ---------------------------------------------------------------------------
# BE-5 — Go same-file call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GO, reason="tree-sitter-go not installed")
def test_goSameFileCall_producesCallsEdge() -> None:
    src = "package main\n\nfunc bar() int {\n\treturn 1\n}\n\nfunc foo() int {\n\treturn bar()\n}\n"
    result = _extract(src, "/repo/mod.go")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.go")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.go")


# ---------------------------------------------------------------------------
# BE-5 — Rust same-file call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RUST, reason="tree-sitter-rust not installed")
def test_rustSameFileCall_producesCallsEdge() -> None:
    src = "fn bar() -> i32 {\n    1\n}\n\nfn foo() -> i32 {\n    bar()\n}\n"
    result = _extract(src, "/repo/mod.rs")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.rs")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.rs")


# ---------------------------------------------------------------------------
# BE-5 — Java same-file call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_JAVA, reason="tree-sitter-java not installed")
def test_javaSameFileCall_producesCallsEdge() -> None:
    src = (
        "class Foo {\n"
        "    int bar() {\n"
        "        return baz();\n"
        "    }\n\n"
        "    int baz() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    )
    result = _extract(src, "/repo/Foo.java")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("bar", "/repo/Foo.java")
    assert edge.target_node_id == _node_id("baz", "/repo/Foo.java")


# ---------------------------------------------------------------------------
# BE-5 — Bash same-file call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_BASH, reason="tree-sitter-bash not installed")
def test_bashSameFileCall_producesCallsEdge() -> None:
    src = 'bar() {\n  echo "bar"\n}\n\nfoo() {\n  bar\n}\n'
    result = _extract(src, "/repo/mod.sh")

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.sh")
    assert edge.target_node_id == _node_id("bar", "/repo/mod.sh")


# ---------------------------------------------------------------------------
# BE-5 — JavaScript imports + inherits + defines (calls-only was S8's original
# gap for the other new languages too — this closes it for JS specifically)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_JAVASCRIPT, reason="tree-sitter-javascript not installed")
def test_javaScriptImportAndInherits_produceEdges() -> None:
    src = (
        'import { Base } from "./base";\n\n'
        "class Foo extends Base {\n"
        "  bar() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    result = _extract(src, "/repo/mod.js")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected exactly 1 imports edge, got {len(imports)}"
    assert imports[0].extraction_method == "extracted"
    assert imports[0].source_node_id == _module_node_id("/repo/mod.js")
    assert imports[0].target_node_id == _node_id("Base", "/repo/mod.js")

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected exactly 1 inherits edge, got {len(inherits)}"
    assert inherits[0].extraction_method == "extracted"
    assert inherits[0].source_node_id == _node_id("Foo", "/repo/mod.js")
    assert inherits[0].target_node_id == _node_id("Base", "/repo/mod.js")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert any(
        e.source_node_id == _module_node_id("/repo/mod.js")
        and e.target_node_id == _node_id("Foo", "/repo/mod.js")
        for e in defines
    ), "Expected module -> Foo (class) defines edge"
    assert any(
        e.source_node_id == _node_id("Foo", "/repo/mod.js")
        and e.target_node_id == _node_id("bar", "/repo/mod.js")
        for e in defines
    ), "Expected Foo -> bar (method) defines edge"


# ---------------------------------------------------------------------------
# BE-5 — Go imports + defines (Go has no classes, so no inherits case; the
# method's receiver type — not a module — scopes the defines edge)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GO, reason="tree-sitter-go not installed")
def test_goImportAndMethodDefines_produceEdges() -> None:
    src = (
        "package main\n\n"
        'import "fmt"\n\n'
        "type Handler struct {\n"
        "}\n\n"
        "func (h *Handler) Process() int {\n"
        "\treturn 1\n"
        "}\n"
    )
    result = _extract(src, "/repo/mod.go")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected exactly 1 imports edge, got {len(imports)}"
    assert imports[0].extraction_method == "extracted"
    assert imports[0].source_node_id == _module_node_id("/repo/mod.go")
    assert imports[0].target_node_id == _node_id("fmt", "/repo/mod.go")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert len(defines) == 1, f"Expected exactly 1 defines edge, got {len(defines)}"
    edge = defines[0]
    assert edge.extraction_method == "extracted"
    # Go has no class defs; the pointer-receiver type "Handler" scopes the method.
    assert edge.source_node_id == _node_id("Handler", "/repo/mod.go")
    assert edge.target_node_id == _node_id("Process", "/repo/mod.go")


# ---------------------------------------------------------------------------
# BE-5 — Rust imports + defines (Rust has no inheritance either; `impl` scopes
# a function to the struct it implements)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RUST, reason="tree-sitter-rust not installed")
def test_rustImportAndImplDefines_produceEdges() -> None:
    src = "use HashMap;\n\nstruct Handler;\n\nimpl Handler {\n    fn process(&self) -> i32 {\n        1\n    }\n}\n"
    result = _extract(src, "/repo/mod.rs")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected exactly 1 imports edge, got {len(imports)}"
    assert imports[0].extraction_method == "extracted"
    assert imports[0].source_node_id == _module_node_id("/repo/mod.rs")
    assert imports[0].target_node_id == _node_id("HashMap", "/repo/mod.rs")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert any(
        e.source_node_id == _module_node_id("/repo/mod.rs")
        and e.target_node_id == _node_id("Handler", "/repo/mod.rs")
        for e in defines
    ), "Expected module -> Handler (struct) defines edge"
    assert any(
        e.source_node_id == _node_id("Handler", "/repo/mod.rs")
        and e.target_node_id == _node_id("process", "/repo/mod.rs")
        for e in defines
    ), "Expected Handler -> process (method via impl) defines edge"


# ---------------------------------------------------------------------------
# BE-5 — Java imports + inherits + defines
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_JAVA, reason="tree-sitter-java not installed")
def test_javaImportAndInherits_produceEdges() -> None:
    src = (
        "import java.util.List;\n\n"
        "class Base {\n"
        "}\n\n"
        "class Foo extends Base {\n"
        "    int bar() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    )
    result = _extract(src, "/repo/Foo.java")

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1, f"Expected exactly 1 imports edge, got {len(imports)}"
    assert imports[0].extraction_method == "extracted"
    assert imports[0].source_node_id == _module_node_id("/repo/Foo.java")
    assert imports[0].target_node_id == _node_id("List", "/repo/Foo.java")

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected exactly 1 inherits edge, got {len(inherits)}"
    assert inherits[0].extraction_method == "extracted"
    assert inherits[0].source_node_id == _node_id("Foo", "/repo/Foo.java")
    assert inherits[0].target_node_id == _node_id("Base", "/repo/Foo.java")

    defines = _edges_of_type(result, RelationshipType.defines)
    assert any(
        e.source_node_id == _module_node_id("/repo/Foo.java")
        and e.target_node_id == _node_id("Foo", "/repo/Foo.java")
        for e in defines
    ), "Expected module -> Foo (class) defines edge"
    assert any(
        e.source_node_id == _node_id("Foo", "/repo/Foo.java")
        and e.target_node_id == _node_id("bar", "/repo/Foo.java")
        for e in defines
    ), "Expected Foo -> bar (method) defines edge"


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
    # .rb has no DefRefExtractor dispatch entry at all (unlike .go, which is
    # BE-5 in-scope) — a genuinely unsupported extension.
    result = _extract("puts 'hi'\n", "/repo/main.rb")
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


# ---------------------------------------------------------------------------
# BE-4 — cross-file name-based matching (inferred tier)
# ---------------------------------------------------------------------------


def _foreign_node(name: str, file_path: str) -> GraphNode:
    """Build a GraphNode as if it were already persisted from a different file."""
    return GraphNode(
        id=_node_id(name, file_path),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-other",
        collection_name="col",
        entity_subtype="python-function",
    )


def test_crossFileSameNameCall_producesInferredEdge() -> None:
    """A call to a name not defined in this file, but found cross-file, is inferred."""
    helper_node = _foreign_node("helper", "/repo/other.py")
    store = _FakeGraphStoreWithNodes({"helper": [helper_node]})

    src = "def foo():\n    return helper()\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 inferred calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "inferred"
    assert edge.source_node_id == _node_id("foo", "/repo/mod.py")
    assert edge.target_node_id == helper_node.id

    # The lookup must be scoped to the unresolved name only.
    assert store.calls == [("col", ["helper"], "default")]


def test_sameFileMatch_neverTaggedInferred() -> None:
    """A same-file match always resolves through the extracted path, never inferred.

    The fake store is seeded with a DECOY node for "bar" to prove that even
    when a cross-file candidate technically exists, a same-file match takes
    precedence and cross-file lookup is never consulted for that name.
    """
    decoy_node = _foreign_node("bar", "/repo/decoy.py")
    store = _FakeGraphStoreWithNodes({"bar": [decoy_node]})

    src = "def bar():\n    return 1\n\ndef foo():\n    return bar()\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 1, f"Expected exactly 1 calls edge, got {len(calls)}"
    edge = calls[0]
    assert edge.extraction_method == "extracted"
    assert edge.target_node_id == _node_id("bar", "/repo/mod.py")
    assert edge.target_node_id != decoy_node.id

    # "bar" was resolved same-file — never looked up cross-file.
    assert store.calls == []


def test_crossFileAmbiguousName_resolvesPerDocumentedPolicy() -> None:
    """Three files each define the same-named function; caller links to ALL candidates.

    Documented policy: best-guess cross-file matching is the ceiling, not a
    single arbitrarily-chosen candidate — every ambiguous match gets an
    "inferred" edge.
    """
    candidates = [
        _foreign_node("process", "/repo/a.py"),
        _foreign_node("process", "/repo/b.py"),
        _foreign_node("process", "/repo/c.py"),
    ]
    store = _FakeGraphStoreWithNodes({"process": candidates})

    src = "def foo():\n    return process()\n"
    result = _extract(src, "/repo/caller.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert len(calls) == 3, f"Expected 1 inferred edge per ambiguous candidate, got {len(calls)}"
    for edge in calls:
        assert edge.extraction_method == "inferred"
        assert edge.source_node_id == _node_id("foo", "/repo/caller.py")

    target_ids = {e.target_node_id for e in calls}
    assert target_ids == {c.id for c in candidates}, "Every candidate must receive its own edge"


# ---------------------------------------------------------------------------
# Fix review follow-ups: cross-file inherits, import isolation, entity_type
# filter, and case-sensitive matching (see task tracking BE-4 review fixes).
# ---------------------------------------------------------------------------


def test_crossFileInherits_addsInferredEdgeAdditively() -> None:
    """An unresolved base class gets a same-file placeholder edge AND an
    additive cross-file inferred edge to the foreign candidate — the
    same-file edge is never replaced.
    """
    base_node = _foreign_node("Base", "/repo/base.py")
    store = _FakeGraphStoreWithNodes({"base": [base_node]})

    src = "class Foo(Base):\n    pass\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 2, f"Expected 1 extracted + 1 inferred edge, got {len(inherits)}"

    extracted = [e for e in inherits if e.extraction_method == "extracted"]
    inferred = [e for e in inherits if e.extraction_method == "inferred"]
    assert len(extracted) == 1
    assert extracted[0].source_node_id == _node_id("Foo", "/repo/mod.py")
    assert extracted[0].target_node_id == _node_id("Base", "/repo/mod.py")

    assert len(inferred) == 1
    assert inferred[0].source_node_id == _node_id("Foo", "/repo/mod.py")
    assert inferred[0].target_node_id == base_node.id


def test_crossFileInherits_noCandidates_keepsOnlySameFileEdge() -> None:
    """An unresolved base with zero cross-file candidates keeps only the
    same-file "extracted" edge; no "inferred" edge is added.
    """
    store = _FakeGraphStoreWithNodes({})

    src = "class Foo(Base):\n    pass\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    inherits = _edges_of_type(result, RelationshipType.inherits)
    assert len(inherits) == 1, f"Expected exactly 1 (extracted-only) edge, got {len(inherits)}"
    assert inherits[0].extraction_method == "extracted"
    assert inherits[0].target_node_id == _node_id("Base", "/repo/mod.py")


def test_importName_neverResolvedCrossFile() -> None:
    """Imports are always same-file: a name collision with a foreign node
    must never produce an "inferred" imports edge, and the import name must
    never be sent to the cross-file lookup at all.
    """
    foreign_os_node = _foreign_node("os", "/repo/other.py")
    store = _FakeGraphStoreWithNodes({"os": [foreign_os_node]})

    src = "import os\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    imports = _edges_of_type(result, RelationshipType.imports)
    assert len(imports) == 1
    assert imports[0].extraction_method == "extracted"
    assert all(e.extraction_method != "inferred" for e in result.edges)

    # "os" must never appear in any batch of names sent to find_nodes_by_name.
    for _collection, names, _ns in store.calls:
        assert "os" not in names, "import names must never enter cross-file resolution"


def test_crossFileCall_entityTypeFilter_excludesNonCodeSymbolCandidates() -> None:
    """A cross-file candidate that matches by name but is not a code_symbol
    (e.g. an NER concept/person node) must never receive an inferred edge.
    """
    non_code_node = GraphNode(
        id=_node_id("helper", "/repo/other.py") + "-concept",
        entity_name="helper",
        entity_type=EntityType.concept,
        source_doc_id="doc-other",
        collection_name="col",
        entity_subtype="ner-concept",
    )
    store = _FakeGraphStoreWithNodes({"helper": [non_code_node]})

    src = "def foo():\n    return helper()\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert calls == [], "A non-code_symbol candidate must never produce an inferred edge"


def test_crossFileCall_entitySubtypeFilter_excludesForeignPseudoNodes() -> None:
    """A foreign candidate that IS entity_type == code_symbol but is one of
    DefRefExtractor's own import/module pseudo-nodes (entity_subtype ending
    in "-import" or "-defref-module") must never receive an inferred edge —
    an import statement or a file's module node is not a real definition.
    """
    foreign_import_node = GraphNode(
        id=_node_id("os", "/repo/other.py") + "-import",
        entity_name="os",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-other",
        collection_name="col",
        entity_subtype="python-import",
    )
    foreign_module_node = GraphNode(
        id=_node_id("other", "/repo/other.py") + "-module",
        entity_name="other",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-other",
        collection_name="col",
        entity_subtype="python-defref-module",
    )
    store = _FakeGraphStoreWithNodes(
        {"os": [foreign_import_node], "other": [foreign_module_node]}
    )

    src = "def foo():\n    os()\n    other()\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert calls == [], (
        "Foreign import/module pseudo-node candidates must never produce an inferred edge"
    )


def test_crossFileCall_caseSensitive_neverCrossesCaseBoundary() -> None:
    """Python/TypeScript are case-sensitive: an unresolved callee `config`
    must never match a foreign candidate named `Config`.
    """
    differently_cased_node = _foreign_node("Config", "/repo/other.py")
    store = _FakeGraphStoreWithNodes({"config": [differently_cased_node]})

    src = "def foo():\n    return config()\n"
    result = _extract(src, "/repo/mod.py", graph_store=store)

    calls = _edges_of_type(result, RelationshipType.calls)
    assert calls == [], "A case-differing candidate must never produce a cross-file inferred edge"
