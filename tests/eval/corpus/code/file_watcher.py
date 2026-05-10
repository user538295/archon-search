"""Directory watcher using polling (cross-platform fallback)."""
from __future__ import annotations
import asyncio
import hashlib
from pathlib import Path
from typing import Callable


ChangedCallback = Callable[[Path], None]


class FileWatcher:
    """Poll a directory for file changes and invoke a callback on modifications."""

    def __init__(
        self,
        directory: Path,
        on_change: ChangedCallback,
        interval: float = 1.0,
        pattern: str = "**/*",
    ) -> None:
        self._dir = directory
        self._on_change = on_change
        self._interval = interval
        self._pattern = pattern
        self._checksums: dict[Path, str] = {}

    async def run(self) -> None:
        self._checksums = self._snapshot()
        while True:
            await asyncio.sleep(self._interval)
            current = self._snapshot()
            for path, checksum in current.items():
                if self._checksums.get(path) != checksum:
                    self._on_change(path)
            self._checksums = current

    def _snapshot(self) -> dict[Path, str]:
        result = {}
        for path in self._dir.glob(self._pattern):
            if path.is_file():
                data = path.read_bytes()
                result[path] = hashlib.md5(data).hexdigest()
        return result
