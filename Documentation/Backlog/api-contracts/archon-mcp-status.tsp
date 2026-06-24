import "@typespec/http";
using Http;

@service(#{ title: "Archon Search Status — MCP Extension" })
namespace ArchonSearchMcpStatus;

/** MCP server bind state when enabled */
model McpStatusDetail {
  enabled: boolean;

  /** null when disabled or not yet bound */
  bindAddress: string | null;
}

/** The fragment of StatusResponse this feature adds */
model McpStatusResponse {
  /** null when mcp.enabled = false */
  mcp: McpStatusDetail | null;
}

@route("/status")
interface StatusEndpoint {
  @get read(): McpStatusResponse;
}
