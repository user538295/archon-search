"""Code symbol context enrichment — AST-based metadata for code chunks.

Provides :class:`CodeEnricher` with two public methods following the same
``prepare()`` / ``enrich_chunk()`` protocol as :class:`MarkdownEnricher`:

* ``prepare(text, ext, file_path, collection_root)`` — parses source with
  tree-sitter and returns a :data:`ScopeTable` of :class:`ScopeEntry` tuples.
* ``enrich_chunk(chunk, scope_table)`` — resolves the innermost scope
  containing ``chunk.start_offset`` and returns a ``dict[str, str]`` with
  five symbol-level metadata fields.

Tree-sitter is an **optional** dependency (``[code]`` extra). This module
must be importable without tree-sitter installed; all grammar loading is
deferred to :func:`_get_grammar` which is called lazily at parse time.
"""

from __future__ import annotations

import logging
from collections import namedtuple
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from archon_search._types import ChunkRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ScopeEntry = namedtuple("ScopeEntry", ["start", "end", "symbol_type", "fn_name", "class_name"])
"""A single scope boundary extracted from a tree-sitter parse.

Fields:
    start (int): Character offset (inclusive) where the scope starts.
    end (int): Character offset (exclusive) where the scope ends.
    symbol_type (str): One of "function", "method", "class", "module".
    fn_name (str): Innermost function/method name; "" if none.
    class_name (str): Innermost class name; "" if none.
"""

ScopeTable = list  # list[ScopeEntry], sorted by (start ASC, end DESC)
"""List of :class:`ScopeEntry` sorted by ``(start ASC, end DESC)``.

Innermost scopes at the same start offset appear last (highest index),
so the backward walk in :func:`_resolve_scope` finds them first.
"""

# ---------------------------------------------------------------------------
# Code extension set — used for pipeline dispatch
# ---------------------------------------------------------------------------

CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".ts", ".js", ".go", ".rs", ".java", ".sh"})
"""File extensions routed to :class:`CodeEnricher` during ingest."""

# ---------------------------------------------------------------------------
# Module-level grammar registry (lazy-loaded)
# ---------------------------------------------------------------------------

_GRAMMAR_CACHE: dict[str, Any | None] = {}
"""Extension → Language object (or None if unavailable). Populated lazily."""

_GRAMMAR_LOGGED: set[str] = set()
"""Extensions for which a one-time WARNING "grammar not available" was emitted."""

_parse_failure_count: dict[str, int] = {}
"""Per-process per-extension WARNING cap (K=10). After 10 failures for an
extension, further parse-failure WARNINGs are downgraded to DEBUG.
"""

_PARSE_FAILURE_CAP: int = 10

# ---------------------------------------------------------------------------
# Module path derivation
# ---------------------------------------------------------------------------


def _module_path(file_path: Path, collection_root: Path | None) -> str:
    """Derive a dotted module path from a file path.

    Algorithm (applied in order):
    1. If ``collection_root`` is None: return ``file_path.stem`` (no dots).
    2. Compute ``rel = file_path.relative_to(collection_root)``.
    3. Strip one extension only: ``parts = rel.with_suffix("").parts``.
    4. Join parts with ``"."`` separating directories.
    5. If the last segment is ``__init__``: drop it (Python package root).
       If this leaves an empty string (e.g. root-level ``__init__.py``),
       return ``""`` — this is the correct value for a root package init.
    6. For Python files only (ext ``.py``): replace hyphens with underscores
       in each path segment.
    7. If the last segment is ``index`` and ext is ``.ts`` or ``.js``: drop it
       (Node convention). Note: ``.d.ts`` files will have last segment
       ``"index.d"`` after step 3, so step 7 does NOT fire for them.
    """
    ext = file_path.suffix.lower()

    # Step 1 — no collection root fallback
    if collection_root is None:
        return file_path.stem

    # Step 2 — relative path
    rel = file_path.relative_to(collection_root)

    # Step 3 — strip one extension
    parts = list(rel.with_suffix("").parts)

    # Step 6 — replace hyphens for Python only (done before joining so we
    # operate on individual segments)
    if ext == ".py":
        parts = [p.replace("-", "_") for p in parts]

    # Step 4 — join with "."
    joined = ".".join(parts)

    # Step 5 — drop trailing __init__
    if joined == "__init__" or joined.endswith(".__init__"):
        joined = joined[: -(len(".__init__"))] if joined.endswith(".__init__") else ""

    # Step 7 — drop trailing index for TS/JS
    if ext in {".ts", ".js"}:
        if joined == "index" or joined.endswith(".index"):
            joined = joined[: -(len(".index"))] if joined.endswith(".index") else ""

    return joined


# ---------------------------------------------------------------------------
# Grammar registry
# ---------------------------------------------------------------------------


