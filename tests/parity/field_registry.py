"""The field-level parity mapping (plan C9, brief Q3): every search command field and result field, and where each lives.

Plain data like ``registry.py``: lists, dicts and string constants, no imports and no behaviour, so a test can deep-copy and
mutate it. The shape, the field naming and the failure kinds are documented in ``tests/parity/field_parity.py``; the checker
reads the live schemas and compares them with this mapping. The shim is waived wholesale as adapter policy (it has its own
protocol envelope and ignores a request ``top_k``), so no row maps it.

The command carries two include-metadata flags: the top-level ``include_metadata`` reaches REST, MCP and the CLI, and
``filters.include_metadata`` reaches REST only (MCP and the CLI carry one flag each, which fills the top-level one).
A surface field that maps to no command field is a failure, bar the CLI transport options of ``TRANSPORT_EXEMPTIONS``.
"""

_ONE_INCLUDE_METADATA_FLAG = (
    "MCP and the CLI carry one include-metadata flag, which fills the command's top-level include_metadata; "
    "the service ORs it with filters.include_metadata (C1, C8)"
)
_SHOWN_AS_ACL_GATE = (
    "Raw stored ACL provenance: the responses show it only through results[].acl_gate (sanitised, present when "
    "acl_context was requested)"
)
_REQUEST_FLAG_NOT_ECHOED = (
    "The ORed request flag decides whether the adapter blanks results[].metadata; no response field echoes it"
)

# The CLI options that carry no command field: they shape the transport, not the search.
TRANSPORT_EXEMPTIONS = {"cli_options": ["--json", "--api-url", "--api-key"]}

# One row per command field (``SearchCommand``, ``filters`` nested): REST property, MCP parameter, CLI option or argument.
# ``--collection`` fills ``collection`` (one value) or ``collections`` (several).
REQUEST_FIELD_MAPPINGS = [
    {"command_field": "query", "rest_field": "query", "mcp_param": "query", "cli_option": "query"},
    {"command_field": "collection", "rest_field": "collection", "mcp_param": "collection", "cli_option": "--collection"},
    {
        "command_field": "collections",
        "rest_field": "collections",
        "mcp_param": "collections",
        "cli_option": "--collection",
    },
    {"command_field": "top_k", "rest_field": "top_k", "mcp_param": "top_k", "cli_option": "--top-k"},
    {
        "command_field": "filters.file_type",
        "rest_field": "filters.file_type",
        "mcp_param": "file_type",
        "cli_option": "--file-type",
    },
    {
        "command_field": "filters.source_path_prefix",
        "rest_field": "filters.source_path_prefix",
        "mcp_param": "source_path_prefix",
        "cli_option": "--source-path-prefix",
    },
    {
        "command_field": "filters.source_path_glob",
        "rest_field": "filters.source_path_glob",
        "mcp_param": "source_path_glob",
        "cli_option": "--source-path-glob",
    },
    {
        "command_field": "filters.indexed_after",
        "rest_field": "filters.indexed_after",
        "mcp_param": "indexed_after",
        "cli_option": "--indexed-after",
    },
    {
        "command_field": "filters.indexed_before",
        "rest_field": "filters.indexed_before",
        "mcp_param": "indexed_before",
        "cli_option": "--indexed-before",
    },
    {
        "command_field": "filters.language",
        "rest_field": "filters.language",
        "mcp_param": "language",
        "cli_option": "--language",
    },
    {
        "command_field": "filters.include_metadata",
        "rest_field": "filters.include_metadata",
        "waived_surfaces": ["mcp", "cli"],
        "waiver_reason": _ONE_INCLUDE_METADATA_FLAG,
    },
    {
        "command_field": "include_metadata",
        "rest_field": "include_metadata",
        "mcp_param": "include_metadata",
        "cli_option": "--include-metadata",
    },
    {
        "command_field": "hyde",
        "rest_field": "hyde",
        "mcp_param": "hyde",
        "cli_option": "--hyde",
    },
    {
        "command_field": "rag_fusion",
        "rest_field": "rag_fusion",
        "mcp_param": "rag_fusion",
        "cli_option": "--rag-fusion",
    },
    {
        "command_field": "graph_mode",
        "rest_field": "graph_mode",
        "mcp_param": "graph_mode",
        "cli_option": "--graph-mode",
    },
    {
        "command_field": "scope_filter",
        "rest_field": "scope_filter",
        "mcp_param": "scope_filter",
        "cli_option": "--scope-filter",
    },
    {
        "command_field": "acl_context",
        "rest_field": "acl_context",
        "mcp_param": "acl_context",
        "cli_option": "--acl-context",
    },
]

