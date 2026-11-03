"""The parity registry (brief C12, plan C4): one capability per roadmap slice S1-S32, the agreed waivers and the descriptions row.

Plain data: lists, dicts and string constants, no imports and no behaviour, so a test can deep-copy and mutate it. The shape
is documented in ``tests/parity/test_parity.py``. Every live REST route, MCP tool and Click command is claimed here exactly as
the live surfaces report it, or waived; ``service_method`` is ``None`` until the slice's service method exists.
When a slice lands, replace its pending waiver with the new surface identifier.
"""

_REASON_1 = "No REST route yet"
_NO_CLI_COMMAND_YET = "No CLI command yet"
_NO_MCP_TOOL_YET = "No MCP tool yet"
_REASON_2 = (
                "Not built: the key `admin` flag is immutable after creation; to elevate, issue a new "
                "time-boxed admin key (roadmap waiver, D3)"
            )
_REASON_3 = "Probe, not a feature (roadmap waiver)"
_DOCS_SURFACE = "HTTP documentation surface, present only when [server].docs_enabled is true (roadmap waiver)"
_OPENAI_SHIM_ADAPTER = (
                           "Protocol adapter, present only when [openai_shim].enabled is true; it calls the "
                           "same services as S1 (roadmap waiver)"
                       )
_LOCAL_OPERATION = (
                       "Local OS or config operation; it acts on the machine, not on the running server "
                       "(roadmap waiver)"
                   )

