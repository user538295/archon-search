"""DefRefExtractor — same-file code def/ref edge extraction (E2g BE-2).

Walks a whole file's tree-sitter AST (Python and TypeScript) and extracts
``calls``/``imports``/``defines``/``inherits`` graph edges between
``code_symbol`` nodes, all tagged ``extraction_method="extracted"`` (the
"proven from this file's own text" tier — as opposed to BE-4's future
cross-file ``"inferred"`` tier).

Design notes
------------
- Full-file, not per-chunk: chunking can split a file across chunk
  boundaries, but call/import/inherit/define relationships must be computed
  from the whole file's AST. ``extract()`` therefore takes the raw file text
  and path, not a list of already-chunked ``ChunkInput`` objects. Wiring
  chunk-vs-file data through the ingest pipeline is BE-3's job, not this one.
- DI'd against ``GraphStoreProtocol`` (forward compatible with BE-4, which
  will extend this class to resolve cross-file matches by reading the store);
  this task's same-file-only scope does not read the store.
- ``code_symbol`` node identity: the ID fed into ``make_stable_entity_id`` is
  the qualified string ``f"{name}::{file_path}"`` (see ``_symbol_id()``, which
  now routes through the shared ``make_code_symbol_qualified_name()`` helper
  in ``graph_types.py`` — the same helper ``graph_extractor.py`` uses) so
  that two unrelated same-named symbols in different files hash to distinct
  node IDs. ``GraphNode.entity_name`` itself is NEVER file-qualified — it
  stays the bare symbol name (mirrors the fix applied to
  ``graph_extractor.py``'s code-symbol path for the exact same reason).
- The module pseudo-node's ID is deliberately NOT ``_symbol_id(module_name,
  file_path)`` — that formula is identical to the one used for a REAL symbol
  literally named ``module_name`` (e.g. ``def main()`` in ``main.py``), which
  would collapse the module node and that symbol's node into one and turn
  the ``defines`` edge into a self-loop. ``_module_symbol_id(file_path)``
  feeds a sentinel qualifier (``"__file_module__::{file_path}"``) into
  ``make_stable_entity_id`` instead, so the module node's ID can never
  collide with any real symbol node. The enclosing-scope trackers in
  ``_walk_python``/``_walk_typescript`` use ``""`` (empty string) as the
  sentinel for "top-level / module-enclosing" rather than falling back to
  the literal ``module_name`` string, removing the same string-equality
  ambiguity at the source.
- Never-propagate safety belongs to BE-3 (the pipeline hook); this class is
  still reasonably defensive (grammar-missing / parse-failure warnings are
  returned via ``GraphExtractionResult.warnings``, not raised) since BE-3
  will consume this same result shape unchanged.
- ``calls`` edges are same-file only by construction: a call is only
  recorded when its callee name matches a symbol defined somewhere in this
  same file. Calls to unresolved (out-of-file) names are deliberately
  dropped here — cross-file "inferred" matching is BE-4's job.
- ``imports``/``inherits`` edges are recorded regardless of whether the
  target is itself defined in this file (an import target or a base class
  is very often external) — only ``calls`` is filtered to same-file defs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archon_search.code_enricher import _get_grammar
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphExtractionResult,
    GraphNode,
    RelationshipType,
    make_code_symbol_qualified_name,
    make_stable_edge_id,
    make_stable_entity_id,
)

if TYPE_CHECKING:
    from archon_search.graph_store_protocol import GraphStoreProtocol

logger = logging.getLogger(__name__)

_EXTRACTION_METHOD = "extracted"

_LANG_LABEL: dict[str, str] = {".py": "python", ".ts": "typescript"}
"""Extensions supported by BE-2. Remaining languages are BE-5's scope."""

# A definition record: (name, kind, enclosing_name).
_DefRecord = tuple[str, str, str]
# A call record: (caller_name, callee_name).
_CallRecord = tuple[str, str]
# An inherits record: (class_name, base_name).
_InheritsRecord = tuple[str, str]


