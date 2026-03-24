"""Tests for archon/rag/parser.py — DocumentParser."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon.rag.parser import DocumentParser, ParseError


# ---------------------------------------------------------------------------
# Plain-text formats
# ---------------------------------------------------------------------------


def test_parser_md_returns_content(tmp_path: Path) -> None:
    md_file = tmp_path / "readme.md"
    md_file.write_text("# Hello\nWorld", encoding="utf-8")
    parser = DocumentParser()
    result = parser._parse_plain(md_file)
    assert result == "# Hello\nWorld"


def test_parser_txt_returns_content(tmp_path: Path) -> None:
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("plain text content", encoding="utf-8")
    parser = DocumentParser()
    result = parser._parse_plain(txt_file)
    assert result == "plain text content"


@pytest.mark.asyncio
async def test_parser_md_async_returns_content(tmp_path: Path) -> None:
    md_file = tmp_path / "doc.md"
    md_file.write_text("async markdown", encoding="utf-8")
    parser = DocumentParser()
    result = await parser.parse(md_file)
    assert result == "async markdown"


@pytest.mark.asyncio
async def test_parser_txt_async_returns_content(tmp_path: Path) -> None:
    txt_file = tmp_path / "file.txt"
    txt_file.write_text("async text", encoding="utf-8")
    parser = DocumentParser()
    result = await parser.parse(txt_file)
    assert result == "async text"


# ---------------------------------------------------------------------------
# Unknown extension — falls back to plain read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_unknown_extension_falls_back_to_plain(tmp_path: Path) -> None:
    xyz_file = tmp_path / "data.xyz"
    xyz_file.write_text("unknown format content", encoding="utf-8")
    parser = DocumentParser()
    result = await parser.parse(xyz_file)
    assert result == "unknown format content"


# ---------------------------------------------------------------------------
# HTML — trafilatura
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_html_calls_trafilatura(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body><p>Hello</p></body></html>", encoding="utf-8")

    mock_extract = MagicMock(return_value="Hello")
    monkeypatch.setattr("archon.rag.parser.trafilatura.extract", mock_extract)

    parser = DocumentParser()
    result = await parser.parse(html_file)

    mock_extract.assert_called_once()
    assert result == "Hello"


@pytest.mark.asyncio
async def test_parser_html_trafilatura_returns_none_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_file = tmp_path / "empty.html"
    html_content = "<html><body></body></html>"
    html_file.write_text(html_content, encoding="utf-8")

    monkeypatch.setattr("archon.rag.parser.trafilatura.extract", MagicMock(return_value=None))

    parser = DocumentParser()
    result = await parser.parse(html_file)

    # Falls back to plain read
    assert result == html_content


# ---------------------------------------------------------------------------
# PDF — docling (not installed; must be monkeypatched)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_pdf_calls_docling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")

    # Patch sys.modules so lazy import inside _parse_pdf gets the mock
    fake_doc = MagicMock()
    fake_doc.export_to_markdown.return_value = "pdf markdown"

    fake_converter_instance = MagicMock()
    fake_converter_instance.convert.return_value = MagicMock(document=fake_doc)

    fake_dc_class = MagicMock(return_value=fake_converter_instance)
    fake_dc_module = MagicMock()
    fake_dc_module.DocumentConverter = fake_dc_class

    monkeypatch.setitem(sys.modules, "docling", MagicMock())
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_dc_module)

    parser = DocumentParser()
    result = await parser.parse(pdf_file)

    fake_converter_instance.convert.assert_called_once_with(str(pdf_file))
    assert result == "pdf markdown"


# ---------------------------------------------------------------------------
# Office — markitdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_office_calls_markitdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docx_file = tmp_path / "report.docx"
    docx_file.write_bytes(b"fake docx bytes")

    fake_result = MagicMock()
    fake_result.text_content = "office content"

    fake_md_instance = MagicMock()
    fake_md_instance.convert.return_value = fake_result

    fake_md_class = MagicMock(return_value=fake_md_instance)
    fake_md_module = MagicMock()
    fake_md_module.MarkItDown = fake_md_class

    monkeypatch.setitem(sys.modules, "markitdown", fake_md_module)

    parser = DocumentParser()
    result = await parser.parse(docx_file)

    fake_md_instance.convert.assert_called_once_with(str(docx_file))
    assert result == "office content"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_unreadable_raises_parse_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_file = tmp_path / "secret.txt"
    bad_file.write_text("content", encoding="utf-8")

    def _raise_permission(*args: object, **kwargs: object) -> str:
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "read_text", _raise_permission)

    parser = DocumentParser()
    with pytest.raises(ParseError) as exc_info:
        await parser.parse(bad_file)

    assert exc_info.value.path == bad_file
    assert isinstance(exc_info.value.cause, PermissionError)


def test_parse_error_stores_path_and_cause() -> None:
    path = Path("/some/file.txt")
    cause = ValueError("bad value")
    err = ParseError(path, cause)
    assert err.path == path
    assert err.cause == cause
