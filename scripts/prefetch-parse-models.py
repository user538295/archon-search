"""Fetch, at image build time, the model files the first PDF or image parse would download.

Run by the Dockerfile with ``HF_HUB_CACHE`` pointing at the image's model cache. Parsing a PDF or image builds
docling's layout and table-structure models, which fetch their files from Hugging Face on first use. At runtime
that first parse paid the download inside ``[ingest].parse_timeout_seconds`` (300 s), needed the network and
could fail by timing out. Running the real parser here warms exactly the files it needs, whatever docling pins
today, and fails the build if a PDF or a scanned image cannot be parsed. RapidOCR's own checkpoints ship
inside its wheel.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from archon_search.parser import DocumentParser

_PAGE_SIZE = (1240, 1754)
_TEXT_ORIGIN = (100, 200)
_SAMPLE_TEXT = "Prefetch sample page"
_SAMPLE_DPI = 150.0
# The runtime parse bound (300 s) is far too tight for a build: the release workflow builds the arm64 image under
# QEMU on an amd64 runner, where the first parse (model downloads, then OCR) outran it and failed the build. A hung
# build is still bounded here, and by the CI job's own limit.
_BUILD_PARSE_TIMEOUT_SECONDS = 3600.0


def _write_samples(directory: Path) -> list[Path]:
    """A scanned-page PDF and a PNG of the same page: both parse through docling's OCR."""
    page = Image.new("RGB", _PAGE_SIZE, "white")
    ImageDraw.Draw(page).text(_TEXT_ORIGIN, _SAMPLE_TEXT, fill="black")
    pdf, png = directory / "sample.pdf", directory / "sample.png"
    page.save(pdf, "PDF", resolution=_SAMPLE_DPI)
    page.save(png)
    return [pdf, png]


async def _prefetch() -> None:
    parser = DocumentParser(parse_timeout_seconds=_BUILD_PARSE_TIMEOUT_SECONDS)
    with tempfile.TemporaryDirectory() as directory:
        for sample in _write_samples(Path(directory)):
            await parser.parse(sample)
            print(f"prefetch: parsed {sample.name}")


if __name__ == "__main__":
    asyncio.run(_prefetch())
