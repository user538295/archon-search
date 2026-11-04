"""The failure of a RAG Fusion generation: a typed reason and one fixed ``expansion_warning`` per reason.

Stdlib only: ``pipeline.py`` imports it at module scope and must not pull the ``llm_client`` package
(which ``rag_fusion.py`` imports) into every CLI start (``tests/test_cli_startup_latency.py``). ``rag_fusion.py`` raises
:class:`RAGFusionFailure`; the pipeline turns it into ``SearchPipelineResult.rag_fusion_warning``.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final

#: ``expansion_warning`` of a RAG Fusion generation that overran ``[rag_fusion].timeout_seconds``.
RAG_FUSION_TIMED_OUT_WARNING: Final = "RAG Fusion timed out"
#: ``expansion_warning`` of every other RAG Fusion failure (the pipeline's search stages use it too): one fixed text, like
#: HyDE's, whatever the cause; the cause is in the server log and in ``rag_fusion_failure_reason`` of ``/explain``.
RAG_FUSION_FAILED_WARNING: Final = "RAG Fusion expansion failed"


class RAGFusionFailureReason(StrEnum):
    """Why :meth:`RAGFusionGenerator.generate_variants` produced no variant; the value is what ``/explain`` reports."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    NO_ROOM = "no_room_for_query"
    PROVIDER_FAILED = "provider_failed"
    NO_VARIANTS = "no_variants"


class RAGFusionFailure(Exception):
    """A RAG Fusion generation produced no variant.

    Carries the typed :attr:`reason` and nothing else: no query, no prompt, no provider text, so :attr:`warning` is safe to
    show a caller and the exception is safe to log, copy or pickle.
    """

    def __init__(self, reason: RAGFusionFailureReason) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def warning(self) -> str:
        """The fixed ``expansion_warning`` for this failure."""
        if self.reason is RAGFusionFailureReason.TIMEOUT:
            return RAG_FUSION_TIMED_OUT_WARNING
        return RAG_FUSION_FAILED_WARNING
