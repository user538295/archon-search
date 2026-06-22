namespace D7KeyStoreBoundary;

enum KeyStatus {
  active,
  revoked,
}

model KeyRecord {
  id: string;
  token_hash: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}

/** Read-side seam: APIKeyMiddleware calls active_keys() on every request; KeyStore owns the write side. */
interface KeyStoreRead {
  active_keys(): KeyRecord[];
  list_keys(): KeyRecord[];
}

/** Write-side seam: route handlers + CLI call these; KeyStore is the sole writer of keys.json. */
interface KeyStoreWrite {
  create(ns: string, label?: string, expires_at?: utcDateTime): string;
  revoke(id: string): void;
}
