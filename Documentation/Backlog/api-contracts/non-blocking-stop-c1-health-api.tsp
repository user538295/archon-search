import "@typespec/http";
using Http;

@service(#{ title: "Archon Search Health API" })
namespace HealthApi;

// Liveness probe. Auth-exempt (listed in _EXEMPT_PATHS).
//
// Contract change (this feature): /health is no longer liveness-only /
// always-200. When the server is draining (app.state.stopping == True,
// set at the top of the FastAPI lifespan `finally` block on SIGTERM),
// it returns 503 Service Unavailable — the "stopping" state. Once the
// process exits, the port stops accepting connections (connection
// refused) — the "stopped" state, not modelled here.
//
// Observable states, in order:
//   running  -> 200 (HealthRunning)
//   stopping -> 503 (HealthStopping)
//   stopped  -> connection refused (transport-level, not an HTTP response)

model McpStatusDetail {
  enabled: boolean;
  bound: boolean;
}

// 200 body while the server is accepting work.
model HealthRunning {
  @statusCode statusCode: 200;
  status: "running";
  version: string;
  mcp: McpStatusDetail | null;
}

// 503 body while the server is draining and shutting down.
// Open questions: whether the `status` field is included at all (Q1)
// and whether a Retry-After header is added (Q2).
model HealthStopping {
  @statusCode statusCode: 503;
  status: "stopping";
  version: string;
  mcp: McpStatusDetail | null;
}

@route("/health")
interface Health {
  @get check(): HealthRunning | HealthStopping;
}
