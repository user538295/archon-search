"""AST walker: traverse a tree-sitter AST and collect symbol definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class SymbolDef:
    """A single symbol definition discovered during AST traversal."""

    name: str
    kind: str  # "function", "method", "class"
    start_byte: int
    end_byte: int
    parent_class: str = ""


def collect_symbols(source: str, language: Any) -> list[SymbolDef]:
    """Collect all top-level and nested symbol definitions from *source*.

    Walks the tree-sitter parse tree depth-first and returns one
    :class:`SymbolDef` per function definition, method definition, or class
    definition encountered.  Decorated definitions use the outer decorator
    node's start byte so the full decorated span is attributed to the symbol.

    Args:
        source: Raw source text; used only for debug output, not re-parsed.
        language: A ``tree_sitter.Language`` instance for the file's language.

    Returns:
        Unsorted list of :class:`SymbolDef` objects — one per symbol found.
    """
    # Encode source for tree-sitter (UTF-8 bytes)
    source_bytes = source.encode("utf-8")

    import tree_sitter  # noqa: PLC0415 — intentionally lazy

    parser = tree_sitter.Parser(language)
    tree = parser.parse(source_bytes)
    return list(_walk_tree(tree.root_node, parent_class=""))


def _walk_tree(node: Any, parent_class: str) -> Iterator[SymbolDef]:
    """Depth-first generator that yields SymbolDef for relevant node types."""
    kind_map = {
        "function_definition": "function",
        "function_declaration": "function",
        "method_definition": "method",
        "class_definition": "class",
        "class_declaration": "class",
    }

    node_type = node.type
    if node_type == "decorated_definition":
        # Prefer the outer decorated_definition span; inspect the inner child.
        inner = _first_named_child(node, {"function_definition", "class_definition"})
        if inner is not None:
            name_node = inner.child_by_field_name("name")
            name = name_node.text.decode() if name_node else ""
            inner_kind = kind_map.get(inner.type, "function")
            if inner_kind == "function" and parent_class:
                inner_kind = "method"
            yield SymbolDef(
                name=name,
                kind=inner_kind,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                parent_class=parent_class,
            )
            # Recurse into the inner node's body, not the outer decorated node
            for child in inner.children:
                if child.is_named:
                    yield from _walk_tree(child, parent_class=parent_class)
        return  # decorated_definition handled; do not fall through

    if node_type in kind_map:
        kind = kind_map[node_type]
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode() if name_node else ""
        this_parent_class = parent_class
        if kind == "class":
            this_parent_class = name
        if kind == "function" and parent_class:
            kind = "method"
        yield SymbolDef(
            name=name,
            kind=kind,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            parent_class=parent_class,
        )
        for child in node.children:
            if child.is_named:
                yield from _walk_tree(child, parent_class=this_parent_class)
        return  # children already visited above

    for child in node.children:
        if child.is_named:
            yield from _walk_tree(child, parent_class=parent_class)


def _first_named_child(node: Any, types: set[str]) -> Any | None:
    """Return the first named child of *node* whose type is in *types*."""
    for child in node.children:
        if child.is_named and child.type in types:
            return child
    return None


class AstWalker:
    """High-level walker that builds a symbol list from a source file."""

    def __init__(self, language: Any) -> None:
        self._language = language
        self._symbols: list[SymbolDef] = field(default_factory=list)  # type: ignore[assignment]

    def walk_tree(self, source: str) -> list[SymbolDef]:
        """Parse *source* and return all discovered symbol definitions.

        Args:
            source: Source code text for the target language.

        Returns:
            List of :class:`SymbolDef` objects, unsorted.
        """
        self._symbols = collect_symbols(source, self._language)
        return self._symbols

    def filter_by_kind(self, kind: str) -> list[SymbolDef]:
        """Return only the symbols whose ``kind`` matches *kind*.

        Args:
            kind: One of ``"function"``, ``"method"``, or ``"class"``.

        Returns:
            Filtered list; may be empty.
        """
        return [s for s in self._symbols if s.kind == kind]

    def symbol_names(self) -> list[str]:
        """Return the names of all discovered symbols, in discovery order."""
        return [s.name for s in self._symbols]
