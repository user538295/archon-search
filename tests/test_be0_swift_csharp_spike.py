"""BE-0 spike — verify tree-sitter-swift / tree-sitter-c-sharp install and parse
cleanly against the pinned `tree-sitter>=0.25,<0.26` core.

This is a SPIKE, not permanent product wiring: Swift/C# are deliberately NOT
added to `RelationshipType`, `CODE_EXTENSIONS`, or any language-dispatch table
in `code_enricher.py` — that belongs to BE-5. This file only proves (or
disproves) ABI compatibility and clean parsing via a minimal, real, runnable
test, mirroring the exact `Language`/`Parser` construction pattern used in
`archon_search/code_enricher.py::_get_grammar` / `_build_scope_table`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_swiftGrammarSpike_installsAndParsesSample() -> None:
    """tree-sitter-swift installs and parses a minimal Swift file cleanly."""
    from tree_sitter import Language, Parser

    try:
        import tree_sitter_swift
    except ImportError:
        pytest.skip("tree-sitter-swift not installed (requires the 'code' extra)")

    lang = Language(tree_sitter_swift.language())
    parser = Parser(lang)

    source = b"""
func greet(name: String) -> String {
    return "Hello, \\(name)!"
}
"""
    tree = parser.parse(source)
    root = tree.root_node

    assert not root.has_error, "Swift sample parsed with AST errors"
    assert root.child_count > 0, "Swift sample produced an empty AST"


def test_cSharpGrammarSpike_installsAndParsesSample() -> None:
    """tree-sitter-c-sharp installs and parses a minimal C# file cleanly."""
    from tree_sitter import Language, Parser

    try:
        import tree_sitter_c_sharp
    except ImportError:
        pytest.skip("tree-sitter-c-sharp not installed (requires the 'code' extra)")

    lang = Language(tree_sitter_c_sharp.language())
    parser = Parser(lang)

    source = b"""
public class Greeter {
    public string Greet(string name) {
        return "Hello, " + name + "!";
    }
}
"""
    tree = parser.parse(source)
    root = tree.root_node

    assert not root.has_error, "C# sample parsed with AST errors"
    assert root.child_count > 0, "C# sample produced an empty AST"
