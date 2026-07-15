namespace SmokeServerContract;

// Port injected via ARCHON_SEARCH_PORT env var; must be in 1–65535.
// DataDir injected via ARCHON_SEARCH_DATA_DIR env var.
// ApiKey injected via ARCHON_SEARCH_API_KEY env var; used by CLI commands
// and REST calls that require Bearer auth.
model ServerConfig {
  port: int32;
  dataDir: string;
  apiKey: string;
}

// capturedStderr is always populated on failure so the test suite can
// surface a server crash without a silent timeout.
model ReadinessResult {
  ready: boolean;
  capturedStderr: string;
}

// returnCode must not be 1 or 2 (those indicate abnormal termination).
// SIGTERM yields 0, -15, or 143 depending on OS/wrapper — all acceptable.
model TeardownResult {
  returnCode: int32;
}

interface SmokeServerProcess {
  // Spawn `archon-search serve` as subprocess with config encoded in env vars.
  start(config: ServerConfig): void;
  // Poll GET /health (auth-exempt) at most timeoutSeconds; resolve when 200.
  awaitReady(timeoutSeconds: int32): ReadinessResult;
  // SIGTERM → wait 10 s → SIGKILL fallback. returnCode not in {1, 2}.
  teardown(): TeardownResult;
}
