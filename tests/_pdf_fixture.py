"""tests/_pdf_fixture.py — shared PDF generation helper for conftest fixtures.

Factored out so both tests/conftest.py (unit/integration fixture) and
tests/eval/conftest.py (eval-corpus fixture) can generate the same
three-page PDF without duplicating code. It also owns ``generate_scanned_pdf``
(an image-only page with no text layer, for the docling OCR lane), which only
tests/conftest.py uses -- eval has no OCR fixture.

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


def generate_scanned_pdf(target: Path, text: str = "ARCHON SEARCH SCANNED PAGE") -> None:
    """Generate a one-page, image-only PDF at *target*: a rendered bitmap of *text* with no PDF
    text layer at all (no `drawString` call), so recovering the words requires docling's OCR
    path rather than its native PDF text extraction.

    `three_page_pdf` / `substantial_three_page_pdf` are real text layers docling extracts
    directly — OCR contributes nothing when parsing them. This fixture is for tests that must
    exercise PDF OCR specifically (2026-08-19 image-OCR memory fix, cycle-1 review: the OCR
    behaviour check previously only covered images, never a PDF).
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    from reportlab.lib.utils import ImageReader  # noqa: PLC0415
    from reportlab.pdfgen.canvas import Canvas  # noqa: PLC0415

    size = 1024
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text(
        (size // 12, size // 3),
        text,
        fill=(0, 0, 0),
        font=ImageFont.load_default(size=size // 12),
    )

    # Square page matching the square source bitmap. A 612x792 page stretched the 1024x1024
    # image by 0.598x horizontally and 0.773x vertically -- a 1.29 aspect distortion on every
    # glyph before docling even renders it, which is gratuitous flake risk in a lane that takes
    # minutes (C2-T-6).
    page = 612
    c = Canvas(str(target), pagesize=(page, page), invariant=True)
    c.setCreator("archon-search-test")
    c.setProducer("archon-search-test")
    c.drawImage(ImageReader(img), 0, 0, width=page, height=page)
    c.showPage()
    c.save()