def _get_grammar(ext: str) -> Any | None:
    """Return the tree-sitter Language for *ext*, or None if unavailable.

    Results are cached in :data:`_GRAMMAR_CACHE`. A one-time WARNING log is
    emitted the first time a grammar is found missing.
    """
    if ext in _GRAMMAR_CACHE:
        return _GRAMMAR_CACHE[ext]

    lang = None
    try:
        from tree_sitter import Language  # type: ignore[import-untyped]  # noqa: PLC0415

        if ext == ".py":
            import tree_sitter_python  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_python.language())
        elif ext == ".ts":
            import tree_sitter_typescript  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_typescript.language_typescript())
        elif ext == ".js":
            import tree_sitter_javascript  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_javascript.language())
        elif ext == ".go":
            import tree_sitter_go  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_go.language())
        elif ext == ".rs":
            import tree_sitter_rust  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_rust.language())
        elif ext == ".java":
            import tree_sitter_java  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_java.language())
        elif ext == ".sh":
            import tree_sitter_bash  # type: ignore[import-untyped]  # noqa: PLC0415

            lang = Language(tree_sitter_bash.language())
        else:
            # Unknown extension — cache None silently
            pass
    except (ImportError, Exception):
        pass

    if lang is None and ext not in _GRAMMAR_LOGGED:
        logger.warning(
            "tree-sitter grammar not available for %s; code enrichment skipped",
            ext,
        )
        _GRAMMAR_LOGGED.add(ext)

    _GRAMMAR_CACHE[ext] = lang
    return lang


def missing_code_parser_extensions() -> list[str]:
    """Return the sorted list of extensions that have hit the missing-grammar branch.

    Reuses :data:`_GRAMMAR_LOGGED` as the source of truth — it is a
    process-global cache of extensions for which no tree-sitter grammar
    could be loaded. This is the single public accessor for that state;
    callers must not read :data:`_GRAMMAR_LOGGED` directly.
    """
    return sorted(_GRAMMAR_LOGGED)


def has_missing_code_parsers() -> bool:
    """Return True if any file extension has hit the missing-grammar branch."""
    return bool(missing_code_parser_extensions())


# ---------------------------------------------------------------------------
# Scope table builder
# ---------------------------------------------------------------------------


def _build_scope_table(source: str, lang: Any, ext: str) -> ScopeTable:
    """Parse *source* with tree-sitter and build a sorted :data:`ScopeTable`.

    Byte offsets from tree-sitter are converted to character offsets by
    pre-computing a byte-to-char map over *source*.
    """
    from tree_sitter import Parser  # type: ignore[import-untyped]  # noqa: PLC0415

    # Build byte-to-char offset map (one pass over source)
    byte_to_char: dict[int, int] = {}
    byte_pos = 0
    for char_idx, ch in enumerate(source):
        byte_to_char[byte_pos] = char_idx
        byte_pos += len(ch.encode("utf-8"))
    # EOF sentinel: tree-sitter root.end_byte == total byte length
    byte_to_char[byte_pos] = len(source)

    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node

    entries: list[ScopeEntry] = []
    _walk_node(root, source, byte_to_char, entries, ext, current_class="")
    entries.sort(key=lambda e: (e.start, -e.end))
    return entries


