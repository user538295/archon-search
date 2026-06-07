"""tests/_pdf_fixture.py — shared PDF generation helper for conftest fixtures.

Factored out so both tests/conftest.py (unit/integration fixture) and
tests/eval/conftest.py (eval-corpus fixture) can generate the same
three-page PDF without duplicating code.

The generated PDF is byte-deterministic across sessions when ``invariant=True``
is passed to reportlab Canvas (suppresses timestamps in CreationDate/ModDate).
This is required for the eval corpus: compute_eval_hash hashes corpus file
bytes, so a non-deterministic PDF would produce a different hash on every
session, permanently breaking the gated eval's staleness check.
"""
from __future__ import annotations

from pathlib import Path


def generate_three_page_pdf(target: Path) -> None:
    """Generate a three-page PDF at *target* with pinned page content.

    Pages contain exactly:
    - Page 1: "alpha content"
    - Page 2: "beta content"
    - Page 3: "gamma content"

    The PDF is byte-deterministic (``invariant=True`` suppresses reportlab
    timestamps). The parent directory is created if it does not already exist.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    from reportlab.pdfgen.canvas import Canvas  # noqa: PLC0415

    c = Canvas(str(target), pagesize=(612, 792), invariant=True)
    c.setCreator("archon-search-test")
    c.setProducer("archon-search-test")
    c.drawString(100, 700, "alpha content")
    c.showPage()
    c.drawString(100, 700, "beta content")
    c.showPage()
    c.drawString(100, 700, "gamma content")
    c.showPage()
    c.save()