class DefRefExtractor:
    """Extracts same-file ``calls``/``imports``/``defines``/``inherits`` edges.

    Usage::

        extractor = DefRefExtractor(graph_store=gs)
        result = await extractor.extract(
            file_text=text, file_path=path, doc_id=doc_id,
            collection=collection, ns=ns,
        )
        await graph_store.write_graph(collection, result.nodes, result.edges, ns=ns)

    ``DefRefExtractor`` never calls ``write_graph`` itself — the caller
    (BE-3's pipeline hook) is responsible for persisting the returned result,
    mirroring ``GraphExtractor.extract()`` and ``SynonymDetector.detect()``.
    """

    def __init__(self, graph_store: "GraphStoreProtocol") -> None:
        self._graph_store = graph_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        *,
        file_text: str,
        file_path: str,
        doc_id: str,
        collection: str,
        ns: str,
    ) -> GraphExtractionResult:
        """Extract def/ref edges from one whole file's source text.

        Args:
            file_text: Full source text of the file (not a single chunk).
            file_path: Source path — used both for the module symbol's bare
                name (basename/stem) and to file-qualify every ``code_symbol``
                node ID via ``_symbol_id()``.
            doc_id: Document ID that produced this extraction; stamped on
                every ``GraphNode``/``GraphEdge`` as ``source_doc_id``.
            collection: Collection name; stamped on every ``GraphNode``.
            ns: Namespace. Unused by this same-file-only slice (kept for
                signature symmetry with the rest of the graph subsystem and
                for BE-4's future cross-file store reads) — last per the
                project's ``ns``-last invariant.

        Returns:
            A ``GraphExtractionResult`` with ``nodes``/``edges`` populated.
            ``mentions`` is always empty: mentions are chunk-scoped incidence
            records and this extractor operates on whole-file text with no
            chunk boundaries — BE-3 owns mapping extraction back onto chunks.
            ``fatal_error`` is never set by this extractor: a missing grammar,
            an unsupported extension, and a tree-sitter parse failure all
            degrade to an empty result with a non-fatal warning instead (see
            the ``except Exception`` branch below — this is intentional and
            defensive, not an oversight).
        """
        del ns  # unused in this same-file-only slice; kept for signature symmetry

        ext = Path(file_path).suffix.lower()
        if ext not in _LANG_LABEL:
            # Out of BE-2's scope (JS/Go/Rust/Java/Bash/Swift/C# are BE-5).
            return GraphExtractionResult(nodes=[], edges=[], mentions=[])

        lang = _get_grammar(ext)
        if lang is None:
            warning = (
                f"tree-sitter grammar unavailable for {ext}; def/ref extraction "
                f"skipped for {file_path!r}"
            )
            logger.warning(warning)
            return GraphExtractionResult(nodes=[], edges=[], mentions=[], warnings=[warning])

        module_name = Path(file_path).stem
        lang_label = _LANG_LABEL[ext]

        try:
            defs, calls, imports, inherits = await asyncio.to_thread(
                _parse_and_walk, file_text, file_path, ext, lang, module_name
            )
        except Exception as exc:  # pragma: no cover - defensive, see module docstring
            warning = f"def/ref extraction failed to parse {file_path!r}: {exc}"
            logger.warning(warning)
            return GraphExtractionResult(nodes=[], edges=[], mentions=[], warnings=[warning])

        return self._build_result(
            module_name=module_name,
            file_path=file_path,
            lang_label=lang_label,
            defs=defs,
            calls=calls,
            imports=imports,
            inherits=inherits,
            doc_id=doc_id,
            collection=collection,
        )

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _build_result(
        self,
        *,
        module_name: str,
        file_path: str,
        lang_label: str,
        defs: list[_DefRecord],
        calls: list[_CallRecord],
        imports: list[str],
        inherits: list[_InheritsRecord],
        doc_id: str,
        collection: str,
    ) -> GraphExtractionResult:
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        defined_names = {name for name, _kind, _enclosing in defs}

        def node_for(name: str, subtype: str) -> GraphNode:
            node_id = _symbol_id(name, file_path)
            if node_id not in nodes:
                nodes[node_id] = GraphNode(
                    id=node_id,
                    entity_name=name,
                    entity_type=EntityType.code_symbol,
                    source_doc_id=doc_id,
                    collection_name=collection,
                    entity_subtype=subtype,
                )
            return nodes[node_id]

        def module_node_for() -> GraphNode:
            """Return (creating if needed) the module pseudo-node.

            Uses ``_module_symbol_id()`` rather than ``node_for(module_name, ...)``
            so this node's ID can never collide with a real symbol node literally
            named ``module_name`` (see module docstring).
            """
            node_id = _module_symbol_id(file_path)
            if node_id not in nodes:
                nodes[node_id] = GraphNode(
                    id=node_id,
                    entity_name=module_name,
                    entity_type=EntityType.code_symbol,
                    source_doc_id=doc_id,
                    collection_name=collection,
                    entity_subtype=f"{lang_label}-module",
                )
            return nodes[node_id]

        def scoped_node_for(scope_name: str, subtype: str) -> GraphNode:
            """Resolve an enclosing/caller scope name to its node.

            ``scope_name == ""`` is the sentinel for "top-level / module-enclosing"
            (see module docstring) and resolves to the module pseudo-node; any
            other value is a real class/function name resolved via ``node_for``.
            """
            if scope_name == "":
                return module_node_for()
            return node_for(scope_name, subtype)

        def add_edge(source_id: str, target_id: str, rel: RelationshipType) -> None:
            edge_id = make_stable_edge_id(source_id, target_id, rel.value)
            edges[edge_id] = GraphEdge(
                id=edge_id,
                source_node_id=source_id,
                target_node_id=target_id,
                relationship_type=rel,
                source_doc_id=doc_id,
                extraction_method=_EXTRACTION_METHOD,
            )

        # Always register the module node — it participates in defines/imports
        # even for files with no top-level definitions.
        module_node = module_node_for()

        # defines: enclosing -> defined symbol. `enclosing == ""` is the
        # top-level sentinel (see module docstring) resolved to the module
        # node; any other value is a real class name.
        for name, kind, enclosing in defs:
            enclosing_node = scoped_node_for(enclosing, f"{lang_label}-class")
            defined_node = node_for(name, f"{lang_label}-{kind}")
            add_edge(enclosing_node.id, defined_node.id, RelationshipType.defines)

        # calls: caller -> callee, only when the callee is defined in this file.
        for caller_name, callee_name in calls:
            if callee_name not in defined_names:
                continue
            caller_node = scoped_node_for(caller_name, f"{lang_label}-symbol")
            callee_node = node_for(callee_name, f"{lang_label}-symbol")
            add_edge(caller_node.id, callee_node.id, RelationshipType.calls)

        # imports: module -> imported name.
        for imported_name in imports:
            imported_node = node_for(imported_name, f"{lang_label}-import")
            add_edge(module_node.id, imported_node.id, RelationshipType.imports)

        # inherits: class -> base.
        for class_name, base_name in inherits:
            class_node = node_for(class_name, f"{lang_label}-class")
            base_node = node_for(base_name, f"{lang_label}-symbol")
            add_edge(class_node.id, base_node.id, RelationshipType.inherits)

        # KNOWN CONTRACT GAP (documentation only — not this task's fix): `mentions`
        # is always empty because mentions are chunk-scoped incidence records and
        # this extractor operates on whole-file text with no chunk boundaries. But
        # GraphStore's orphan-GC (maintenance loop) deletes any node/edge with zero
        # remaining mention rows — once BE-3 wires this extractor into the ingest
        # pipeline, every node/edge produced here is GC-eligible on the very next
        # maintenance pass. BE-3 must resolve this before enabling GC on def/ref
        # data: either by writing mentions for these nodes, or by exempting
        # `extraction_method in {"extracted", "inferred"}` edges from orphan GC.
        return GraphExtractionResult(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            mentions=[],
        )


