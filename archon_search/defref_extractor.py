"""DefRefExtractor — same-file code def/ref edge extraction (E2g BE-2).

Walks a whole file's tree-sitter AST (Python, TypeScript, JavaScript, Go,
Rust, Java, Bash, Swift, C#) and extracts
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
- DI'd against ``GraphStoreProtocol``. BE-4 extends this class to resolve
  cross-file matches: ``calls``/``inherits`` targets not found among this
  file's own definitions are looked up via ``find_nodes_by_name`` against the
  graph store's existing ``code_symbol`` nodes, filtered to ``entity_type ==
  code_symbol`` and matched by exact (case-sensitive) name — Python/TypeScript
  are case-sensitive languages, so a call to ``config`` never matches a class
  named ``Config``. Every
  match found there becomes an edge tagged ``extraction_method="inferred"``
  (the "best-guess, cross-file" tier — as opposed to same-file
  ``"extracted"``). Ambiguous multi-candidate matching policy (Q11): when
  ``find_nodes_by_name`` returns multiple candidates for one name (the same
  name defined in several files), link to **all** of them, each tagged
  ``inferred`` — there is no "best" candidate to pick, so best-guess matching
  is the ceiling, not a single arbitrarily-chosen candidate. Ingest-order
  dependency: cross-file resolution only finds symbols that were already
  written to the graph store by a prior ``write_graph`` call (i.e. a prior
  ingest of the defining file) — if the defining file has not been ingested
  yet, the reference is simply dropped (same as any other unresolved name),
  and will only be resolved retroactively if/when that file is later
  re-ingested and its ``DefRefExtractor.extract()`` is re-run.
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
- ``calls`` edges: a call whose callee name matches a symbol defined
  somewhere in this same file is recorded immediately, tagged ``"extracted"``
  (same-file match always wins — cross-file lookup is never attempted for a
  name already resolved same-file). A call to a name NOT defined in this
  file is looked up cross-file via ``find_nodes_by_name`` (BE-4); every match
  found becomes an ``"inferred"`` edge. A call with no same-file AND no
  cross-file match is dropped (nothing to link to yet — see the ingest-order
  dependency note above).
- ``imports`` edges are recorded regardless of whether the target is itself
  defined in this file (an import target is very often external) and are
  ALWAYS same-file — imports name modules, not this project's cross-file
  ``code_symbol`` graph, so no cross-file resolution is attempted for them.
- ``inherits`` edges: the base class always gets a same-file ``"extracted"``
  edge to a (possibly placeholder) same-file node, exactly as before BE-4
  (an inherited base is very often external and this local edge is cheap
  provenance). Additionally, when the base name is not defined anywhere in
  this file, BE-4 also looks it up cross-file via ``find_nodes_by_name`` and
  adds one additional ``"inferred"`` edge per match found — additive, never
  replacing the pre-existing same-file edge.
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
_INFERRED_EXTRACTION_METHOD = "inferred"

_LANG_LABEL: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".js": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".sh": "bash",
    ".swift": "swift",
    ".cs": "csharp",
}
"""Extensions supported by BE-2 (python/typescript) and BE-5 (the rest)."""

DEFREF_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_LANG_LABEL.keys())
"""File extensions for which DefRefExtractor performs real extraction (BE-2's python/typescript plus BE-5's remaining seven languages)."""

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
            ns: Namespace, passed through to ``find_nodes_by_name`` for BE-4's
                cross-file lookups — last per the project's ``ns``-last
                invariant.

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
        ext = Path(file_path).suffix.lower()
        if ext not in _LANG_LABEL:
            # Out of DefRefExtractor's scope entirely (no grammar dispatch exists).
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

        return await self._build_result(
            module_name=module_name,
            file_path=file_path,
            lang_label=lang_label,
            defs=defs,
            calls=calls,
            imports=imports,
            inherits=inherits,
            doc_id=doc_id,
            collection=collection,
            ns=ns,
        )

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    async def _build_result(
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
        ns: str,
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
                    entity_subtype=f"{lang_label}-defref-module",
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

        def add_edge(
            source_id: str,
            target_id: str,
            rel: RelationshipType,
            extraction_method: str = _EXTRACTION_METHOD,
        ) -> None:
            edge_id = make_stable_edge_id(source_id, target_id, rel.value)
            edges[edge_id] = GraphEdge(
                id=edge_id,
                source_node_id=source_id,
                target_node_id=target_id,
                relationship_type=rel,
                source_doc_id=doc_id,
                extraction_method=extraction_method,
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

        # calls: caller -> callee. Same-file match wins immediately and is
        # tagged "extracted"; unresolved (out-of-file) callees are queued for
        # BE-4's cross-file lookup below and NEVER get a same-file placeholder
        # edge (a call to a genuinely unresolved name has nothing to link to).
        unresolved_calls: list[tuple[str, str]] = []  # (caller_node_id, callee_name)
        for caller_name, callee_name in calls:
            if callee_name in defined_names:
                caller_node = scoped_node_for(caller_name, f"{lang_label}-symbol")
                callee_node = node_for(callee_name, f"{lang_label}-symbol")
                add_edge(caller_node.id, callee_node.id, RelationshipType.calls)
            else:
                caller_node = scoped_node_for(caller_name, f"{lang_label}-symbol")
                unresolved_calls.append((caller_node.id, callee_name))

        # imports: module -> imported name. Always same-file — imports name
        # modules, not this project's code_symbol graph, so no cross-file
        # lookup is attempted for them (BE-4 scope note in module docstring).
        for imported_name in imports:
            imported_node = node_for(imported_name, f"{lang_label}-import")
            add_edge(module_node.id, imported_node.id, RelationshipType.imports)

        # inherits: class -> base. The same-file "extracted" edge is always
        # recorded (unchanged pre-BE-4 behavior — an external base is common
        # and this local edge is cheap provenance). When the base isn't
        # defined in this file, it is ALSO queued for BE-4's cross-file
        # lookup below, which adds additional "inferred" edges per match.
        unresolved_inherits: list[tuple[str, str]] = []  # (class_node_id, base_name)
        for class_name, base_name in inherits:
            class_node = node_for(class_name, f"{lang_label}-class")
            base_node = node_for(base_name, f"{lang_label}-symbol")
            add_edge(class_node.id, base_node.id, RelationshipType.inherits)
            if base_name not in defined_names:
                unresolved_inherits.append((class_node.id, base_name))

        # BE-4 cross-file resolution (Q11 ambiguous multi-candidate policy):
        # look up every unresolved callee/base name against the graph store's
        # existing code_symbol nodes in one batched call. Every match found
        # becomes an "inferred" edge; when a name has multiple candidates
        # (same name defined in several files), link to ALL of them — there
        # is no single "best" candidate to pick (best-guess matching is the
        # ceiling, not an arbitrary choice). A name with zero matches yields
        # no edge (see the ingest-order dependency note in the module
        # docstring: the defining file may simply not be ingested yet).
        names_needed = sorted(
            {callee for _caller_id, callee in unresolved_calls}
            | {base for _class_id, base in unresolved_inherits}
        )
        if names_needed:
            # find_nodes_by_name queries the whole nodes table (shared with
            # graph_expander/alias_loader/pipeline local-mode, which intentionally
            # match all entity types case-insensitively) — it returns PERSON/ORG/GPE
            # NER nodes, synonym nodes, and defref's own import/module pseudo-nodes
            # alongside real code_symbol nodes. The entity_type check below excludes
            # the non-code_symbol NER/synonym/concept nodes, but import/module
            # pseudo-nodes (created by node_for(..., f"{lang_label}-import") and
            # module_node_for()) are THEMSELVES entity_type == code_symbol, so they
            # survive that check — an import statement or a file's module node is not
            # a real definition you can meaningfully call/inherit from, so the
            # entity_subtype suffix check below additionally excludes any foreign
            # candidate whose subtype ends in "-import" or "-defref-module" (these
            # suffixes are language-agnostic — always f"{lang_label}-import" /
            # f"{lang_label}-defref-module" regardless of the candidate's own
            # language). Matching is also exact-case (not lowercased): Python/
            # TypeScript are case-sensitive languages, so `config` must never link to
            # a class named `Config` in another file. find_nodes_by_name's own
            # case-insensitive over-fetch is fine — it's this exact-name +
            # entity_type + entity_subtype filter that narrows it back down.
            candidate_nodes = await self._graph_store.find_nodes_by_name(
                collection, names_needed, ns
            )
            candidates_by_name: dict[str, list[GraphNode]] = {}
            for candidate in candidate_nodes:
                if candidate.entity_type != EntityType.code_symbol:
                    continue
                if candidate.entity_subtype is not None and (
                    candidate.entity_subtype.endswith("-import")
                    or candidate.entity_subtype.endswith("-defref-module")
                ):
                    continue
                candidates_by_name.setdefault(candidate.entity_name, []).append(candidate)

            for caller_node_id, callee_name in unresolved_calls:
                for candidate in candidates_by_name.get(callee_name, []):
                    add_edge(
                        caller_node_id,
                        candidate.id,
                        RelationshipType.calls,
                        _INFERRED_EXTRACTION_METHOD,
                    )

            for class_node_id, base_name in unresolved_inherits:
                for candidate in candidates_by_name.get(base_name, []):
                    add_edge(
                        class_node_id,
                        candidate.id,
                        RelationshipType.inherits,
                        _INFERRED_EXTRACTION_METHOD,
                    )

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

    # `extract()` already rejects any ext not in `_LANG_LABEL` (same keys as
    # `_WALKERS`) before calling this function, so a fallback default here
    # would only mask a future dispatch-table bug.
    walker = _WALKERS[ext]
    walker(tree.root_node, module_name, "", "", defs, calls, imports, inherits)

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


# ---------------------------------------------------------------------------
# JavaScript AST walker (BE-5)
# ---------------------------------------------------------------------------


def _walk_javascript(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a JavaScript tree-sitter AST, populating the record lists.

    Structurally close to ``_walk_typescript`` (identical ``import_clause``
    shape, reused via ``_collect_typescript_import_names``) except
    ``class_heritage`` has no ``extends_clause`` wrapper — the base class is a
    direct ``identifier`` child of ``class_heritage`` in this grammar.
    """
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
                        if heritage_child.type == "identifier":
                            inherits.append((name, heritage_child.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_javascript(child, module_name, name, "", defs, calls, imports, inherits)
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
                _walk_javascript(child, module_name, "", name, defs, calls, imports, inherits)
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
        _walk_javascript(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# Go AST walker (BE-5)
# ---------------------------------------------------------------------------


def _go_import_spec_names(node: Any, imports: list[str]) -> None:
    """Recursively collect Go import package names from an ``import_declaration``.

    Handles both the single-import form (``import "fmt"`` — an ``import_spec``
    directly under the declaration) and the parenthesized form (``import (
    "fmt"\\n"os" )`` — ``import_spec`` nodes nested inside an
    ``import_spec_list``). The imported name used is the last path segment
    (Go's own package-name convention), taken from the ``path`` field's
    ``interpreted_string_literal_content`` child (the string with quotes
    stripped).
    """
    if node.type == "import_spec":
        path_node = node.child_by_field_name("path")
        if path_node is not None:
            for child in path_node.children:
                if child.type == "interpreted_string_literal_content":
                    content = child.text.decode("utf-8")
                    imports.append(content.rsplit("/", 1)[-1])
        return
    for child in node.children:
        _go_import_spec_names(child, imports)


def _walk_go(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Go tree-sitter AST, populating the record lists.

    Go has no classes, so ``inherits`` always stays empty (expected, not a
    gap — Go has no inheritance to extract). Methods are scoped by receiver
    type (``current_class`` slot repurposed to hold the receiver type name)
    so a ``defines`` edge links the receiver type to its method, mirroring
    class-scoped methods in other languages.
    """
    node_type = node.type

    if node_type == "import_declaration":
        _go_import_spec_names(node, imports)
        return

    if node_type == "function_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        if name:
            defs.append((name, "function", ""))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_go(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "method_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        receiver_node = node.child_by_field_name("receiver")
        receiver_type = ""
        if receiver_node is not None:
            for child in receiver_node.children:
                if child.type == "parameter_declaration":
                    type_node = child.child_by_field_name("type")
                    if type_node is not None:
                        # Strip pointer-receiver "*" prefix so `(h *Handler)` and
                        # `(h Handler)` scope to the same receiver type name.
                        receiver_type = type_node.text.decode("utf-8").lstrip("*")
        if name:
            defs.append((name, "method", receiver_type))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_go(child, module_name, receiver_type, name, defs, calls, imports, inherits)
        return

    if node_type == "call_expression":
        func_node = node.child_by_field_name("function")
        callee: str | None = None
        if func_node is not None:
            if func_node.type == "identifier":
                callee = func_node.text.decode("utf-8")
            elif func_node.type == "selector_expression":
                field = func_node.child_by_field_name("field")
                if field is not None:
                    callee = field.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))

    for child in node.children:
        _walk_go(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# Rust AST walker (BE-5)
# ---------------------------------------------------------------------------


def _rust_collect_use_names(node: Any, imports: list[str]) -> None:
    """Recursively collect imported symbol names from a Rust ``use`` argument.

    Handles ``use a::b::C;`` (``scoped_identifier`` — take the ``name`` field,
    the last path segment), ``use a::b::{C, D};`` (``scoped_use_list`` wrapping
    a ``use_list`` of bare identifiers), and a bare ``use name;`` (a plain
    ``identifier``).
    """
    if node.type == "scoped_identifier":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            imports.append(name_node.text.decode("utf-8"))
        return
    if node.type == "identifier":
        imports.append(node.text.decode("utf-8"))
        return
    if node.type == "scoped_use_list":
        list_node = node.child_by_field_name("list")
        if list_node is not None:
            _rust_collect_use_names(list_node, imports)
        return
    if node.type == "use_list":
        for child in node.children:
            if child.type == "identifier":
                imports.append(child.text.decode("utf-8"))
            elif child.type in {"scoped_identifier", "use_list", "scoped_use_list"}:
                _rust_collect_use_names(child, imports)
        return


def _walk_rust(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Rust tree-sitter AST, populating the record lists.

    Rust has no class inheritance, so ``inherits`` always stays empty
    (expected, not a gap). ``struct_item`` defines a "class"-kind symbol;
    ``impl_item`` scopes its ``function_item`` children as methods of that
    struct's name (``current_class`` slot repurposed to hold the ``impl``
    target type name).
    """
    node_type = node.type

    if node_type == "use_declaration":
        arg_node = node.child_by_field_name("argument")
        if arg_node is not None:
            _rust_collect_use_names(arg_node, imports)
        return

    if node_type == "struct_item":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        if name:
            defs.append((name, "class", ""))
        return

    if node_type == "impl_item":
        type_node = node.child_by_field_name("type")
        impl_type = type_node.text.decode("utf-8") if type_node is not None else ""
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_rust(child, module_name, impl_type, "", defs, calls, imports, inherits)
        return

    if node_type == "function_item":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            kind = "method" if current_class else "function"
            defs.append((name, kind, enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_rust(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "call_expression":
        func_node = node.child_by_field_name("function")
        callee: str | None = None
        if func_node is not None:
            if func_node.type == "identifier":
                callee = func_node.text.decode("utf-8")
            elif func_node.type == "field_expression":
                field = func_node.child_by_field_name("field")
                if field is not None:
                    callee = field.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))

    for child in node.children:
        _walk_rust(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# Java AST walker (BE-5)
# ---------------------------------------------------------------------------


def _java_import_name(node: Any) -> str | None:
    """Return the last-segment identifier text from a Java import's target node."""
    if node.type == "scoped_identifier":
        name_node = node.child_by_field_name("name")
        return name_node.text.decode("utf-8") if name_node is not None else None
    if node.type == "identifier":
        return node.text.decode("utf-8")
    return None


def _walk_java(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Java tree-sitter AST, populating the record lists."""
    node_type = node.type

    if node_type == "import_declaration":
        for child in node.children:
            imported_name = _java_import_name(child)
            if imported_name:
                imports.append(imported_name)
        return

    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "class", enclosing))
            superclass_node = node.child_by_field_name("superclass")
            if superclass_node is not None:
                for child in superclass_node.children:
                    if child.type == "type_identifier":
                        inherits.append((name, child.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_java(child, module_name, name, "", defs, calls, imports, inherits)
        return

    if node_type == "method_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "method", enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_java(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "method_invocation":
        name_node = node.child_by_field_name("name")
        callee = name_node.text.decode("utf-8") if name_node is not None else None
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))
        # fall through — recurse into children (e.g. nested calls in arguments)

    for child in node.children:
        _walk_java(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# Bash AST walker (BE-5)
# ---------------------------------------------------------------------------


def _walk_bash(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Bash tree-sitter AST, populating the record lists.

    Bash has no classes or imports in the traditional sense, so ``inherits``
    and ``imports`` always stay empty (expected, not a gap). Function
    definitions map to ``defs``; every command invocation (builtin, external
    binary, or user-defined function — tree-sitter-bash makes no distinction
    at parse time) maps to ``calls``; a call to a name not defined in this
    file (e.g. ``echo``, ``ls``) is dropped downstream by the same
    same-file-only resolution every other language goes through.
    """
    node_type = node.type

    if node_type == "function_definition":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        if name:
            defs.append((name, "function", ""))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_bash(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "command":
        name_node = node.child_by_field_name("name")
        callee: str | None = None
        if name_node is not None:
            for child in name_node.children:
                if child.type == "word":
                    callee = child.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))
        # fall through — recurse into children (e.g. command substitution)

    for child in node.children:
        _walk_bash(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# Swift AST walker (BE-5 — new grammar, per Q7)
# ---------------------------------------------------------------------------


def _swift_import_name(node: Any) -> str | None:
    """Return the imported module name from a Swift ``import_declaration`` target node."""
    if node.type == "identifier":
        for child in node.children:
            if child.type == "simple_identifier":
                return child.text.decode("utf-8")
        return node.text.decode("utf-8")
    return None


def _walk_swift(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a Swift tree-sitter AST, populating the record lists."""
    node_type = node.type

    if node_type == "import_declaration":
        for child in node.children:
            imported_name = _swift_import_name(child)
            if imported_name:
                imports.append(imported_name)
        return

    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "class", enclosing))
            for child in node.children:
                if child.type == "inheritance_specifier":
                    for spec_child in child.children:
                        if spec_child.type == "user_type":
                            for type_child in spec_child.children:
                                if type_child.type == "type_identifier":
                                    inherits.append((name, type_child.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_swift(child, module_name, name, "", defs, calls, imports, inherits)
        return

    if node_type == "function_declaration":
        # NOTE: this grammar labels BOTH the function name and its return type
        # "name" (a return type is `name=user_type`) — child_by_field_name
        # returns the FIRST match, which is the function's own name (verified
        # against the installed grammar; the name node always precedes the
        # return-type node positionally).
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            kind = "method" if current_class else "function"
            defs.append((name, kind, enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_swift(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "call_expression":
        callee: str | None = None
        if node.children:
            first = node.children[0]
            if first.type == "simple_identifier":
                callee = first.text.decode("utf-8")
            elif first.type == "navigation_expression":
                # Member/navigation call (e.g. `self.baz()`, `x.baz()`): the
                # callee name is the `simple_identifier` inside the
                # `navigation_suffix` child, not the receiver expression.
                for suffix_child in first.children:
                    if suffix_child.type == "navigation_suffix":
                        for name_child in suffix_child.children:
                            if name_child.type == "simple_identifier":
                                callee = name_child.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))

    for child in node.children:
        _walk_swift(child, module_name, current_class, current_func, defs, calls, imports, inherits)


# ---------------------------------------------------------------------------
# C# AST walker (BE-5 — new grammar, per Q7)
# ---------------------------------------------------------------------------


def _csharp_using_name(node: Any) -> str | None:
    """Return the last-segment identifier text from a C# ``using_directive`` target node."""
    if node.type == "qualified_name":
        name_node = node.child_by_field_name("name")
        return name_node.text.decode("utf-8") if name_node is not None else None
    if node.type == "identifier":
        return node.text.decode("utf-8")
    return None


def _walk_csharp(
    node: Any,
    module_name: str,
    current_class: str,
    current_func: str,
    defs: list[_DefRecord],
    calls: list[_CallRecord],
    imports: list[str],
    inherits: list[_InheritsRecord],
) -> None:
    """Recursively walk a C# tree-sitter AST, populating the record lists."""
    node_type = node.type

    if node_type == "using_directive":
        for child in node.children:
            imported_name = _csharp_using_name(child)
            if imported_name:
                imports.append(imported_name)
        return

    if node_type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "class", enclosing))
            for child in node.children:
                if child.type == "base_list":
                    for base_child in child.children:
                        if base_child.type == "identifier":
                            inherits.append((name, base_child.text.decode("utf-8")))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_csharp(child, module_name, name, "", defs, calls, imports, inherits)
        return

    if node_type == "method_declaration":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode("utf-8") if name_node is not None else ""
        enclosing = current_class
        if name:
            defs.append((name, "method", enclosing))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_csharp(child, module_name, "", name, defs, calls, imports, inherits)
        return

    if node_type == "invocation_expression":
        func_node = node.child_by_field_name("function")
        callee: str | None = None
        if func_node is not None:
            if func_node.type == "identifier":
                callee = func_node.text.decode("utf-8")
            elif func_node.type == "member_access_expression":
                name_node = func_node.child_by_field_name("name")
                if name_node is not None:
                    callee = name_node.text.decode("utf-8")
        if callee:
            caller = current_func or current_class
            calls.append((caller, callee))

    for child in node.children:
        _walk_csharp(child, module_name, current_class, current_func, defs, calls, imports, inherits)


_WALKERS: dict[str, Any] = {
    ".py": _walk_python,
    ".ts": _walk_typescript,
    ".js": _walk_javascript,
    ".go": _walk_go,
    ".rs": _walk_rust,
    ".java": _walk_java,
    ".sh": _walk_bash,
    ".swift": _walk_swift,
    ".cs": _walk_csharp,
}
"""Extension -> walker function. ``_parse_and_walk`` dispatches through this."""
