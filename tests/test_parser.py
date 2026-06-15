"""packages/archon-search/tests/test_parser.py — unit tests for DocumentParser."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.parser import DocumentParser, ParseError


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
async def test_parser_image_calls_docling(tmp_path: Path) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"fake png")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "extracted text"
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        result = await parser.parse(f)

    mock_converter.return_value.convert.assert_called_once_with(str(f))
    assert result == "extracted text"


@pytest.mark.asyncio
async def test_parser_image_empty_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "image.jpg"
    f.write_bytes(b"fake jpg")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "   \n  "
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_image_none_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "image.jpeg"
    f.write_bytes(b"fake jpeg")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = None
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_image_docling_failure_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "image.tiff"
    f.write_bytes(b"fake tiff")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = RuntimeError("ocr failed")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert exc_info.value.path == f
    assert isinstance(exc_info.value.cause, RuntimeError)
    assert "ocr failed" in str(exc_info.value.cause)


@pytest.mark.asyncio
async def test_parser_image_corrupt_file_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "corrupt.png"
    f.write_bytes(b"")  # zero-byte file
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = ValueError("invalid image")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert exc_info.value.path == f
    assert isinstance(exc_info.value.cause, ValueError)


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"])
async def test_parser_all_image_extensions_routed(ext: str, tmp_path: Path) -> None:
    f = tmp_path / f"image{ext}"
    f.write_bytes(b"fake image")
    parser = DocumentParser()

    with patch.object(parser, "_parse_image", return_value="image text") as mock_parse_image:
        result = await parser.parse(f)

    mock_parse_image.assert_called_once_with(f)
    assert result == "image text"


@pytest.mark.asyncio
async def test_parser_converter_reused_across_calls(tmp_path: Path) -> None:
    f1 = tmp_path / "image1.png"
    f2 = tmp_path / "image2.png"
    f1.write_bytes(b"fake png 1")
    f2.write_bytes(b"fake png 2")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "text"
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        await parser.parse(f1)
        await parser.parse(f2)

    mock_converter.assert_called_once()


@pytest.mark.asyncio
async def test_parser_pdf_none_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = None
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_pdf_whitespace_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "  \n  "
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        result = await parser.parse(f)

    assert result == ""


# ---------------------------------------------------------------------------
# Task 1.2 — page_break_placeholder kwarg tests
# ---------------------------------------------------------------------------


def test_parse_with_docling_kwarg_passes_page_break_marker(
    tmp_path: Path,
) -> None:
    """Verify _parse_with_docling passes page_break_placeholder=PAGE_BREAK_MARKER to docling.

    Uses monkeypatch-style sys.modules patching to capture kwargs without real docling.
    Independent of fixture availability.
    """
    from archon_search.enricher import PAGE_BREAK_MARKER

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    captured_kwargs: dict = {}

    mock_document = MagicMock()

    def capture_export(**kwargs: object) -> str:
        captured_kwargs.update(kwargs)
        return "page1 content"

    mock_document.export_to_markdown.side_effect = capture_export
    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document = mock_document
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict(
        "sys.modules",
        {"docling": mock_docling, "docling.document_converter": mock_docling},
    ):
        parser._parse_with_docling(f)

    assert "page_break_placeholder" in captured_kwargs, (
        f"Expected page_break_placeholder kwarg, got: {captured_kwargs}"
    )
    assert captured_kwargs["page_break_placeholder"] == PAGE_BREAK_MARKER, (
        f"Expected PAGE_BREAK_MARKER, got: {captured_kwargs['page_break_placeholder']!r}"
    )


@pytest.mark.integration
@pytest.mark.xdist_group("docling")
def test_parse_with_docling_emits_page_marker(three_page_pdf: Path) -> None:
    """Integration: parser output from a real three-page PDF contains at least one PAGE_BREAK_MARKER.

    Requires the three_page_pdf fixture from Task 5.1.
    Skipped if docling is unavailable or non-functional in the test environment.

    NOTE: The fixture PDF has 3 pages. Docling may emit 1 or 2 page break markers
    depending on how it segments the very short page content — short pages may be
    merged into a single section, resulting in fewer markers than (pages - 1). We
    assert >= 1 (at least one page boundary detected) rather than == 2 to avoid
    tying the test to docling's internal segmentation heuristics.
    """
    pytest.importorskip("docling")

    from archon_search.enricher import PAGE_BREAK_MARKER
    from archon_search.parser import ParseError

    parser = DocumentParser()
    try:
        result = parser._parse_with_docling(three_page_pdf)
    except ParseError as exc:
        pytest.skip(f"docling not functional in this environment: {exc}")

    marker_count = result.count(PAGE_BREAK_MARKER)
    assert marker_count >= 1, (
        f"Expected at least 1 occurrence of PAGE_BREAK_MARKER in parsed output "
        f"from three-page PDF, got {marker_count}.\nParsed output:\n{result[:500]}"
    )


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