# One row per result field (``SearchOutcome``, hits and nested models as dotted paths, ``[]`` for a list): REST's
# ``SearchResponse`` field and the MCP ``search`` response field. The CLI's ``--json`` prints the REST body.
RESPONSE_FIELD_MAPPINGS = [
    {"result_field": "results[].result.doc_id", "rest_field": "results[].doc_id", "mcp_field": "results[].doc_id"},
    {"result_field": "results[].result.chunk_id", "rest_field": "results[].chunk_id", "mcp_field": "results[].chunk_id"},
    {"result_field": "results[].result.text", "rest_field": "results[].text", "mcp_field": "results[].text"},
    {"result_field": "results[].result.score", "rest_field": "results[].score", "mcp_field": "results[].score"},
    {"result_field": "results[].result.source_path", "rest_field": "results[].source_path", "mcp_field": "results[].source_path"},
    {"result_field": "results[].result.file_type", "rest_field": "results[].file_type", "mcp_field": "results[].file_type"},
    {"result_field": "results[].result.language", "rest_field": "results[].language", "mcp_field": "results[].language"},
    {"result_field": "results[].result.indexed_at", "rest_field": "results[].indexed_at", "mcp_field": "results[].indexed_at"},
    {"result_field": "results[].result.updated_at", "rest_field": "results[].updated_at", "mcp_field": "results[].updated_at"},
    {"result_field": "results[].result.ingested_by", "rest_field": "results[].ingested_by", "mcp_field": "results[].ingested_by"},
    {"result_field": "results[].result.metadata", "rest_field": "results[].metadata", "mcp_field": "results[].metadata"},
    {"result_field": "results[].result.acl", "rest_field": "results[].acl", "mcp_field": "results[].acl"},
    {"result_field": "results[].result.collection", "rest_field": "results[].collection", "mcp_field": "results[].collection"},
    {"result_field": "results[].result.reranker_score", "rest_field": "results[].reranker_score", "mcp_field": "results[].reranker_score"},
    {"result_field": "results[].result.acl_source", "waived_surfaces": ["rest", "mcp"], "waiver_reason": _SHOWN_AS_ACL_GATE},
    {"result_field": "results[].result.acl_sidecar_path", "waived_surfaces": ["rest", "mcp"], "waiver_reason": _SHOWN_AS_ACL_GATE},
    {"result_field": "results[].result.acl_warning", "waived_surfaces": ["rest", "mcp"], "waiver_reason": _SHOWN_AS_ACL_GATE},
    {"result_field": "results[].acl_gate.allowed_principals", "rest_field": "results[].acl_gate.allowed_principals", "mcp_field": "results[].acl_gate.allowed_principals"},
    {"result_field": "results[].acl_gate.source", "rest_field": "results[].acl_gate.source", "mcp_field": "results[].acl_gate.source"},
    {"result_field": "results[].acl_gate.sidecar_path", "rest_field": "results[].acl_gate.sidecar_path", "mcp_field": "results[].acl_gate.sidecar_path"},
    {"result_field": "results[].acl_gate.warnings", "rest_field": "results[].acl_gate.warnings", "mcp_field": "results[].acl_gate.warnings"},
    {"result_field": "acl_filtered", "rest_field": "acl_filtered", "mcp_field": "acl_filtered"},
    {"result_field": "excluded_collections[].name", "rest_field": "excluded_collections[].name", "mcp_field": "excluded_collections[].name"},
    {"result_field": "excluded_collections[].reason", "rest_field": "excluded_collections[].reason", "mcp_field": "excluded_collections[].reason"},
    {"result_field": "embedding_model", "rest_field": "embedding_model", "mcp_field": "embedding_model"},
    {"result_field": "hyde_applied", "rest_field": "hyde_applied", "mcp_field": "hyde_applied"},
    {"result_field": "rag_fusion_applied", "rest_field": "rag_fusion_applied", "mcp_field": "rag_fusion_applied"},
    {"result_field": "rag_fusion_queries_used", "rest_field": "rag_fusion_queries_used", "mcp_field": "rag_fusion_queries_used"},
    {"result_field": "rag_fusion_attempted", "rest_field": "rag_fusion_attempted", "mcp_field": "rag_fusion_attempted"},
    {"result_field": "graph_expansion_applied", "rest_field": "graph_expansion_applied", "mcp_field": "graph_expansion_applied"},
    {"result_field": "expansion_used", "rest_field": "expansion_used", "mcp_field": "expansion_used"},
    {"result_field": "expansion_warning", "rest_field": "expansion_warning", "mcp_field": "expansion_warning"},
    {"result_field": "applied_filters.file_type", "rest_field": "applied_filters.file_type", "mcp_field": "applied_filters.file_type"},
    {"result_field": "applied_filters.source_path_prefix", "rest_field": "applied_filters.source_path_prefix", "mcp_field": "applied_filters.source_path_prefix"},
    {"result_field": "applied_filters.source_path_glob", "rest_field": "applied_filters.source_path_glob", "mcp_field": "applied_filters.source_path_glob"},
    {"result_field": "applied_filters.indexed_after", "rest_field": "applied_filters.indexed_after", "mcp_field": "applied_filters.indexed_after"},
    {"result_field": "applied_filters.indexed_before", "rest_field": "applied_filters.indexed_before", "mcp_field": "applied_filters.indexed_before"},
    {"result_field": "applied_filters.language", "rest_field": "applied_filters.language", "mcp_field": "applied_filters.language"},
    {"result_field": "applied_filters.include_metadata", "rest_field": "applied_filters.include_metadata", "mcp_field": "applied_filters.include_metadata"},
    {"result_field": "ppr_entities_matched", "rest_field": "ppr_entities_matched", "mcp_field": "ppr_entities_matched"},
    {
        "result_field": "include_metadata",
        "waived_surfaces": ["rest", "mcp"],
        "waiver_reason": _REQUEST_FLAG_NOT_ECHOED,
    },
]
