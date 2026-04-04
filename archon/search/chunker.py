"""Document chunking for RAG — wraps Chonkie RecursiveChunker."""
from __future__ import annotations

from datetime import datetime, timezone

from archon.search._types import ChunkRecord


class DocumentChunker:
    """Splits text into token-sized ChunkRecords using Chonkie's RecursiveChunker."""

    def __init__(self, chunk_size: int = 512) -> None:
        from chonkie import RecursiveChunker  # noqa: PLC0415

        self._chunker = RecursiveChunker(tokenizer="gpt2", chunk_size=chunk_size)

    def chunk(self, text: str, doc_id: str, source_path: str) -> list[ChunkRecord]:
        """Split text into ChunkRecords.

        chunk_id is left as "" — the pipeline assigns sequential "{doc_id}-{idx:06d}" IDs.
        vector is left as [] — the pipeline fills it after embedding.
        """
        if not text or not text.strip():
            return []

        chunks = self._chunker.chunk(text)
        now = datetime.now(timezone.utc).isoformat()
        return [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id="",
                text=chunk.text,
                vector=[],
                source_path=source_path,
                indexed_at=now,
            )
            for chunk in chunks
        ]