# ---------------------------------------------------------------------------
# CPU-bound parse + walk — run inside asyncio.to_thread by extract()
# ---------------------------------------------------------------------------


def _parse_and_walk(
    file_text: str,
    file_path: str,
    ext: str,
    lang: Any,
    module_name: str,
) -> tuple[list[_DefRecord], list[_CallRecord], list[str], list[_InheritsRecord]]:
    """Parse *file_text* with tree-sitter and walk the resulting AST.

    CPU-bound (tree-sitter parse + recursive walk) — ``extract()`` runs this
    inside ``asyncio.to_thread`` so it never blocks the event loop, mirroring
    the pattern ``GraphExtractor`` uses for its CPU-bound spaCy calls.
    """
    from tree_sitter import Parser  # type: ignore[import-untyped]  # noqa: PLC0415

    parser = Parser(lang)
    tree = parser.parse(file_text.encode("utf-8"))

    defs: list[_DefRecord] = []
    calls: list[_CallRecord] = []
    imports: list[str] = []
    inherits: list[_InheritsRecord] = []

    if ext == ".py":
        _walk_python(tree.root_node, module_name, "", "", defs, calls, imports, inherits)
    else:
        _walk_typescript(tree.root_node, module_name, "", "", defs, calls, imports, inherits)

    return defs, calls, imports, inherits


# ---------------------------------------------------------------------------
# Node identity — file-qualified hash input, bare display name
# ---------------------------------------------------------------------------


_MODULE_ID_SENTINEL = "<module>"
"""Qualifier prefix for the module pseudo-node's ID (see ``_module_symbol_id``).

Contains ``<``/``>``, which cannot appear in a Python or TypeScript
identifier, so — unlike a plain-underscore name such as ``__file_module__``
(which IS a syntactically valid identifier and could theoretically be a real
symbol name) — this sentinel can never collide with a real extracted symbol
name.
"""


def _symbol_id(name: str, file_path: str) -> str:
    """Return the file-qualified ``code_symbol`` node ID.

    Qualified format: ``f"{name}::{file_path}"`` (via the shared
    ``make_code_symbol_qualified_name()`` helper) fed into
    ``make_stable_entity_id`` — the node's ``entity_name`` stays the bare
    ``name`` (see module docstring).
    """
    return make_stable_entity_id(
        EntityType.code_symbol.value, make_code_symbol_qualified_name(name, file_path)
    )


