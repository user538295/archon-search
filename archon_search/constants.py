"""Standalone constants for archon-search — defined independently of archon.ai.constants."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# Pre-A1 sentinel for ``ingested_by``. Stored on legacy rows; normalized to
# ``"cli"`` at read/header-parse boundaries (see _types.IngestedBy). Reindex
# (Task 6.2) rewrites legacy stored values to ``"reindex"``.
LEGACY_INGESTED_BY: Final[str] = "archon-search-cli"

# Canonical ``ingested_by`` values (must mirror the ``IngestedBy`` Literal in
# ``_types.py`` — drift is pinned by tests/test_types_ingested_by.py). Legacy
# is intentionally NOT included.
INGESTED_BY_VALUES: Final[tuple[str, ...]] = ("cli", "http", "watcher", "reindex")

# Per-collection ingest-lock acquisition timeout (Task 6.1). Hardcoded for v1;
# the only externally-visible knob is the 503 ``Retry-After`` header derived
# from this value (rounded up to integer seconds per RFC 7231).
INGEST_LOCK_TIMEOUT_S: Final[float] = 30.0

# ``[maintenance].version_retention_hours`` default: LanceDB keeps each superseded table version
# (manifest, transaction and data files) until it is older than this. Long enough that a reader
# still on an old version, or an operator rolling a table back, is never cut off; short enough that
# an ingest-heavy server's disk use stays bounded. LanceDB's own default is 7 days.
DEFAULT_VERSION_RETENTION_HOURS: Final[int] = 24

# asyncio.wait_for timeout used by SearchStore.ping() (B2 Task 1.1).
PING_TIMEOUT_SECONDS: Final[float] = 1.0

# In-process cache TTL for the ping result (B2 Task 1.1).
PING_TTL_SECONDS: Final[float] = 1.0

# Pinned dated version for internal fast-model tasks (description generation).
DEFAULT_FAST_MODEL: str = "claude-haiku-4-5-20251001"

# Default model for description generation when a more capable model is preferred.
DEFAULT_MODEL: str = "claude-sonnet-4-6"

# Default namespace used when no explicit namespace is specified.
DEFAULT_NAMESPACE: str = "default"

# Upper bound for a key-rotation grace period (``POST /keys/rotate`` / MCP ``rotate_key``
# ``grace_seconds``, TOML ``[auth].rotate_grace_seconds``). It exists only so
# ``now + timedelta(seconds=grace)`` cannot overflow ``datetime`` (year 9999, reached at
# ~2.5e11 s from now); 100 years (365-day years) restricts no realistic grace window.
MAX_ROTATE_GRACE_SECONDS: Final[int] = 100 * 365 * 24 * 60 * 60

# Minimum length (characters) of any bearer token the server accepts from configuration: the default
# key (``ARCHON_SEARCH_API_KEY`` / ``.search.env``) and every TOML ``[namespaces]`` token. Keys the server
# generates are 64 hex characters (``secrets.token_hex(32)``); 32 hex characters is 128 bits of entropy.
MIN_API_KEY_LENGTH: Final[int] = 32

# Lifetime of a graph-viewer view token (``POST /graph/{collection}/view-token``). The viewer page loads
# the token from its URL and fetches the graph with it immediately, so a minute is ample; the token is
# scoped to one namespace and one collection and grants read access to that collection's graph only.
GRAPH_VIEW_TOKEN_TTL_SECONDS: Final[int] = 60

# Query parameter carrying a graph-viewer view token; the access log redacts its value.
VIEW_TOKEN_QUERY_PARAM: Final[str] = "view_token"

# ``[server].docs_enabled`` default: the interactive ``/docs`` and ``/redoc`` pages (Swagger UI loads its
# JavaScript from a CDN) are not served unless the operator opts in.
DEFAULT_DOCS_ENABLED: Final[bool] = False

# Bytes in one MiB: the unit of every ``*_mb`` size limit in the configuration.
BYTES_PER_MIB: Final[int] = 1024 * 1024

# The environment variable ``archon-search`` reads as "I run inside a container". The Dockerfile sets it to ``1``;
# it selects the container-only defaults (the bind address below, stderr logging).
CONTAINER_ENV_VAR: Final[str] = "ARCHON_SEARCH_CONTAINER"

# Bind address ``archon-search serve`` defaults to inside a container, where the published port is only reachable
# through a non-loopback interface. Natively the default stays ``SearchConfig.host`` (loopback); the operator's
# ``[server].host`` / ``ARCHON_SEARCH_HOST`` win in both.
CONTAINER_BIND_HOST: Final[str] = "0.0.0.0"

# ``[server].max_request_body_mb`` default: the largest request body the server buffers (inline ``documents``
# ingest, a bulk collection payload). 32 MiB fits a batch of inline documents several times over at the default
# per-document ``[ingest].max_file_mb`` of one file; a larger body is refused with 413 before it is read.
DEFAULT_MAX_REQUEST_BODY_MB: Final[int] = 32

# ``[server].graceful_shutdown_seconds`` default: how long uvicorn lets open requests (an in-flight ingest) finish
# after SIGTERM before it cancels them. Shorter than the 30 s ``docker stop`` waits before SIGKILL, so the server
# always gets to finish its own shutdown (flushing state, releasing the data directory) before the kill arrives.
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS: Final[int] = 25

# ``[ingest].max_file_mb`` default. A chosen cap, not a measured limit: it bounds the memory one parse (a plain-text
# read, a docling or markitdown conversion) can pin, so a single hostile or runaway file cannot exhaust the host.
# 0 disables the cap, at the operator's risk.
DEFAULT_MAX_FILE_MB: Final[int] = 100

# ``[ingest].parse_timeout_seconds`` default: wall-clock bound on one docling (PDF / image) conversion. A large
# scanned PDF with OCR takes minutes; ten times that is a wedged parse worker, which is then replaced.
DEFAULT_PARSE_TIMEOUT_SECONDS: Final[float] = 300.0

# ``[ingest].parse_timeout_seconds_per_mb`` default: seconds added to the parse bound per MiB of the file, so a big
# scanned PDF is judged by its size and not by the flat bound a one-page file needs. Measured on an Apple-silicon
# Mac (docling on CPU, which ``parser._pin_docling_off_mps`` forces there): 3.8 s per MiB of a 150-dpi scanned PDF
# (2.1 s per page, 0.55 MiB per page). The default is 2.5 times that, for slower hosts and container CPUs.
# 0 turns the scaling off: the bound is then ``parse_timeout_seconds`` alone.
DEFAULT_PARSE_TIMEOUT_SECONDS_PER_MB: Final[float] = 10.0

# ``[ingest].max_zip_entries`` / ``max_zip_uncompressed_mb`` defaults: bounds checked on the central directory of a
# zip-based Office file (.docx, .pptx, .xlsx, .epub) before markitdown opens it, so a zip bomb is refused rather
# than inflated. A real document has tens to low thousands of entries; 512 MiB uncompressed is above any real
# document and still a quarter of a typical container memory limit.
DEFAULT_MAX_ZIP_ENTRIES: Final[int] = 10_000
DEFAULT_MAX_ZIP_UNCOMPRESSED_MB: Final[int] = 512

# ``[ingest].max_discovered_files`` default: the most files one directory ingest enumerates. The walk is bounded so a
# request for ``/`` (or a glob that matches a whole disk) cannot build an unbounded list in memory; a directory with
# more files is refused with an explanation instead of being silently truncated.
DEFAULT_MAX_DISCOVERED_FILES: Final[int] = 100_000

# ``[ingest].max_concurrent_tasks`` default: ingest jobs (``POST /ingest``, ``POST /collections/``) running at once
# across all collections. Each one parses, embeds and writes; more than a few at once only contend for the CPU and
# the embedder. A further request is refused with 503 ``ingest_busy`` and a ``Retry-After``.
DEFAULT_MAX_CONCURRENT_INGEST_TASKS: Final[int] = 4

# ``[graph].max_cross_collection_collections`` default: the collection names one ``GET /graph/cross-collection``
# (or MCP ``get_graph_cross_collection``) call may name. Equal to the default multi-collection search fan-out
# (``max_fanout``), so graph reads are bounded like searches are.
DEFAULT_MAX_CROSS_COLLECTION_COLLECTIONS: Final[int] = 8

# Default blend weight for description-embedding cosine in hybrid routing.
# Shared by router.py, config.py, and eval/runner.py to avoid hardcoding 0.3.
DEFAULT_ROUTING_DESCRIPTION_WEIGHT: Final[float] = 0.3

# Default neighbour chunks per side for search_with_context (pipeline and MCP tool), and the
# floor config.py enforces on [search].context_window_max so a default call is never rejected.
DEFAULT_CONTEXT_WINDOW: Final[int] = 1

# Default [search].context_window_max: ceiling on search_with_context's context_window,
# the neighbour chunks fetched on each side of a hit — at most 2 * context_window_max chunks
# per hit (top_k_return hits), each up to [database].chunk_size tokens. A chosen cap (10x
# DEFAULT_CONTEXT_WINDOW), not a measured limit. A constant (unlike the top_k_max literal)
# because the SearchPipeline constructor default and the MCP schema description need it too.
DEFAULT_CONTEXT_WINDOW_MAX: Final[int] = 10

# S107: emitted when [logging].log_file is empty (file logging disabled) outside
# container mode. Shared by config.load_config and logging_setup.configure_logging
# so the two call sites never drift.
LOG_FILE_DISABLED_WARNING: Final[str] = (
    "[logging].log_file is set to an empty string — file logging is disabled. "
    "To re-enable, set [logging].log_file to a path, "
    "or set ARCHON_SEARCH_CONTAINER=1 to use stderr output instead."
)

# Maximum number of chunks passed to a single store.ingest_chunks() call during
# ingest_file() batch-emit (D4). At ~4 KB per chunk, 512 chunks ≈ 2 MB per
# batch — safe on any memory-constrained host.
# ponytail: profile on large PDFs before changing
# ponytail: monitor for LanceDB fragmentation on large corpora
_INGEST_CHUNK_BATCH_SIZE: Final[int] = 512

# Files a docling parse worker handles before it is recycled (`[ingest].max_tasks_per_child`).
# Shared by parser.DocumentParser and config.IngestConfig so the two never drift.
# Worker exit is the only complete reclaim of the native OCR memory docling/onnxruntime retain
# (2026-08-19 OOM incident). At the measured ~9 MB retained per image with OCR scale 1.0,
# 25 files bounds a worker's excess at ~225 MB; the respawn costs ~9 s (docling re-import plus
# the first conversion's model load), i.e. ~0.4 s per file amortised.
DEFAULT_DOCLING_MAX_TASKS_PER_CHILD: Final[int] = 25

# S293: chunk-metadata marker for a document ingested inline (`POST /ingest` with a
# `documents` payload). Its source_path is a logical identity naming no file on the
# server, so the maintenance orphan sweep must not read "file missing" as "file
# deleted" and destroy the document. Absence means disk-backed — correct for every
# row written before the inline mode existed, so no schema migration is needed.
# The value is a string because `store.parse_metadata` coerces every value to `str`.
INLINE_CHUNK_METADATA_KEY: Final[str] = "_inline"
INLINE_CHUNK_METADATA_VALUE: Final[str] = "true"

# Chunk-metadata marker for a chunk written by `POST /collections/{name}/import`. The
# archive keeps the exporting host's source_path verbatim, so the maintenance orphan sweep
# must not read its missing file as a deleted one. Re-ingest semantics: see
# `MaintenanceLoop._run_orphan_cleanup` and OperatorGuide/50_maintenance_and_jobs.md.
# The value is the string "true" because chunk metadata is dict[str, str].
IMPORTED_CHUNK_METADATA_KEY: Final[str] = "_imported"
IMPORTED_CHUNK_METADATA_VALUE: Final[str] = "true"

# Every system-set marker of a chunk not ingested from a file on this host (inline or
# imported), by key. The orphan sweep never deletes a document carrying any of them. The
# keys count toward the metadata field cap like any other key.
NOT_DISK_INGESTED_CHUNK_MARKERS: Final[Mapping[str, str]] = MappingProxyType({
    INLINE_CHUNK_METADATA_KEY: INLINE_CHUNK_METADATA_VALUE,
    IMPORTED_CHUNK_METADATA_KEY: IMPORTED_CHUNK_METADATA_VALUE,
})

_NAMESPACE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _validate_segment_safe(name: str, label: str) -> None:
    """Raise ValueError if *name* contains patterns that break the __ table-name separator."""
    if name.endswith("_"):
        raise ValueError(
            f"Invalid {label} {name!r}: must not end with '_'"
        )
    if "__" in name:
        raise ValueError(
            f"Invalid {label} {name!r}: must not contain consecutive underscores '__'"
        )


def _validate_namespace(name: str) -> None:
    """Raise ValueError if name is not a valid namespace identifier."""
    if name == "deny-all":
        raise ValueError("Namespace name 'deny-all' is reserved and cannot be used.")
    if not _NAMESPACE_RE.fullmatch(name):
        raise ValueError(
            f"Invalid namespace {name!r}: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$"
        )
    _validate_segment_safe(name, "namespace")
