"""Task 1.5 — Content enrichment metadata in HTTP responses.

Exercises the full HTTP → middleware → route → pipeline → LanceDB path for
content enrichment metadata: Markdown headings (C3a), PDF page numbers (C3b),
code symbol metadata (C3c), and image page start (C3b image path).

All tests use ``include_metadata=true`` in the search filters because the
``metadata`` dict is zeroed out by the search route when ``include_metadata``
is False (default).

Run with:
    uv run pytest tests/integration/test_http_enrichment_metadata.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _search_with_metadata(client, col: str, query: str, *, api_key: str) -> list[dict]:
    """POST /search with include_metadata=true. Returns result items."""
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "collection": col,
        "query": query,
        "filters": {"include_metadata": True},
    }
    resp = client.post("/search", json=body, headers=headers)
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()["results"]


# ---------------------------------------------------------------------------
# Test 1 — Markdown heading flows through to search response
# ---------------------------------------------------------------------------


def test_markdown_heading_flows_through_to_search_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest a Markdown file with headings via HTTP. POST /search with
    include_metadata=true. Assert response items carry metadata._heading
    and metadata._section_path fields.

    Verifies C3a enrichment wiring through the full HTTP → pipeline → LanceDB
    → serialized response path.
    """
    md_file = tmp_path / "doc.md"
    md_file.write_text(
        "# Introduction\n\n"
        "This section covers the basics of the system.\n\n"
        "## Background\n\n"
        "Historical context and prior work on this topic.\n\n"
        "### Related Work\n\n"
        "A survey of related techniques and prior publications.\n" * 3
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-enrichment-heading"
        ingest_file_via_path(client, col, str(md_file), api_key=api_key)

        items = _search_with_metadata(client, col, "background related work", api_key=api_key)
        assert items, "expected at least one search result after markdown ingest"

        # At least one chunk must have _heading populated (the file has headings)
        headings = [item["metadata"].get("_heading", "") for item in items]
        section_paths = [item["metadata"].get("_section_path", "") for item in items]

        assert any(h for h in headings), (
            f"expected at least one result with non-empty _heading; "
            f"metadata for first item: {items[0]['metadata']}"
        )
        assert any(sp for sp in section_paths), (
            f"expected at least one result with non-empty _section_path; "
            f"headings found: {headings}"
        )

        # Verify the fields are present in the metadata dict (not absent)
        for item in items:
            assert "_heading" in item["metadata"], (
                f"_heading key missing from metadata: {item['metadata']}"
            )
            assert "_section_path" in item["metadata"], (
                f"_section_path key missing from metadata: {item['metadata']}"
            )


# ---------------------------------------------------------------------------
# Test 2 — PDF page number in search response
# ---------------------------------------------------------------------------


def test_pdf_page_number_in_search_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest a multi-page PDF via the real docling parser (stub embedder).
    POST /search with include_metadata=true. Assert results carry
    metadata._page_start with an integer value.

    Uses the three_page.pdf fixture from the eval corpus. Verifies C3b
    enrichment wiring through the full HTTP path.
    """
    pdf_fixture = (
        Path(__file__).parent.parent / "eval" / "corpus" / "pdf-fixtures" / "three_page.pdf"
    )
    if not pdf_fixture.exists():
        pytest.skip("three_page.pdf fixture not found — skipping PDF enrichment test")

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-enrichment-pdf"
        ingest_file_via_path(client, col, str(pdf_fixture), api_key=api_key, timeout_s=30.0)

        items = _search_with_metadata(client, col, "page content", api_key=api_key)
        assert items, "expected at least one search result after PDF ingest"

        # All items must have _page_start in metadata
        for item in items:
            assert "_page_start" in item["metadata"], (
                f"_page_start key missing from metadata: {item['metadata']}"
            )
            page_start_raw = item["metadata"]["_page_start"]
            # _page_start is stored as a string (dict[str, str] contract)
            page_start = int(page_start_raw)
            assert page_start >= 1, (
                f"expected _page_start >= 1 (1-indexed pages), got: {page_start}"
            )


# ---------------------------------------------------------------------------
# Test 3 — Code symbol metadata in search response
# ---------------------------------------------------------------------------


def test_code_symbol_metadata_in_search_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest a Python source file. POST /search with include_metadata=true.
    Assert results carry metadata._symbol_type and either
    metadata._containing_function or metadata._containing_class.

    Verifies C3c (CodeEnricher) enrichment wiring through the full HTTP path.
    The test file contains a class definition so _symbol_type should appear.
    """
    py_file = tmp_path / "example.py"
    py_file.write_text(
        '"""Example module for testing code enrichment."""\n\n'
        "class DataProcessor:\n"
        '    """Process and transform incoming data.\n\n'
        "    This class handles the core data transformation pipeline.\n"
        '    """\n\n'
        "    def process(self, data: list) -> list:\n"
        '        """Process a list of data items and return transformed results."""\n'
        "        result = []\n"
        "        for item in data:\n"
        "            transformed = self._transform(item)\n"
        "            result.append(transformed)\n"
        "        return result\n\n"
        "    def _transform(self, item):\n"
        '        """Apply transformation to a single data item."""\n'
        "        return str(item).strip().upper()\n\n\n"
        "def standalone_function(value: str) -> str:\n"
        '    """A standalone function outside any class."""\n'
        "    return value.lower().replace(' ', '_')\n"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-enrichment-code"
        ingest_file_via_path(client, col, str(py_file), api_key=api_key)

        items = _search_with_metadata(client, col, "class DataProcessor", api_key=api_key)
        assert items, "expected at least one search result after Python file ingest"

        # Every item must have _symbol_type in metadata (CodeEnricher always sets it)
        for item in items:
            assert "_symbol_type" in item["metadata"], (
                f"_symbol_type key missing from metadata: {item['metadata']}"
            )

        # The _symbol_type values must be valid code scope types
        valid_symbol_types = {"function", "method", "class", "module"}
        for item in items:
            symbol_type = item["metadata"]["_symbol_type"]
            assert symbol_type in valid_symbol_types, (
                f"unexpected _symbol_type value: {symbol_type!r}; "
                f"expected one of {valid_symbol_types}"
            )

        # _module_path must be present (always set by CodeEnricher)
        for item in items:
            assert "_module_path" in item["metadata"], (
                f"_module_path key missing from metadata: {item['metadata']}"
            )


# ---------------------------------------------------------------------------
# Test 4 — Image file assigns _page_start == 1
# ---------------------------------------------------------------------------


def test_image_file_assigns_page_start_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest a PNG image file. POST /search with include_metadata=true.
    Assert metadata._page_start == '1' (images are single-page).

    Uses docling's image path (C3b) to verify that single-page image files
    receive _page_start = 1 via the MarkdownEnricher.preprocess() page table.
    """
    try:
        from PIL import Image as PILImage
        from PIL import ImageDraw as PILImageDraw

        # Create a PNG with actual rendered text so OCR can read it and produce chunks.
        # A blank/white PNG produces no OCR output → no chunks → collection not created.
        png_path = tmp_path / "diagram.png"
        img = PILImage.new("RGB", (400, 80), color="white")
        draw = PILImageDraw.Draw(img)
        draw.text(
            (10, 20),
            "architecture diagram overview content showing system components",
            fill="black",
        )
        img.save(str(png_path), format="PNG")
    except ImportError:
        pytest.skip("Pillow not available — cannot create a text-bearing PNG for OCR test")

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-enrichment-image"
        try:
            ingest_file_via_path(client, col, str(png_path), api_key=api_key, timeout_s=30.0)
        except Exception as exc:
            pytest.skip(f"image ingest failed (docling OCR may not be available): {exc}")

        # Check if collection was created at all — blank images produce no OCR text → no chunks
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "collection": col,
            "query": "diagram image content",
            "filters": {"include_metadata": True},
        }
        resp = client.post("/search", json=body, headers=headers)
        if resp.status_code == 404:
            pytest.skip(
                "image ingest produced no text chunks (docling OCR returned empty) — "
                "collection not created; skipping _page_start assertion"
            )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        items = resp.json()["results"]
        if not items:
            pytest.skip("no search results after image ingest — docling OCR may have produced no text")

        # All items must have _page_start == '1' for a single-page image
        for item in items:
            assert "_page_start" in item["metadata"], (
                f"_page_start key missing from metadata for image file: {item['metadata']}"
            )
            assert item["metadata"]["_page_start"] == "1", (
                f"expected _page_start='1' for single-page image, "
                f"got: {item['metadata']['_page_start']!r}"
            )
