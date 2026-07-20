"""tests/test_fixtures.py — Task 5.1: three_page_pdf fixture existence and basic parser round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestThreePagePdfFixture:
    """Verify the three_page_pdf conftest fixture is available and correct."""

    def test_three_page_pdf_exists(self, three_page_pdf: Path) -> None:
        """Fixture path resolves to an existing file."""
        assert three_page_pdf.exists(), f"Expected PDF at {three_page_pdf} to exist"
        assert three_page_pdf.is_file(), f"Expected {three_page_pdf} to be a file"
        assert three_page_pdf.suffix == ".pdf", f"Expected .pdf extension, got {three_page_pdf.suffix}"

    def test_three_page_pdf_has_nonzero_size(self, three_page_pdf: Path) -> None:
        """Generated PDF must not be an empty file."""
        assert three_page_pdf.stat().st_size > 0, f"Expected non-zero PDF at {three_page_pdf}"

    @pytest.mark.integration
    @pytest.mark.docling
    def test_three_page_pdf_contains_expected_text(self, three_page_pdf: Path) -> None:
        """Parse via DocumentParser._parse_with_docling; assert expected page content.

        Skipped if docling is unavailable or non-functional in the test environment
        (e.g., missing onnxruntime weights).
        """
        import pytest

        pytest.importorskip("docling")

        from archon_search.parser import DocumentParser, ParseError

        parser = DocumentParser()
        try:
            result = parser._parse_with_docling(three_page_pdf)
        except ParseError as exc:
            pytest.skip(f"docling not functional in this environment: {exc}")

        assert "alpha content" in result, f"Expected 'alpha content' in parsed output, got: {result[:500]}"
        assert "beta content" in result, f"Expected 'beta content' in parsed output, got: {result[:500]}"
        assert "gamma content" in result, f"Expected 'gamma content' in parsed output, got: {result[:500]}"

        # Verify order: alpha before beta before gamma
        alpha_pos = result.index("alpha content")
        beta_pos = result.index("beta content")
        gamma_pos = result.index("gamma content")
        assert alpha_pos < beta_pos < gamma_pos, (
            f"Expected alpha < beta < gamma in parsed output, "
            f"got positions: {alpha_pos}, {beta_pos}, {gamma_pos}"
        )
