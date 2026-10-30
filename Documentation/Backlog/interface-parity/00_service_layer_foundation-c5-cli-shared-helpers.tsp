// C5 — Shared CLI helpers (brief In Scope 4): one API key resolution and one default server URL.
namespace CliSharedHelperContract;

enum ApiKeySource {
  option,
  environment,
  keyFile,
}

model ResolvedApiKey {
  value: string;
  source: ApiKeySource;
}

// Exit 1 with a fixed message on stderr when no key resolves; behaviour is unchanged from today.
model NoApiKeyFound {
  exitCode: int32;
  message: string;
}

model ServerTarget {
  // One default for every command that connects to the server.
  defaultApiUrl: string;
}

interface CliHelper {
  resolveApiKey(optionValue: string | null): ResolvedApiKey | NoApiKeyFound;
  // The one default every `--api-url` option uses.
  defaultServerTarget(): ServerTarget;
}

// Not part of this helper: the best-effort lookups (status, and `_fetch_server_status` in backup_cmd and maintenance_cmd) keep their own
// `api_key or load_key()` that returns no key instead of exiting; key_cmd and maintenance_cmd keep their try/except around resolveApiKey.
