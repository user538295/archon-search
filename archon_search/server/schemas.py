"""Shared Pydantic response models for archon-search REST API.

Pure data models — no business logic.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


# Inclusive valid range for TTL seconds — shared by IngestRequest and PatchCollectionBody.
_TTL_MIN: int = 1
_TTL_MAX: int = 2**31 - 1


class CheckStatus(str, Enum):
    """Check result for readiness probes.

    ``PENDING`` and ``WARN`` (D6 C2) are additive values used by the model
    validation check: ``PENDING`` while background validation has not yet
    completed, ``WARN`` when a model loaded via provider fallback (e.g. CPU
    instead of GPU). See contract C2 in
    ``Documentation/Backlog/D6-provider-validation-team-plan.md``.
    """

    OK = "ok"
    FAIL = "fail"
    PENDING = "pending"
    WARN = "warn"


class ReadinessChecks(BaseModel):
    storage: CheckStatus
    # D6 BE-5 — model validation check; PENDING until background validation
    # completes. Default required: routes_ready.py constructs this with only
    # ``storage=`` while validation may still be running.
    models: CheckStatus = CheckStatus.PENDING


class ReadinessResponse(BaseModel):
    """Terse, unauthenticated response body for GET /ready."""

    ready: bool
    checks: ReadinessChecks


class WatcherReport(BaseModel):
    running: bool
    watching: list[str] = []


class JobCounts(BaseModel):
    """Job queue depth snapshot.

    ``running`` counts jobs in RUNNING status only; CANCELLING jobs are
    excluded — a cancelling job is in the process of stopping and does not
    represent available capacity.
    """

    pending: int = Field(ge=0)
    running: int = Field(ge=0)


class ReadinessDetail(BaseModel):
    """Rich readiness sub-object for authenticated GET /status."""

    storage_connected: bool
    embedder_warm: bool
    reranker_warm: bool
    jobs: JobCounts
    collections_indexing: int = Field(ge=0)
    collections_failed: int = Field(ge=0)
    watcher: WatcherReport


class HealthResponse(BaseModel):
    status: str  # "running"
    version: str
    # D9 BE-9 — MCP status field (additive, nullable); null when mcp.enabled = false
    mcp: McpStatusDetail | None = None


class StatusCollectionEntry(BaseModel):
    name: str
    path: str
    doc_count: int = 0
    chunk_count: int = 0
    status: str
    watching: bool
    eta_seconds: float | None = None
    processed_files: int = 0
    total_files: int = 0
    error: str | None = None
    error_count: int = 0
    needs_reindex: bool = False
    warning: str | None = None
    # E1b BE-8 — community stats (populated when graph.enabled=true and communities built)
    community_count: int = 0
    last_built_at: str | None = None


class CollectionBackupStatus(BaseModel):
    """Per-collection backup state surfaced under StatusResponse.backup (D2 Task 4.2)."""

    collection: str
    last_backup_at: str | None = None
    archive_count: int = 0


class BackupStatusDetail(BaseModel):
    """Scheduled-backup state for the caller's namespace (D2 Task 4.2)."""

    enabled: bool
    interval_hours: int
    last_tick_at: str | None = None
    next_run_at: str | None = None
    collections_excluded: list[str] = []
    collection_status: list[CollectionBackupStatus] = []


class CollectionHealthEntry(BaseModel):
    """Per-collection health snapshot written after each maintenance pass (D5 C1)."""

    collection: str
    fts_optimized_at: str | None = None
    orphans_removed_last_run: int = Field(default=0, ge=0)
    last_retry_at: str | None = None
    last_error: str | None = None
    mutations_since_recompute: int = Field(default=0, ge=0)
    centroid_recompute_threshold: int = Field(default=0, ge=0)
    meta_chunk_count: int = Field(default=0, ge=0)


class MaintenanceStatusDetail(BaseModel):
    """Maintenance loop state for the caller's namespace (D5 C1)."""

    enabled: bool
    interval_hours: int = Field(default=0, ge=0)
    last_run_at: str | None = None
    next_run_at: str | None = None
    collection_health: list[CollectionHealthEntry] = []
    # E2a BE-8 — live count of chunks currently past their expires_at (never null)
    expired_chunk_count: int = Field(default=0, ge=0)
    # E2a BE-8 — timestamp of the last expired-chunk prune run (null until first run)
    last_expired_pruned_at: str | None = None
    # BE-8 (graph GC) — timestamp of the last graph GC pass (null until first GC run)
    last_graph_gc_at: str | None = None


