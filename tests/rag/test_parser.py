"""tests/rag/test_parser.py — unit tests for DocumentParser."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon.rag.parser import DocumentParser, ParseError


@pytest.mark.asyncio
async def test_parser_md_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("# Hello\nworld")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "Hello" in result


@pytest.mark.asyncio
async def test_parser_txt_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("plain text content")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "plain text content" in result


@pytest.mark.asyncio
async def test_parser_unknown_extension_falls_back_to_plain(tmp_path: Path) -> None:
    f = tmp_path / "doc.xyz"
    f.write_text("unknown ext content")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "unknown ext content" in result


@pytest.mark.asyncio
async def test_parser_html_calls_trafilatura(tmp_path: Path) -> None:
    f = tmp_path / "page.html"
    f.write_text("<html><body><p>hello</p></body></html>")
    parser = DocumentParser()
    mock_trafilatura = MagicMock()
    mock_trafilatura.extract.return_value = "extracted html"
    with patch.dict("sys.modules", {"trafilatura": mock_trafilatura}):
        result = await parser.parse(f)
    mock_trafilatura.extract.assert_called_once()
    assert result == "extracted html"


@pytest.mark.asyncio
async def test_parser_html_trafilatura_returns_none_falls_back(tmp_path: Path) -> None:
    """If trafilatura.extract returns None, fall back to plain read."""
    f = tmp_path / "page.html"
    f.write_text("<html><body>fallback</body></html>")
    parser = DocumentParser()
    mock_trafilatura = MagicMock()
    mock_trafilatura.extract.return_value = None
    with patch.dict("sys.modules", {"trafilatura": mock_trafilatura}):
        result = await parser.parse(f)
    assert "fallback" in result


@pytest.mark.asyncio
async def test_parser_pdf_calls_docling(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = (
        "# PDF content"
    )
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {
            "docling": mock_docling,
            "docling.document_converter": mock_docling,
        },
    ):
        result = await parser.parse(f)

    mock_converter.return_value.convert.assert_called_once()
    assert "PDF content" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".docx", ".pptx", ".xlsx"])
async def test_parser_office_calls_markitdown(ext: str, tmp_path: Path) -> None:
    f = tmp_path / f"doc{ext}"
    f.write_bytes(b"fake office doc")
    parser = DocumentParser()

    mock_md_instance = MagicMock()
    mock_md_instance.convert.return_value.text_content = "office content"
    mock_md_cls = MagicMock(return_value=mock_md_instance)
    mock_markitdown = MagicMock()
    mock_markitdown.MarkItDown = mock_md_cls

    with patch.dict("sys.modules", {"markitdown": mock_markitdown}):
        result = await parser.parse(f)

    mock_md_instance.convert.assert_called_once()
    assert result == "office content"


@pytest.mark.asyncio
async def test_parser_unreadable_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "unreadable.txt"
    f.write_text("data")
    f.chmod(0o000)  # remove read permission
    parser = DocumentParser()
    try:
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)
        assert exc_info.value.path == f
        assert isinstance(exc_info.value.cause, Exception)
    finally:
        f.chmod(0o644)  # restore for cleanup


@pytest.mark.asyncio
async def test_parser_parse_error_has_path_and_cause(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"bad pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = RuntimeError("corrupt pdf")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)
    assert exc_info.value.path == f
    assert "corrupt pdf" in str(exc_info.value.cause)
