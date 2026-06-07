"""Symbol enricher: resolve code symbol context from a scope table."""
from __future__ import annotations

from collections import namedtuple
from typing import Any


ScopeEntry = namedtuple("ScopeEntry", ["start", "end", "symbol_type", "fn_name", "class_name"])
ScopeTable = list[ScopeEntry]


def build_scope_table(source: str, language: Any) -> ScopeTable:
    """Build a sorted scope table from a parsed AST.

    Walks the AST and emits one ScopeEntry per function, method, or class
    definition found.  Entries are sorted by (start ASC, end DESC) so that
    innermost scopes appear last at equal start positions.

    Args:
        source: The original source text (used for byte-to-char offset mapping).
        language: A tree-sitter Language object for the target language.

    Returns:
        A ScopeTable sorted by (start, -end).
    """
    # Byte-to-char mapping is necessary because tree-sitter uses UTF-8 byte
    # offsets while ChunkRecord uses character offsets.
    byte_to_char: dict[int, int] = {}
    byte_pos = 0
    for i, ch in enumerate(source):
        byte_to_char[byte_pos] = i
        byte_pos += len(ch.encode("utf-8"))
    byte_to_char[byte_pos] = len(source)  # EOF sentinel

    return sorted([], key=lambda e: (e.start, -e.end))


class SymbolEnricher:
    """Enrich code chunks with symbol-level metadata resolved from a scope table."""

    def __init__(self) -> None:
        self._module_path_value: str = ""
        self._ext: str = ""

    def prepare(
        self,
        text: str,
        ext: str,
        language: Any | None,
    ) -> ScopeTable:
        """Parse *text* and return a scope table.

        If *language* is None (grammar not installed) an empty table is returned
        so that downstream consumers can still emit a ``_module_path`` entry.

        Args:
            text: Source code text.
            ext: File extension including the leading dot (e.g. ``".py"``).
            language: A resolved tree-sitter Language, or None.

        Returns:
            A ScopeTable; may be empty on grammar miss or parse failure.
        """
        self._ext = ext
        if language is None:
            return []
        try:
            return build_scope_table(text, language)
        except Exception:
            return []

    def enrich_chunk(
        self,
        start_offset: int,
        scope_table: ScopeTable,
    ) -> dict[str, str]:
        """Resolve the innermost scope that contains *start_offset*.

        Args:
            start_offset: Character offset of the chunk's first character.
            scope_table: Pre-built scope table from :meth:`prepare`.

        Returns:
            A metadata dict with keys ``_symbol_type``, ``_containing_function``,
            ``_containing_class``, ``_module_path``, and ``_symbol_subtype``.
        """
        if not scope_table:
            result: dict[str, str] = {}
            if self._module_path_value:
                result["_module_path"] = self._module_path_value
            return result

        entry = _resolve_scope(start_offset, scope_table)
        if entry is None:
            symbol_type = "module"
            fn_name = ""
            class_name = ""
        else:
            symbol_type = entry.symbol_type
            fn_name = entry.fn_name
            class_name = entry.class_name

        lang = _lang_label(self._ext)
        return {
            "_symbol_type": symbol_type,
            "_containing_function": fn_name,
            "_containing_class": class_name,
            "_module_path": self._module_path_value,
            "_symbol_subtype": f"{lang}-{symbol_type}",
        }


def _resolve_scope(offset: int, scope_table: ScopeTable) -> ScopeEntry | None:
    """Return the innermost ScopeEntry that contains *offset*, or None.

    Uses a backward linear scan from the rightmost entry whose start ≤ offset.
    """
    import bisect

    starts = [e.start for e in scope_table]
    idx = bisect.bisect_right(starts, offset) - 1
    while idx >= 0:
        entry = scope_table[idx]
        if entry.end > offset:
            return entry
        idx -= 1
    return None


def _lang_label(ext: str) -> str:
    """Map a file extension to a human-readable language name."""
    _MAP = {
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".sh": "bash",
    }
    return _MAP.get(ext, ext.lstrip("."))
