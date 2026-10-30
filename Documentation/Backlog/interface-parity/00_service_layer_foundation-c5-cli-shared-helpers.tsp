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
}