class MaintenanceTriggerResponse(BaseModel):
    """Response body for POST /maintenance/trigger (D5 C2)."""

    status: Literal["triggered", "already_triggered"]


class ModelValidationStatus(BaseModel):
    """API-facing mirror of ModelValidationResult, nested under GET /status (D6 C1).

    A null (``None``) boolean field means the corresponding probe has not run /
    completed. ``validated_at`` is ``None`` while validation is pending and a UTC
    timestamp once it has finished. See ``D6-model-validation-status.tsp`` (C1).
    """

    embedder_ok: bool | None = None
    reranker_ok: bool | None = None
    provider_warnings: list[str] = Field(default_factory=list)
    validated_at: datetime | None = None


class TelemetryStatusDetail(BaseModel):
    """Telemetry status sub-object for GET /status (D8 BE-5 / C3).

    Present when ``telemetry.enabled = true``; the parent ``telemetry`` field
    being ``null`` signals that telemetry is disabled.

    ``hash_doc_ids_enabled`` is ``True`` only when both the config flag is on
    **and** a valid salt was loaded at startup (guards the salt-unreadable
    fallback case per S5).
    """

    enabled: bool
    hash_doc_ids_enabled: bool


class McpStatusDetail(BaseModel):
    """MCP server status sub-object for GET /status (D9 C3).

    ``bind_address`` is ``None`` when MCP is enabled but the mount has not
    succeeded (not yet bound), per the C3 contract; non-null only after a
    successful ``app.mount("/mcp", ...)`` in the lifespan.
    ``enabled`` is always ``True`` when this object is present; the parent ``mcp``
    field being ``null`` is the signal that MCP is disabled.

    The JSON field is ``bindAddress`` (camelCase) per the C3 contract
    (``api-contracts/archon-mcp-status.openapi.yaml``); the Python attribute is
    ``bind_address`` to match the snake_case convention used elsewhere.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    enabled: bool
    # Required-and-nullable per the C3 contract: ``bindAddress`` is always present
    # in the response (the route always passes a value) but is ``null`` until the
    # mount succeeds. No ``default`` so the generated schema lists it under
    # ``required`` — matching ``archon-mcp-status.openapi.yaml``.
    bind_address: str | None = Field(
        serialization_alias="bindAddress",
        validation_alias=AliasChoices("bindAddress", "bind_address"),
    )


class SearchStatusDetail(BaseModel):
    """Search configuration status sub-object for GET /status (E0c BE-4 / C4).

    Exposes the operator-configured search limits so clients can introspect
    the active ceiling without reading TOML directly.  Both fields always
    reflect the live ``SearchConfig`` values — defaults or TOML overrides.
    """

    max_fanout: int
    top_k_max: int


class ExpiringChunkItem(BaseModel):
    """One entry in an ExpiringChunksResponse (E2a BE-4).

    Mirrors the dict shape returned by ``SearchStore.query_expiring_chunks``.
    """

    chunk_id: str
    doc_id: str
    source_path: str
    expires_at: str


class ExpiringChunksResponse(BaseModel):
    """Response for GET /collections/{name}/expiring (E2a BE-4).

    ``items`` contains the current page of expiring chunks sorted by
    ``(expires_at ASC, chunk_id ASC)``.  ``next_cursor`` is the opaque
    cursor for the next page (``None`` on the last page).  ``page_count``
    is the count of items in the current page (scan-bounded endpoint —
    unlike ``DocumentListResponse.total``, there is no full-collection count).
    """

    items: list[ExpiringChunkItem]
    next_cursor: str | None
    page_count: int


class DocumentInfoItem(BaseModel):
    """One document entry in a DocumentListResponse (E0c BE-6).

    Mirrors the public fields of ``archon_search._types.DocumentInfo``.
    """

    doc_id: str
    source_path: str
    chunk_count: int
    indexed_at: str
    scopes: list[str] = []


class DocumentListResponse(BaseModel):
    """Response for GET /collections/{name}/documents (E0c BE-6).

    Mirrors the shape of ``JobListResponse`` — ``items`` contains the current
    page, ``next_cursor`` is the opaque cursor for the next page (``None`` on
    the last page), and ``total`` is the full document count for the collection
    independent of pagination.
    """

    items: list[DocumentInfoItem]
    next_cursor: str | None
    total: int


class HydeStatusDetail(BaseModel):
    """HyDE feature status sub-object for GET /status (E0b BE-8 / C2).

    Present only when ``[hyde] enabled = true``; the parent ``hyde`` field
    being ``null`` signals that HyDE is not configured.

    ``key_available`` is ``True`` when ``ANTHROPIC_API_KEY`` is set in the
    server's environment at request time.
    """

    key_available: bool


class RagFusionStatusDetail(BaseModel):
    """RAG Fusion feature status sub-object for GET /status (E0b BE-8 / C2).

    Present only when ``[rag_fusion] enabled = true``; the parent ``rag_fusion``
    field being ``null`` signals that RAG Fusion is not configured.

    ``key_available`` is ``True`` when ``ANTHROPIC_API_KEY`` is set in the
    server's environment at request time.
    """

    key_available: bool


class GraphCollectionStats(BaseModel):
    """Per-collection graph statistics (E1a FE-1 / C2).

    Each entry reports the number of nodes and edges in the collection's
    ``_archon_graph_{col}_nodes`` / ``_archon_graph_{col}_edges`` tables.
    ``communities_invalidated`` is ``True`` when graph GC removed nodes/edges
    but communities have not yet been rebuilt for this collection (BE-8).

    BE-7 health fields:
    ``synonym_edge_count`` — number of edges with relationship_type='synonym_of'.
    ``singleton_node_pct`` — percentage of nodes with no edges (isolated nodes, 0.0–100.0).
    ``synonym_link_rate`` — fraction of edges that are synonym edges (0.0–1.0). Proxy for
        enrichment coverage.
    """

    collection: str
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    # BE-8 — True when GC invalidated communities and rebuild is pending
    communities_invalidated: bool = False
    # BE-7 — health metrics
    synonym_edge_count: int = 0
    singleton_node_pct: float = 0.0
    synonym_link_rate: float = 0.0


class GraphStatusDetail(BaseModel):
    """Graph feature status sub-object for GET /status (E1a FE-1 / C2).

    Present when ``[graph] enabled = true``; the parent ``graph`` field
    being ``null`` signals that graph is disabled.

    ``backend_threshold_edges`` mirrors the configured operator threshold
    for the NetworkX backend — above this count a WARNING is emitted on
    ingest and a hint to prune old documents or raise
    ``backend_threshold_edges`` is surfaced.
    ``stale_mention_count`` is the aggregate count of stale mention rows
    detected during the last graph GC pass (BE-8); 0 before any GC pass.
    """

    enabled: bool
    backend_threshold_edges: int
    collections: list[GraphCollectionStats] = []
    # BE-8 — sum of stale mention rows detected across all collections during last GC
    stale_mention_count: int = 0


class CodeParsersStatusDetail(BaseModel):
    """Code parser (tree-sitter) status sub-object for GET /status (E2g BE-11).

    Present when ``[graph] enabled = true``; the parent ``code_parsers`` field
    being ``null`` signals that graph is disabled (this check is only
    meaningful alongside code graphing).

    ``degraded`` is ``True`` once any file extension has hit the missing-grammar
    branch in ``code_enricher._get_grammar`` (lazy, per-extension cache —
    reflects extensions actually attempted during this process's lifetime, not
    an eager scan of installed packages). ``missing_extensions`` lists the
    extensions with no available grammar so far. ``message`` names the fix.
    """

    degraded: bool
    missing_extensions: list[str] = []
    message: str = ""


class StatusResponse(BaseModel):
    running: bool
    pid: int
    version: str
    collections: list[StatusCollectionEntry]
    readiness: ReadinessDetail | None = None
    backup: BackupStatusDetail | None = None
    # D3 BE-15 — schema migration health fields
    store_schema_version: int = 0
    collections_schema_behind: int = Field(default=0, ge=0)
    # D5 BE-3 — maintenance health field (additive, nullable)
    maintenance: MaintenanceStatusDetail | None = None
    # D6 BE-2 — model validation health field (additive, nullable)
    model_validation: ModelValidationStatus | None = None
    # D8 BE-5 — telemetry status field (additive, nullable); null when telemetry disabled
    telemetry: TelemetryStatusDetail | None = None
    # D9 BE-8 — MCP status field (additive, nullable)
    mcp: McpStatusDetail | None = None
    # E0c BE-4 — search config limits; nullable for schema consistency with sibling sub-objects;
    # the builder always populates it — never null in practice.
    search: SearchStatusDetail | None = None
    # E0b BE-8 — HyDE key availability (additive, nullable); null when hyde.enabled=false
    hyde: HydeStatusDetail | None = None
    # E0b BE-8 — RAG Fusion key availability (additive, nullable); null when rag_fusion.enabled=false
    rag_fusion: RagFusionStatusDetail | None = None
    # E0b BE-10 — count of FAILED_EXPIRED IngestJob instances in the caller's namespace
    failed_expired_ingest_count: int = 0
    # E1a FE-1 — graph feature status (additive, nullable); null when graph.enabled=false
    graph: GraphStatusDetail | None = None
    # E2g BE-11 — code parser (tree-sitter) soft-degrade status (additive, nullable);
    # null when graph.enabled=false
    code_parsers: CodeParsersStatusDetail | None = None


class IndexingStateCollectionEntry(BaseModel):
    status: str
    processed_files: int = 0
    total_files: int = 0
    error: str | None = None
    error_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None


class IndexingStateResponse(BaseModel):
    collections: dict[str, IndexingStateCollectionEntry]
    last_updated: str | None = None
    trigger: str | None = None


class CollectionSummary(BaseModel):
    name: str
    path: str
    description: str = ""
    doc_count: int = 0
    chunk_count: int = 0
    namespace: str
    status: str
    active_embedding_model: str = ""
    needs_reindex: bool = False


class CollectionDetail(CollectionSummary):
    active_embedding_model: str
    pending_embedding_model: str | None = None
    needs_reindex: bool = False
    reindex_job_id: str | None = None
    centroid_present: bool = False
    last_indexed: str | None = None
    acl_protected_count: int = 0
    acl_open_count: int = 0


class JobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    result: str | dict | None = None
    error: str | None = None
    namespace: str
    progress: dict | None = None
    # D5-BE-7: source, source_path, collection, retry_count are now on the
    # IngestJob base class. source defaults to "user"; collection and
    # source_path default to ""; retry_count defaults to 0.
    source: str = "user"
    source_path: str = ""
    collection: str = ""
    retry_count: int = 0
    output_path: str | None = None
    archive_path: str | None = None
    # D3 MigrationJob fields. Nullable and additive: non-migration jobs get None.
    kind: str | None = None
    migrations_applied: list[str] | None = None
    backup_confirmed: bool | None = None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
    total: int


class DeleteResponse(BaseModel):
    name: str
    deleted: bool


class ErrorDetail(BaseModel):
    detail: str


class ExcludedCollectionSchema(BaseModel):
    name: str
    reason: str


class SkippedItem(BaseModel):
    """One entry in BackupTriggerResponse.skipped — collection + machine-readable reason."""

    collection: str
    reason: str


class BackupTriggerResponse(BaseModel):
    """Response body for POST /backup/trigger (D2 Task 4.1)."""

    queued: list[str]
    skipped: list[SkippedItem]


class PatchCollectionBody(BaseModel):
    embedding_model: str | None = None
    # E2a BE-4: optional default TTL in seconds for new chunks (None = no change;
    # explicit null clears the collection default).
    default_ttl_seconds: int | None = Field(default=None)

    @field_validator("embedding_model")
    @classmethod
    def validate_embedding_model_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("embedding_model must not be empty string")
        return v

    @field_validator("default_ttl_seconds")
    @classmethod
    def validate_default_ttl_seconds(cls, v: int | None) -> int | None:
        if v is not None and not (_TTL_MIN <= v <= _TTL_MAX):
            raise ValueError(f"default_ttl_seconds must be in [{_TTL_MIN}, {_TTL_MAX}], got {v}")
        return v


class KeyResponse(BaseModel):
    """A single key record in list/revoke responses — no token field (D7 BE-6).

    ``id`` is ``str | None`` to accommodate TOML synthetic keys (which have
    ``id=None`` since they are not managed entries).  TOML synthetic keys
    cannot be targeted by ``DELETE /keys/{id}``.
    """

    id: str | None
    namespace: str
    label: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] = "active"


class KeyListResponse(BaseModel):
    """Response body for GET /keys (D7 BE-6).

    ``hidden_revoked_count`` is the count of revoked keys excluded when the
    ``status`` filter is ``'active'`` (default).  Always 0 when the filter is
    ``'revoked'`` or ``'all'``.
    """

    keys: list[KeyResponse]
    hidden_revoked_count: int = 0


class KeyRevokeResponse(BaseModel):
    """Response body for DELETE /keys/{id} (D7 BE-6).

    ``status`` is always ``'revoked'`` — a successful DELETE always produces a revoked key.
    """

    id: str
    status: Literal["revoked"] = "revoked"


class KeyCreateRequest(BaseModel):
    """Request body for POST /keys (D7 BE-4)."""

    namespace: str
    label: str | None = None
    expires_at: AwareDatetime | None = None


class KeyCreateResponse(BaseModel):
    """Response body for POST /keys (D7 BE-4).

    The ``token`` field is present only in the create response — it is printed
    once and never recoverable from the server thereafter.  ``status`` is always
    ``'active'`` at creation time (for consistency with ``KeyResponse`` which
    carries the live status value).

    See contract C3 in ``Documentation/Backlog/D7-multi-key-auth-rotation-team-plan.md``.
    """

    id: str
    token: str
    namespace: str
    label: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] = "active"


class KeyRotateRequest(BaseModel):
    """Request body for POST /keys/rotate (D7 BE-8).

    ``grace_seconds`` overrides the TOML ``[auth].rotate_grace_seconds`` default.
    If absent, the TOML default is used.  If both are 0, rotation is immediate.
    """

    grace_seconds: int | None = Field(default=None, ge=0)


class KeyRotateResponse(BaseModel):
    """Response body for POST /keys/rotate (D7 BE-8).

    The ``token`` field carries the new raw bearer token — printed once and
    never recoverable.  Optional ``old_key_*`` fields are populated when a
    previous default managed key was found and mutated (revoked or
    grace-expiring).
    """

    new_key_id: str
    token: str
    status: Literal["active"] = "active"
    old_key_id: str | None = None
    old_key_expires_at: datetime | None = None
    old_key_status: Literal["active", "revoked"] | None = Field(
        default=None,
        description=(
            "Status of the old key after rotation.  ``'active'`` with a non-null "
            "``old_key_expires_at`` means the key is grace-expiring — it will be "
            "rejected after ``old_key_expires_at`` but is still valid until then.  "
            "``'revoked'`` means the old key is immediately invalid.  ``null`` "
            "means no previous managed key was found (e.g., first rotation from "
            "an auto-generated key that was never in keys.json)."
        ),
    )


class MigrationSpecSchema(BaseModel):
    """Serialized representation of a MigrationSpec for REST responses."""

    name: str
    kind: str
    description: str
    introduced_at: int


class MigrationPendingResponse(BaseModel):
    """Response body for GET /collections/{name}/migrations/pending."""

    collection: str
    pending: list[MigrationSpecSchema]
    schema_version: int


class MigrateRequest(BaseModel):
    """Request body for POST /collections/{name}/migrate."""

    backup_confirmed: bool = False
    dry_run: bool = False


class MigrateInPlaceResponse(BaseModel):
    """Response body for POST /collections/{name}/migrate (in-place synchronous path)."""

    migrations_applied: list[str]


class GraphNodeResponse(BaseModel):
    """A node in a graph inspection response (E2b)."""

    entity_id: str
    """ID of the graph node (from make_stable_entity_id)."""
    entity_name: str
    """Human-readable name of the entity."""
    chunk_count: int
    """Number of distinct chunks where this entity was mentioned."""
    salience: float
    """Salience score for this entity. In frequency mode: chunk ratio clamped to [0.0, 1.0]. In tfidf mode: TF×max(IDF, 0), ≥ 0.0 and unbounded above."""
    entity_type: str
    """Entity type value from EntityType enum (e.g. 'person', 'concept', 'system', 'event', 'code_symbol') — E2j BE-1."""
    pagerank_score: float | None = None
    """Persisted unweighted PageRank importance score over code-symbol edges — E2g BE-7.
    ``None`` when not yet computed by the background recompute."""


class GraphEdgeResponse(BaseModel):
    """An edge in a graph inspection response (E2b)."""

    edge_id: str
    """ID of the edge (from make_stable_edge_id)."""
    source_entity_id: str
    """ID of the source node."""
    target_entity_id: str
    """ID of the target node."""
    weight: int
    """Number of chunks where both endpoints are mentioned (co-occurrence count)."""
    source_chunk_ids: list[str]
    """Chunk IDs where both endpoints co-occur; sorted lexicographically, capped at 20."""
    relationship_type: str = "related_to"
    """Semantic relationship type (e.g. 'related_to', 'synonym_of'). BE-7."""


class GraphInspectionResponse(BaseModel):
    """Response body for GET /graph/{collection} (E2b)."""

    nodes: list[GraphNodeResponse]
    """List of nodes with derived metrics, sorted and truncated."""
    edges: list[GraphEdgeResponse]
    """List of edges where both endpoints survive node truncation, sorted and truncated."""
    truncated: bool
    """True if node cap, edge cap, or mention scan ceiling was reached."""
    node_count: int
    """Total number of nodes in the graph (before truncation)."""
    edge_count: int
    """Number of edges where BOTH endpoints exist (after node filter, before edge cap)."""
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency"
    """Salience scoring mode used: 'frequency' (chunk ratio, clamped), 'tfidf' (TF×IDF), or 'importance' (persisted PageRank over code-symbol edges, nulls-last)."""


class CrossCollectionGraphInspectionResponse(BaseModel):
    """Response body for GET /graph/cross-collection (E2b)."""

    collections: list[str]
    """Names of the collections included in the merged graph."""
    nodes: list[GraphNodeResponse]
    """Merged nodes (deduplicated by entity_id, chunk counts summed)."""
    edges: list[GraphEdgeResponse]
    """Merged edges (deduplicated by edge id, weights summed)."""
    truncated: bool
    """True if node cap, edge cap, or mention scan ceiling was reached."""
    node_count: int
    """Total merged node count (before edge survival filter and truncation)."""
    edge_count: int
    """Total merged edge count (after node survival filter, before edge cap)."""
    salience_mode: Literal["frequency", "tfidf", "importance"] = "frequency"
    """Salience scoring mode used: 'frequency' (chunk ratio, clamped), 'tfidf' (TF×IDF), or 'importance' (persisted PageRank over code-symbol edges, nulls-last)."""


class ImpactEdgeResponse(BaseModel):
    """One caller/callee entry in an ``ImpactGroupResponse`` — E2g BE-9."""

    entity_id: str
    """ID of the neighbouring graph node reached by this traversal step."""
    entity_name: str
    """Display name of the neighbouring entity."""
    relationship_type: str
    """Relationship type of the edge connecting this hop."""
    extraction_method: str | None
    """Extraction method of the edge connecting this hop."""
    depth: int
    """Hop distance from the resolved root symbol (1 = direct, 2+ = indirect)."""


class ImpactGroupResponse(BaseModel):
    """Direct vs. indirect callers/callees with truncation visibility — E2g BE-9."""

    direct: list[ImpactEdgeResponse]
    """Hop-1 neighbours, ordered by PageRank descending (nulls-last)."""
    indirect: list[ImpactEdgeResponse]
    """Hop 2..depth neighbours, deduped by node ID, excluding anything already in ``direct``."""
    truncated: bool
    """``True`` when this group's combined ``direct``+``indirect`` population was capped."""
    omitted_count: int
    """Number of entries excluded by the group-size cap; ``0`` when ``truncated`` is ``False``."""


class GraphImpactResponse(BaseModel):
    """Response body for GET /graph/{collection}/impact/{symbol} (E2g BE-9)."""

    symbol: str
    """The requested symbol name (as passed by the caller, not normalized)."""
    callers: ImpactGroupResponse
    """Callers-side blast radius; empty when not requested or the symbol could not be resolved."""
    callees: ImpactGroupResponse
    """Callees-side blast radius; empty when not requested or the symbol could not be resolved."""
    depth_used: int
    """Actual max hop reached (``<= min(depth, MAX_IMPACT_DEPTH)``); ``0`` when unresolved."""
