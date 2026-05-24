"""Document chunking for RAG — wraps Chonkie RecursiveChunker."""
from __future__ import annotations

from datetime import datetime, timezone

from archon_search._types import ChunkRecord, IngestedBy


class DocumentChunker:
    """Splits text into token-sized ChunkRecords using Chonkie's RecursiveChunker."""

    def __init__(self, chunk_size: int = 512) -> None:
        from chonkie import RecursiveChunker  # noqa: PLC0415

        self._chunker = RecursiveChunker(tokenizer="gpt2", chunk_size=chunk_size)

    def chunk(
        self,
        text: str,
        doc_id: str,
        source_path: str,
        *,
        file_type: str,
        updated_at: str,
        ingested_by: IngestedBy,
    ) -> list[ChunkRecord]:
        """Split text into ChunkRecords.

        chunk_id is left as "" — the pipeline assigns sequential "{doc_id}-{idx:06d}" IDs.
        vector is left as [] — the pipeline fills it after embedding.

        ``file_type``, ``updated_at``, ``ingested_by`` are keyword-only and required;
        every call site must supply them deliberately (Task 3.3 wires the callers).
        """
        if not text or not text.strip():
            return []

        chunks = self._chunker.chunk(text)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        return [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id="",
                text=chunk.text,
                vector=[],
                source_path=source_path,
                indexed_at=now,
                file_type=file_type,
                updated_at=updated_at,
                ingested_by=ingested_by,
            )
            for chunk in chunks
        ]
