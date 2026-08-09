"""S296: MCP ingest_file and search flow must be documented.

Wire-truth: ``archon_search/server/mcp.py`` defines ``ingest_file`` with params
``path``, ``collection``, ``chunk_ttl_seconds``, ``chunk_scopes``; output is
``IngestResultSchema`` with fields ``doc_id``, ``chunks_created``, ``status``,
``error``, ``warnings``, ``code``.

Doc contract:
- ``UserManual/50_ingestion_and_collections.md`` must document the MCP
  ``ingest_file`` tool's parameters and output schema.
- ``DeveloperGuide/05_mcp_integration.md`` must list all ``ingest_file``
  parameters (including ``chunk_ttl_seconds``, ``chunk_scopes``) and output
  fields (including ``warnings``, ``code``).
"""

import inspect
from pathlib import Path

from archon_search.server.mcp_schemas import IngestResultSchema

REPO_ROOT = Path(__file__).parent.parent


def test_mcp_ingest_file_doc_in_ingestion_guide() -> None:
    """50_ingestion must document MCP ingest_file params and output fields."""
    # --- wire truth: IngestResultSchema fields ---
    schema_fields = set(IngestResultSchema.model_fields.keys())
    for expected in ("doc_id", "chunks_created", "status", "error", "warnings", "code"):
        assert expected in schema_fields, (
            f"wire-truth drift: IngestResultSchema missing field `{expected}`"
        )

    doc = (
        REPO_ROOT / "Documentation" / "UserManual" / "50_ingestion_and_collections.md"
    ).read_text(encoding="utf-8")

    # Must have a section that documents the MCP ingest tool
    assert "ingest_file" in doc, (
        "50_ingestion_and_collections.md must document the MCP ingest_file tool"
    )
    # Must name the required parameter
    assert "`path`" in doc or "`path: str`" in doc, (
        "50_ingestion_and_collections.md must name the `path` parameter"
    )
    # Must name all output fields documented at line 180 (S469)
    assert "`doc_id`" in doc, (
        "50_ingestion_and_collections.md must name the `doc_id` output field"
    )
    assert "`chunks_created`" in doc, (
        "50_ingestion_and_collections.md must name the `chunks_created` output field"
    )
    assert "`status`" in doc, (
        "50_ingestion_and_collections.md must name the `status` output field"
    )
    assert "`warnings`" in doc or "warnings" in doc, (
        "50_ingestion_and_collections.md must name the `warnings` output field"
    )
    assert "`code`" in doc or "`code: str" in doc, (
        "50_ingestion_and_collections.md must name the `code` output field"
    )


def test_mcp_integration_doc_ingest_file_complete() -> None:
    """DeveloperGuide/05 must list all ingest_file params and output fields."""
    doc = (
        REPO_ROOT / "Documentation" / "DeveloperGuide" / "05_mcp_integration.md"
    ).read_text(encoding="utf-8")

    # All four ingest_file params must be documented
    assert "chunk_ttl_seconds" in doc, (
        "05_mcp_integration.md must document the chunk_ttl_seconds parameter"
    )
    assert "chunk_scopes" in doc, (
        "05_mcp_integration.md must document the chunk_scopes parameter"
    )
    # Output fields
    assert "warnings" in doc, (
        "05_mcp_integration.md must document the warnings output field"
    )
    assert "`code`" in doc or "code:" in doc or "`code: str" in doc, (
        "05_mcp_integration.md must document the code output field"
    )


def test_mcp_integration_doc_no_stale_unverified_wiring_note() -> None:
    """DeveloperGuide/05 must not claim MCP is unwired (it IS mounted at /mcp)."""
    # Wire-truth: app.py mounts MCP at /mcp
    app_src = (
        REPO_ROOT / "archon_search" / "server" / "app.py"
    ).read_text(encoding="utf-8")
    assert 'app.mount("/mcp"' in app_src, (
        "wire-truth drift: app.py no longer mounts MCP at /mcp"
    )

    doc = (
        REPO_ROOT / "Documentation" / "DeveloperGuide" / "05_mcp_integration.md"
    ).read_text(encoding="utf-8")
    assert "create_mcp_http_app` has no caller" not in doc, (
        "05_mcp_integration.md still claims create_mcp_http_app has no caller "
        "— MCP is wired in app.py since the mount was added"
    )