def _walk_node(
    node: Any,
    source: str,
    byte_to_char: dict[int, int],
    entries: list[ScopeEntry],
    ext: str,
    current_class: str,
) -> None:
    """Recursively walk the AST and populate *entries* with scope entries."""
    node_type = node.type

    # Python: decorated_definition wraps decorator + function/class.
    # Emit a single entry using the decorated_definition boundaries but
    # extracting names from the inner function_definition or class_definition.
    if node_type == "decorated_definition":
        inner = None
        for child in node.children:
            if child.type in {"function_definition", "class_definition"}:
                inner = child
                break
        if inner is not None:
            inner_type = inner.type
            name_node = _find_name_child(inner)
            name = name_node.text.decode("utf-8") if name_node else ""
            start_char = byte_to_char[node.start_byte]
            end_char = byte_to_char[node.end_byte]
            if inner_type == "function_definition":
                symbol_type = "method" if current_class else "function"
                entries.append(
                    ScopeEntry(
                        start=start_char,
                        end=end_char,
                        symbol_type=symbol_type,
                        fn_name=name,
                        class_name=current_class,
                    )
                )
                # Walk children of the inner function body (not the decorated_definition
                # children, to avoid re-processing the inner function_definition node)
                body = _find_body_child(inner)
                if body is not None:
                    for child in body.children:
                        _walk_node(child, source, byte_to_char, entries, ext, current_class)
            elif inner_type == "class_definition":
                entries.append(
                    ScopeEntry(
                        start=start_char,
                        end=end_char,
                        symbol_type="class",
                        fn_name="",
                        class_name=name,
                    )
                )
                body = _find_body_child(inner)
                if body is not None:
                    for child in body.children:
                        _walk_node(child, source, byte_to_char, entries, ext, name)
        return  # do not recurse into decorated_definition children

    # Python function definition
    if node_type == "function_definition":
        name_node = _find_name_child(node)
        name = name_node.text.decode("utf-8") if name_node else ""
        symbol_type = "method" if current_class else "function"
        start_char = byte_to_char[node.start_byte]
        end_char = byte_to_char[node.end_byte]
        entries.append(
            ScopeEntry(
                start=start_char,
                end=end_char,
                symbol_type=symbol_type,
                fn_name=name,
                class_name=current_class,
            )
        )
        # Recurse into the function body; nested classes get current_class="" reset
        for child in node.children:
            if child.type in {"block", "statement_block"}:
                for grandchild in child.children:
                    _walk_node(grandchild, source, byte_to_char, entries, ext, current_class="")
        return

    # Python class definition
    if node_type == "class_definition":
        name_node = _find_name_child(node)
        name = name_node.text.decode("utf-8") if name_node else ""
        start_char = byte_to_char[node.start_byte]
        end_char = byte_to_char[node.end_byte]
        entries.append(
            ScopeEntry(
                start=start_char,
                end=end_char,
                symbol_type="class",
                fn_name="",
                class_name=name,
            )
        )
        # Recurse into class body with updated class context
        for child in node.children:
            if child.type in {"block", "class_body"}:
                for grandchild in child.children:
                    _walk_node(grandchild, source, byte_to_char, entries, ext, current_class=name)
        return

    # TypeScript/JavaScript: function_declaration
    if node_type == "function_declaration":
        name_node = _find_name_child(node)
        name = name_node.text.decode("utf-8") if name_node else ""
        symbol_type = "method" if current_class else "function"
        start_char = byte_to_char[node.start_byte]
        end_char = byte_to_char[node.end_byte]
        entries.append(
            ScopeEntry(
                start=start_char,
                end=end_char,
                symbol_type=symbol_type,
                fn_name=name,
                class_name=current_class,
            )
        )
        for child in node.children:
            if child.type == "statement_block":
                for grandchild in child.children:
                    _walk_node(grandchild, source, byte_to_char, entries, ext, current_class="")
        return

    # TypeScript/JavaScript: class_declaration
    if node_type == "class_declaration":
        name_node = _find_name_child(node)
        name = name_node.text.decode("utf-8") if name_node else ""
        start_char = byte_to_char[node.start_byte]
        end_char = byte_to_char[node.end_byte]

        # Find the earliest decorator and extend scope start
        earliest_decorator_start = start_char
        for child in node.children:
            if child.type == "decorator":
                d_start = byte_to_char[child.start_byte]
                if d_start < earliest_decorator_start:
                    earliest_decorator_start = d_start

        entries.append(
            ScopeEntry(
                start=earliest_decorator_start,
                end=end_char,
                symbol_type="class",
                fn_name="",
                class_name=name,
            )
        )
        for child in node.children:
            if child.type == "class_body":
                for grandchild in child.children:
                    _walk_node(grandchild, source, byte_to_char, entries, ext, current_class=name)
        return

    # TypeScript/JavaScript method definitions within a class
    if node_type in {"method_definition", "method_signature"}:
        name_node = _find_name_child(node)
        name = name_node.text.decode("utf-8") if name_node else ""
        start_char = byte_to_char[node.start_byte]
        end_char = byte_to_char[node.end_byte]
        entries.append(
            ScopeEntry(
                start=start_char,
                end=end_char,
                symbol_type="method",
                fn_name=name,
                class_name=current_class,
            )
        )
        return

    # TypeScript/JavaScript export_statement: recurse to find declarations inside
    if node_type == "export_statement":
        for child in node.children:
            _walk_node(child, source, byte_to_char, entries, ext, current_class)
        return

    # Arrow functions (TS `arrow_function` assigned to a const) are NOT captured
    # as scope entries — they fall through to the enclosing scope. Skip them.
    if node_type == "arrow_function":
        return

    # Default: recurse into children
    for child in node.children:
        _walk_node(child, source, byte_to_char, entries, ext, current_class)


def _find_name_child(node: Any) -> Any | None:
    """Return the first 'identifier' or 'property_identifier' child of *node*."""
    for child in node.children:
        if child.type in {"identifier", "property_identifier", "type_identifier"}:
            return child
    return None


def _find_body_child(node: Any) -> Any | None:
    """Return the block/body child of a function or class node."""
    for child in node.children:
        if child.type in {"block", "statement_block", "class_body"}:
            return child
    return None