def _module_symbol_id(file_path: str) -> str:
    """Return the module pseudo-node's ID — never collides with a real symbol node.

    Unlike ``_symbol_id``, the qualified string fed into
    ``make_stable_entity_id`` is anchored to a sentinel (``_MODULE_ID_SENTINEL``)
    rather than the module's bare name, so a real symbol literally named the
    same as the module (e.g. ``def main()`` in ``main.py``) can never hash to
    the same ID as the module node (see module docstring).
    """
    return make_stable_entity_id(
        EntityType.code_symbol.value,
        make_code_symbol_qualified_name(_MODULE_ID_SENTINEL, file_path),
    )


# ---------------------------------------------------------------------------
# Python AST walker
# ---------------------------------------------------------------------------


def _python_import_name(name_node: Any) -> str | None:
    """Return the imported symbol's bare name from a python import ``name`` field entry.

    ``name_node`` is either a ``dotted_name``/``identifier`` (plain import) or
    an ``aliased_import`` (``X as Y``), in which case the ORIGINAL name (not
    the local alias) is returned via its own ``name`` field.
    """
    if name_node.type == "aliased_import":
        inner = name_node.child_by_field_name("name")
        return inner.text.decode("utf-8") if inner is not None else None
    return name_node.text.decode("utf-8")


def _walk_python(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Python tree-sitter AST, populating the record lists."""
    node_type = node.type

    if node_type in {"import_statement", "import_from_statement"}:
        for name_node in node.children_by_field_name("name"):
            imported_name = _python_import_name(name_node)
            if imported_name:
                imports.append(imported_name)
        return

    if node_type == "class_definition":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "class", enclosing))
            superclasses = node.child_by_field_name("superclasses")
            if superclasses is not None:
                for child in superclasses.children:
                    if child.type == "identifier":
                        inherits.append((name, child.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_python(child, module_name, name, "", defs, calls, imports, inherits)
        return

    if node_type == "function_definition":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            kind = "method" if current_class else "function"
            defs.append((name, kind, enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_python(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "call":
        func_node = node.child_by_field_name("function")
        callee: str | None = None
        if func_node is not None:
            if func_node.type == "identifier":
                callee = func_node.text.decode("utf-8")
            elif func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                if attr is not None:
                    callee = attr.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))
        # fall through — recurse into children (e.g. nested calls in arguments)

    for child in node.children:
        _walk_python(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# TypeScript AST walker
# ---------------------------------------------------------------------------


def _walk_typescript(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a TypeScript tree-sitter AST, populating the record lists."""
    node_type = node.type

    if node_type == "import_statement":
        for child in node.children:
            if child.type == "import_clause":
                _collect_typescript_import_names(child, imports)
        return

    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "class", enclosing))
            for child in node.children:
                if child.type == "class_heritage":
                    for heritage_child in child.children:
                        if heritage_child.type == "extends_clause":
                            base_node = heritage_child.child_by_field_name("value")
                            if base_node is not None and base_node.type == "identifier":
                                inherits.append((name, base_node.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_typescript(child, module_name, name, "", defs, calls, imports, inherits)
        return

    if node_type in {"function_declaration", "method_definition"}:
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            kind = "method" if (node_type == "method_definition" or current_class) else "function"
            defs.append((name, kind, enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_typescript(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "call_expression":
        func_node = node.child_by_field_name("function")
        callee: str | None = None
        if func_node is not None:
            if func_node.type == "identifier":
                callee = func_node.text.decode("utf-8")
            elif func_node.type == "member_expression":
                prop = func_node.child_by_field_name("property")
                if prop is not None:
                    callee = prop.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))
        # fall through — recurse into children (e.g. nested calls in arguments)

    for child in node.children:
        _walk_typescript(child, module_name, current_class, current_func, defs, calls, imports, inherits)


def _collect_typescript_import_names(import_clause: Any, imports: list[str]) -> None:
    """Append imported symbol names found in a TS ``import_clause`` node to *imports*.

    Handles default imports (``import Def from ...``), named imports
    (``import { A, B as C } from ...`` — the ORIGINAL name is used, not the
    local alias), and namespace imports (``import * as NS from ...``).
    """
    for child in import_clause.children:
        if child.type == "identifier":
            imports.append(child.text.decode("utf-8"))
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type == "import_specifier":
                    name_node = spec.child_by_field_name("name")
                    if name_node is not None:
                        imports.append(name_node.text.decode("utf-8"))
        elif child.type == "namespace_import":
            for grandchild in child.children:
                if grandchild.type == "identifier":
                    imports.append(grandchild.text.decode("utf-8"))
