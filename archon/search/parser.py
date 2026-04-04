"""Document parser — extracts plain text from various file formats.

Supported formats:
- Plain text: .md, .txt, .py, .js, .ts, .go, .rs, .java, .sh,
              .yaml, .yml, .json, .toml, .csv (and any unknown extension)
- HTML: .html, .htm — via trafilatura
- PDF: .pdf — via docling (lazy import)
- Office: .docx, .pptx, .xlsx — via markitdown (lazy import)
- Images: .png, .jpg, .jpeg, .tiff, .tif, .bmp, .webp — via docling OCR
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter


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
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
# .gif and .svg are intentionally excluded: .gif has animated frames (OCR on frame 0 is
# misleading) and .svg is XML text (plain-text fallback is more appropriate).


class DocumentParser:
    """Parses documents into plain text, routing by file extension."""

    def __init__(self) -> None:
        self._converter: DocumentConverter | None = None  # lazy-initialised on first docling call

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
        elif suffix in _IMAGE_EXTENSIONS:
            fn = self._parse_image
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
            import trafilatura  # lazy: optional search extra
            raw = path.read_text(encoding="utf-8", errors="replace")
            extracted = trafilatura.extract(raw, include_tables=True, include_links=False)
            if extracted is None:
                return raw
            return extracted
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_with_docling(self, path: Path) -> str:
        # NOT THREAD SAFE: self._converter lazy init has a check-then-set race if
        # multiple threads call this concurrently (e.g. asyncio.gather over images).
        # Current RAG pipeline is sequential, so this is an accepted limitation.
        # If parallel ingestion is added, guard with threading.Lock.
        try:
            from docling.document_converter import DocumentConverter  # noqa: PLC0415
            if self._converter is None:
                self._converter = DocumentConverter()
            result = self._converter.convert(str(path)).document.export_to_markdown()
            return result.strip() if result else ""
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_pdf(self, path: Path) -> str:
        return self._parse_with_docling(path)

    def _parse_image(self, path: Path) -> str:
        return self._parse_with_docling(path)

    def _parse_office(self, path: Path) -> str:
        try:
            from markitdown import MarkItDown  # noqa: PLC0415
            return MarkItDown().convert(str(path)).text_content
        except Exception as exc:
            raise ParseError(path, exc) from exc
