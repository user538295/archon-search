"""JSONL file parser with streaming support."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterator


def parse_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed dicts from a JSONL file, skipping blank lines."""
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Line {lineno}: {exc}") from exc


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write rows to a JSONL file, one JSON object per line."""
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
