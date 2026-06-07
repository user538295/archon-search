"""tests/_pdf_fixture.py — shared PDF generation helper for conftest fixtures.

Factored out so both tests/conftest.py (unit/integration fixture) and
tests/eval/conftest.py (eval-corpus fixture) can generate the same
three-page PDF without duplicating code.

NOTE: The generated PDF is NOT byte-deterministic across sessions —
reportlab embeds the current timestamp in CreationDate/ModDate regardless
of setCreator/setProducer. Only the textual content is stable.
Tests must rely on textual assertions, not byte-hash assertions.
"""
from __future__ import annotations

from pathlib import Path


def generate_three_page_pdf(target: Path) -> None:
    """Generate a three-page PDF at *target* with pinned page content.

    Pages contain exactly:
    - Page 1: "alpha content"
    - Page 2: "beta content"
    - Page 3: "gamma content"

    The parent directory is created if it does not already exist.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    from reportlab.pdfgen.canvas import Canvas  # noqa: PLC0415

    c = Canvas(str(target), pagesize=(612, 792))
    c.setCreator("archon-search-test")
    c.setProducer("archon-search-test")
    c.drawString(100, 700, "alpha content")
    c.showPage()
    c.drawString(100, 700, "beta content")
    c.showPage()
    c.drawString(100, 700, "gamma content")
    c.showPage()
    c.save()
