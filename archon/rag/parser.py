"""Document parser — extracts plain text from various file formats.

Supported formats:
- Plain text: .md, .txt, .py, .js, .ts, .go, .rs, .java, .sh,
              .yaml, .yml, .json, .toml, .csv (and any unknown extension)
- HTML: .html, .htm — via trafilatura
- PDF: .pdf — via docling (lazy import)
- Office: .docx, .pptx, .xlsx — via markitdown (lazy import)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import trafilatura


class ParseError(Exception):
    """Raised when a document cannot be parsed."""

    def __init__(self, path: Path, cause: Exception) -> None:
        super().__init__(f"Failed to parse {path}: {cause}")
        self.path = path
        self.cause = cause


_PLAIN_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".go", ".rs", ".java", ".sh",
    ".yaml", ".yml", ".json", ".toml", ".csv",
}
_HTML_EXTENSIONS = {".html", ".htm"}
_PDF_EXTENSIONS = {".pdf"}
_OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}


class DocumentParser:
    """Parses documents into plain text, routing by file extension."""

    async def parse(self, path: Path) -> str:
        """Parse *path* and return its text content.

        All CPU-bound work runs in a thread via asyncio.to_thread().
        Raises ParseError on failure.
        """
        suffix = path.suffix.lower()
        if suffix in _HTML_EXTENSIONS:
            fn = self._parse_html
        elif suffix in _PDF_EXTENSIONS:
            fn = self._parse_pdf
        elif suffix in _OFFICE_EXTENSIONS:
            fn = self._parse_office
        else:
            fn = self._parse_plain

        try:
            return await asyncio.to_thread(fn, path)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(path, exc) from exc

    # ------------------------------------------------------------------
    # Format handlers
    # ------------------------------------------------------------------

    def _parse_plain(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_html(self, path: Path) -> str:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            extracted = trafilatura.extract(raw, include_tables=True, include_links=False)
            if extracted is None:
                return raw
            return extracted
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_pdf(self, path: Path) -> str:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import]
            return DocumentConverter().convert(str(path)).document.export_to_markdown()
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_office(self, path: Path) -> str:
        try:
            from markitdown import MarkItDown  # type: ignore[import]
            return MarkItDown().convert(str(path)).text_content
        except Exception as exc:
            raise ParseError(path, exc) from exc
