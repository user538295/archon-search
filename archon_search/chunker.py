"""Document chunking for RAG — wraps Chonkie RecursiveChunker."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from archon_search._types import ChunkRecord, IngestedBy

if TYPE_CHECKING:
    from archon_search.code_enricher import ScopeTable


class DocumentChunker:
    """Splits text into token-sized ChunkRecords using Chonkie's RecursiveChunker."""

    def __init__(self, chunk_size: int = 512) -> None:
        from chonkie import RecursiveChunker  # noqa: PLC0415

        self._chunk_size = chunk_size
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
        language: str = "",
    ) -> list[ChunkRecord]:
        """Split text into ChunkRecords.

        chunk_id is left as "" — the pipeline assigns sequential "{doc_id}-{idx:06d}" IDs.
        vector is left as [] — the pipeline fills it after embedding.

        ``file_type``, ``updated_at``, ``ingested_by`` are keyword-only and required;
        every call site must supply them deliberately (Task 3.3 wires the callers).

        ``language`` is keyword-only with default ``""`` (untagged/legacy).  Pass the
        ISO 639-1 / ISO 639-3 code (or ``"unknown"``) from language detection; all
        produced ChunkRecords will carry that tag.
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
                language=language,
                start_offset=chunk.start_index,
                end_offset=chunk.end_index,
            )
            for chunk in chunks
        ]


def _top_level_scopes(scope_table: "ScopeTable") -> list:
    """Return the entries in *scope_table* not strictly contained by any other entry.

    A scope is top-level if no other entry in the table has ``other.start <=
    this.start and other.end >= this.end`` (excluding itself).
    """
    top_level = []
    for i, entry in enumerate(scope_table):
        contained = False
        for j, other in enumerate(scope_table):
            if i == j:
                continue
            if other.start <= entry.start and other.end >= entry.end:
                # Exact-duplicate boundaries (other.start == entry.start and
                # other.end == entry.end) would otherwise mark BOTH entries as
                # contained by each other and drop both; tiebreak on index so
                # only the later one (j > i) is excluded.
                if other.start == entry.start and other.end == entry.end and j < i:
                    continue
                contained = True
                break
        if not contained:
            top_level.append(entry)
    return top_level


class ASTChunker:
    """AST/cAST chunker — splits/merges ChunkRecords on a shared ScopeTable's boundaries.

    Splits source text at top-level (outermost) function/class scope boundaries
    from a :data:`~archon_search.code_enricher.ScopeTable` built by the SAME
    tree-sitter parse pass that :class:`~archon_search.code_enricher.CodeEnricher`
    uses for enrichment (one shared parse, not two). Small adjacent top-level
    scopes — and any module-level text between/around them — merge up to the
    configured token budget. A single scope segment that alone exceeds the
    budget is further split via Chonkie's recursive chunker.

    Falls back to plain token chunking (delegating to :class:`DocumentChunker`)
    when *scope_table* is empty — i.e. tree-sitter is unavailable or parsing
    failed catastrophically.

    A merged chunk spanning multiple top-level scopes is enriched based only
    on the scope containing its start offset — sibling scopes merged into the
    same chunk are not separately represented in symbol metadata; this is an
    existing property of `CodeEnricher.enrich_chunk`'s single-offset
    resolution, not new to this chunker.
    """

    def __init__(self, chunk_size: int = 512) -> None:
        from chonkie import RecursiveChunker  # noqa: PLC0415

        self._chunk_size = chunk_size
        self._sub_chunker = RecursiveChunker(tokenizer="gpt2", chunk_size=chunk_size)
        self._fallback_chunker = DocumentChunker(chunk_size=chunk_size)

    def chunk(
        self,
        text: str,
        doc_id: str,
        source_path: str,
        *,
        file_type: str,
        updated_at: str,
        ingested_by: IngestedBy,
        scope_table: "ScopeTable",
        language: str = "",
    ) -> list[ChunkRecord]:
        """Split *text* into ChunkRecords aligned to *scope_table* boundaries.

        ``scope_table`` is required (no silent default) — the caller (pipeline.py)
        always has one, even if empty. An empty ``scope_table`` triggers the
        token-chunking fallback via :class:`DocumentChunker`.

        Same output contract as :meth:`DocumentChunker.chunk`: ``chunk_id=""``,
        ``vector=[]``. Every returned ``start_offset``/``end_offset`` is an
        accurate offset into the ORIGINAL *text* (not scope-local) — required
        for :meth:`CodeEnricher.enrich_chunk`'s later scope lookup.
        """
        if not text or not text.strip():
            return []

        if not scope_table:
            return self._fallback_chunker.chunk(
                text,
                doc_id,
                source_path,
                file_type=file_type,
                updated_at=updated_at,
                ingested_by=ingested_by,
                language=language,
            )

        segments = self._build_segments(text, scope_table)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        return [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id="",
                text=seg_text,
                vector=[],
                source_path=source_path,
                indexed_at=now,
                file_type=file_type,
                updated_at=updated_at,
                ingested_by=ingested_by,
                language=language,
                start_offset=start,
                end_offset=end,
            )
            for start, end, seg_text in segments
        ]

    def _build_segments(self, text: str, scope_table: "ScopeTable") -> list[tuple[int, int, str]]:
        """Split *text* into scope-aligned blocks, then merge/sub-split to budget."""
        top_level = sorted(_top_level_scopes(scope_table), key=lambda e: e.start)

        # Step 1: split text into blocks at scope boundaries — top-level scope
        # bodies, plus any non-whitespace module-level text between/around them.
        blocks: list[tuple[int, int, str]] = []
        cursor = 0
        for scope in top_level:
            if scope.start > cursor:
                gap = text[cursor:scope.start]
                if gap.strip():
                    blocks.append((cursor, scope.start, gap))
            blocks.append((scope.start, scope.end, text[scope.start:scope.end]))
            # max() guards against scope.end < cursor (overlapping siblings); this
            # is defensive-only — real tree-sitter siblings are always nested-or-disjoint.
            cursor = max(cursor, scope.end)
        if cursor < len(text):
            gap = text[cursor:]
            if gap.strip():
                blocks.append((cursor, len(text), gap))

        if not blocks:
            return []

        # Step 2: merge adjacent blocks up to the token budget; sub-split any
        # single block that alone exceeds the budget via the recursive chunker
        # (keeps a huge single function's chunking behavior sane). Token count
        # per block is measured by delegating to the same RecursiveChunker
        # instance used for oversized-block splitting: chunking a block that
        # fits under the budget always yields exactly one chunk, whose
        # token_count is the block's token count; more than one chunk means
        # the block alone exceeds the budget. When merging, the running
        # `cur_tokens` total is a SUM of per-block token_counts, not a
        # re-tokenization of the merged text — for BPE tokenizers this is a
        # fast, conservative approximation, not an exact re-measurement.
        # Merges can occur across the concatenation boundary, so the true
        # merged-text token count is typically <= the summed value (the safe
        # direction: the budget is never silently exceeded, but a merged
        # chunk can end up conservatively smaller than optimal). This is an
        # accepted tradeoff — re-tokenizing the merged text on every merge
        # step would add real cost to the ingest path.
        segments: list[tuple[int, int, str]] = []
        cur_start: int | None = None
        cur_end = 0
        cur_tokens = 0

        for b_start, b_end, b_text in blocks:
            sub_chunks = self._sub_chunker.chunk(b_text)
            if not sub_chunks:
                continue

            if len(sub_chunks) > 1:
                if cur_start is not None:
                    segments.append((cur_start, cur_end, text[cur_start:cur_end]))
                    cur_start = None
                for sub in sub_chunks:
                    segments.append((b_start + sub.start_index, b_start + sub.end_index, sub.text))
                continue

            b_tokens = sub_chunks[0].token_count
            if cur_start is None:
                cur_start, cur_end, cur_tokens = b_start, b_end, b_tokens
            elif cur_tokens + b_tokens <= self._chunk_size:
                cur_end = b_end
                cur_tokens += b_tokens
            else:
                segments.append((cur_start, cur_end, text[cur_start:cur_end]))
                cur_start, cur_end, cur_tokens = b_start, b_end, b_tokens

        if cur_start is not None:
            segments.append((cur_start, cur_end, text[cur_start:cur_end]))

        return segments