REGISTRY = [
    {
        "name": "S1 Search (pilot)",
        "service_method": "SearchService.search",
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /search",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "search",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "search",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S2 Search with context",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _REASON_1,
                    "pending_slice": "S2",
                },
            },
            {
                "surface": "mcp",
                "identifier": "search_with_context",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S2",
                },
            },
        ],
    },
    {
        "name": "S3 Explain",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /explain",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "explain",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S3",
                },
            },
        ],
    },
    {
        "name": "S4 Route",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /route",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S4",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S4",
                },
            },
        ],
    },
    {
        "name": "S5 Jobs get and list",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /jobs",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "GET /jobs/{job_id}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S5",
                },
            },
            {
                "surface": "cli",
                "identifier": "jobs list",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "jobs show",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "jobs status",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S6 Job resume and cancel",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /jobs/{job_id}/resume",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "DELETE /jobs/{job_id}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S6",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S6",
                },
            },
        ],
    },
    {
        "name": "S7 Keys",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /keys",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "POST /keys",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "DELETE /keys/{key_id}",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "POST /keys/rotate",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "list_keys",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "create_key",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "revoke_key",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "rotate_key",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "key list",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "key create",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "key revoke",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "key rotate",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S8 List collections",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /collections/",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "list_collections",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "collection list",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S9 Collection info",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /collections/{name}",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": "No REST route for all-collection metadata yet",
                    "pending_slice": "S9",
                },
            },
            {
                "surface": "mcp",
                "identifier": "get_collection_meta",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "get_collections_meta",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "collection info",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S10 List documents",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /collections/{name}/documents",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "list_documents",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S10",
                },
            },
        ],
    },
    {
        "name": "S11 Delete document",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _REASON_1,
                    "pending_slice": "S11",
                },
            },
            {
                "surface": "mcp",
                "identifier": "delete_document",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S11",
                },
            },
        ],
    },
    {
        "name": "S12 Expiring chunks",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /collections/{name}/expiring",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S12",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S12",
                },
            },
        ],
    },
    {
        "name": "S13 Patch collection",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "PATCH /collections/{name}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "update_collection",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S13",
                },
            },
        ],
    },
    {
        "name": "S14 Delete collection",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "DELETE /collections/{name}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S14",
                },
            },
            {
                "surface": "cli",
                "identifier": "collection remove",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S15 Add collection (admin)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S15",
                },
            },
            {
                "surface": "cli",
                "identifier": "collection add",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S16 Ingest",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /ingest",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "ingest_file",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "ingest_directory",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "ingest",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": "REST `documents` input has no MCP equivalent yet; S16 adds one or records a waiver",
                    "pending_slice": "S16",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": "REST `documents` input has no CLI equivalent yet; S16 adds one or records a waiver",
                    "pending_slice": "S16",
                },
            },
        ],
    },
    {
        "name": "S17 Reindex",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/reindex",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S17",
                },
            },
            {
                "surface": "cli",
                "identifier": "collection reindex",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S18 Reindex metadata",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/reindex-metadata",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S18",
                },
            },
            {
                "surface": "cli",
                "identifier": "collection reindex-metadata",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S19 Migrate (with pending)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/migrate",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "GET /collections/{name}/migrations/pending",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S19",
                },
            },
            {
                "surface": "cli",
                "identifier": "collection migrate",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S20 Export",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/export",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "export_collection",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "export",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S21 Import",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/import",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "import_collection",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "import",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S22 Sync (admin)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /sync",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S22",
                },
            },
            {
                "surface": "cli",
                "identifier": "sync",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S23 Backup trigger (admin)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /backup/trigger",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S23",
                },
            },
            {
                "surface": "cli",
                "identifier": "backup",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S24 Maintenance trigger (admin)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /maintenance/trigger",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S24",
                },
            },
            {
                "surface": "cli",
                "identifier": "maintenance run",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S25 Status",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /status",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S25",
                },
            },
            {
                "surface": "cli",
                "identifier": "status",
                "waiver": {
                    "kind": "permanent",
                    "reason": (
                                "The local service-manager part of status acts on the machine, not on the "
                                "running server (roadmap waiver)"
                            ),
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "backup status",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "maintenance status",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S26 Indexing-state",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /indexing-state",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S26",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S26",
                },
            },
        ],
    },
    {
        "name": "S27 Telemetry stats and entries (admin)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /telemetry/stats",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "GET /telemetry/entries",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S27",
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S27",
                },
            },
        ],
    },
    {
        "name": "S28 Graph single collection",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /graph/{collection}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "get_graph",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S28",
                },
            },
        ],
    },
    {
        "name": "S29 Graph cross-collection",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /graph/cross-collection",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "get_graph_cross_collection",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S29",
                },
            },
        ],
    },
    {
        "name": "S30 Graph impact",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /graph/{collection}/impact/{symbol}",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "graph_impact",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_CLI_COMMAND_YET,
                    "pending_slice": "S30",
                },
            },
        ],
    },
    {
        "name": "S31 Graph rebuild communities",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /graph/{collection}/rebuild-communities",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": _NO_MCP_TOOL_YET,
                    "pending_slice": "S31",
                },
            },
            {
                "surface": "cli",
                "identifier": "graph build-communities",
                "waiver": None,
            },
        ],
    },
    {
        "name": "S32 Graph view and view-token",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /graph/{collection}/view",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "POST /graph/{collection}/view-token",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "permanent",
                    "reason": "HTTP-specific browser credential; the MCP interface is waived (roadmap waiver, D4)",
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "pending",
                    "reason": "No `graph view` command yet",
                    "pending_slice": "S32",
                },
            },
        ],
    },
    {
        "name": "Key admin toggle (waived)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": None,
                "waiver": {
                    "kind": "permanent",
                    "reason": _REASON_2,
                    "pending_slice": None,
                },
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "permanent",
                    "reason": _REASON_2,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": None,
                "waiver": {
                    "kind": "permanent",
                    "reason": _REASON_2,
                    "pending_slice": None,
                },
            },
        ],
    },
    {
        "name": "Descriptions (LLM collection descriptions)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "PUT /collections/{name}/description",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "DELETE /collections/{name}/description",
                "waiver": None,
            },
            {
                "surface": "rest",
                "identifier": "POST /collections/{name}/describe",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "set_collection_description",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": "clear_collection_description",
                "waiver": None,
            },
            {
                "surface": "mcp",
                "identifier": None,
                "waiver": {
                    "kind": "permanent",
                    "reason": (
                                "Agents get no tool to rewrite a collection's description: a rewrite calls the "
                                "language model (cost with a cloud provider, no per-key limit); descriptions "
                                "plan C8 keeps the manual set and clear tools and ADR 19 lists a describe MCP "
                                "tool as out of scope (roadmap D9)"
                            ),
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "collection set-description",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "collection clear-description",
                "waiver": None,
            },
            {
                "surface": "cli",
                "identifier": "collection describe",
                "waiver": None,
            },
        ],
    },
    {
        "name": "Probes and HTTP documentation (waived)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "GET /health",
                "waiver": {
                    "kind": "permanent",
                    "reason": _REASON_3,
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /ready",
                "waiver": {
                    "kind": "permanent",
                    "reason": _REASON_3,
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /openapi.json",
                "waiver": {
                    "kind": "permanent",
                    "reason": "HTTP documentation surface (roadmap waiver)",
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /docs",
                "waiver": {
                    "kind": "permanent",
                    "reason": _DOCS_SURFACE,
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /docs/oauth2-redirect",
                "waiver": {
                    "kind": "permanent",
                    "reason": _DOCS_SURFACE,
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /redoc",
                "waiver": {
                    "kind": "permanent",
                    "reason": _DOCS_SURFACE,
                    "pending_slice": None,
                },
            },
        ],
    },
    {
        "name": "MCP transport mount (waived)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "/mcp",
                "waiver": {
                    "kind": "permanent",
                    "reason": (
                                "The MCP transport endpoint itself, not a feature; present only when "
                                "[mcp].enabled is true and mounted in the app lifespan (roadmap waiver)"
                            ),
                    "pending_slice": None,
                },
            },
        ],
    },
    {
        "name": "OpenAI-compatible shim (waived)",
        "service_method": None,
        "claims": [
            {
                "surface": "rest",
                "identifier": "POST /v1/chat/completions",
                "waiver": {
                    "kind": "permanent",
                    "reason": _OPENAI_SHIM_ADAPTER,
                    "pending_slice": None,
                },
            },
            {
                "surface": "rest",
                "identifier": "GET /v1/models",
                "waiver": {
                    "kind": "permanent",
                    "reason": _OPENAI_SHIM_ADAPTER,
                    "pending_slice": None,
                },
            },
        ],
    },
    {
        "name": "Local OS and config operations (waived)",
        "service_method": None,
        "claims": [
            {
                "surface": "cli",
                "identifier": "start",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "serve",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "stop",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "install",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "uninstall",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "wizard",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "config show",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "config get",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
            {
                "surface": "cli",
                "identifier": "config set",
                "waiver": {
                    "kind": "permanent",
                    "reason": _LOCAL_OPERATION,
                    "pending_slice": None,
                },
            },
        ],
    },
]