# ---------------------------------------------------------------------------
# Scope resolver
# ---------------------------------------------------------------------------


def _resolve_scope(offset: int, scope_table: ScopeTable) -> ScopeEntry | None:
    """Find the innermost scope containing *offset*.

    Uses binary search (``bisect_right``) on ``start`` fields to find the
    rightmost entry whose ``start <= offset``, then walks backwards to find
    the first entry whose ``end > offset``.

    The scope table is sorted by ``(start ASC, end DESC)`` so that at equal
    starts, the outermost scope (largest ``end``) appears first. Walking
    backwards from the bisect point therefore reaches the innermost scope
    (smallest ``end`` at a given ``start``) first.

    Returns ``None`` if no scope contains *offset* (module-level code).
    """
    import bisect  # noqa: PLC0415 (stdlib, always available)

    starts = [e.start for e in scope_table]
    idx = bisect.bisect_right(starts, offset)
    # Walk backwards from idx-1
    for i in range(idx - 1, -1, -1):
        entry = scope_table[i]
        if entry.end > offset:
            return entry
    return None


# ---------------------------------------------------------------------------
# Language label
# ---------------------------------------------------------------------------


def _lang_label(ext: str) -> str:
    """Map a file extension to a human-readable language name.

    Raises ``KeyError`` for unknown extensions — the caller should handle this
    (e.g. by catching it and using the raw extension as a fallback).
    """
    _MAP = {
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".sh": "bash",
    }
    return _MAP[ext]


# ---------------------------------------------------------------------------
# Public CodeEnricher class
# ---------------------------------------------------------------------------


class CodeEnricher:
    """AST-based code chunk enricher.

    Follows the same ``prepare()`` / ``enrich_chunk()`` two-pass protocol as
    :class:`archon_search.enricher.MarkdownEnricher`.

    Instances are NOT reusable across files. Create a new instance per
    ``ingest_file()`` call. ``_module_path_value`` and ``_ext`` are set during
    ``prepare()``; calling ``enrich_chunk()`` before ``prepare()`` will return
    stale or empty data.
    """

    def __init__(self) -> None:
        self._module_path_value: str = ""
        self._ext: str = ""

    def prepare(
        self,
        text: str,
        ext: str,
        file_path: Path,
        collection_root: Path | None,
    ) -> ScopeTable:
        """Parse *text* with tree-sitter; return a :data:`ScopeTable`.

        Stores :attr:`_module_path_value` and :attr:`_ext` as instance state.
        Returns an empty list if the grammar is unavailable or if a
        catastrophic scope-builder failure occurs (WARNING logged in the
        latter case).

        Note: tree-sitter does NOT raise on broken syntax — ERROR nodes are
        processed normally. The exception path is for catastrophic failures
        such as null language objects or library crashes.
        """
        self._ext = ext
        self._module_path_value = _module_path(file_path, collection_root)

        lang = _get_grammar(ext)
        if lang is None:
            return []

        try:
            return _build_scope_table(text, lang, ext)
        except Exception as exc:
            count = _parse_failure_count.get(ext, 0) + 1
            _parse_failure_count[ext] = count
            if count <= _PARSE_FAILURE_CAP:
                logger.warning("tree-sitter parse failed for %s: %s", file_path, exc)
            else:
                logger.debug("tree-sitter parse failed for %s: %s", file_path, exc)
            return []

    def enrich_chunk(
        self,
        chunk: "ChunkRecord",
        scope_table: ScopeTable,
    ) -> dict[str, str]:
        """Resolve the innermost scope for *chunk* and return metadata.

        Returns:
            - Full 5-field dict on success.
            - ``{"_module_path": ...}`` if *scope_table* is empty but
              module path is known (e.g. grammar missing or parse failed).
            - ``{}`` if *scope_table* is empty and no module path available.
        """
        if not scope_table:
            if self._module_path_value:
                return {"_module_path": self._module_path_value}
            return {}

        offset = chunk.start_offset
        # Guard for sentinel offsets (no offset information)
        if offset < 0:
            scope = None
        else:
            scope = _resolve_scope(offset, scope_table)

        try:
            language = _lang_label(self._ext)
        except KeyError:
            language = self._ext.lstrip(".")

        if scope is None:
            # Module-level code (between functions, at file top/bottom)
            return {
                "_symbol_type": "module",
                "_containing_function": "",
                "_containing_class": "",
                "_module_path": self._module_path_value,
                "_symbol_subtype": f"{language}-module",
            }

        return {
            "_symbol_type": scope.symbol_type,
            "_containing_function": scope.fn_name,
            "_containing_class": scope.class_name,
            "_module_path": self._module_path_value,
            "_symbol_subtype": f"{language}-{scope.symbol_type}",
        }
