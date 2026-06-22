namespace D7KeyStoreBoundary;

/** Only two stored statuses. "Expired" is NOT a stored status -- it is a runtime-computed state when status=active AND expires_at <= utcNow. The rotation-revocation guard in the middleware checks both: status=revoked (revoke() was called) and expires_at check (key timed out). */
enum KeyStatus {
  active,
  revoked,
}

/** Duplicate of C1 d7-key-record.tsp KeyRecord -- keep in sync. TypeSpec standalone files cannot import across namespaces without a tspconfig. */
model KeyRecord {
  id?: string;
  token_hash: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}

/** Read-side seam: APIKeyMiddleware calls active_keys() on every request; KeyStore owns the write side. */
interface KeyStoreRead {
  /** Returns records where status=active AND (expires_at is null OR expires_at > utcNow). Comparison is strict: a key at exactly its expiry instant is considered expired. Emits an INFO log on first rejection of each expired key (suppressed thereafter via an in-memory _logged_expired_ids set). Does not acquire the write lock. */
  active_keys(): KeyRecord[];
  /** Returns ALL records regardless of status or expiry — for operator display. Includes TOML synthetic records when loaded. Applies no filtering; route handler applies query-param filters (namespace, status). Calls load() on every invocation (disk-read-on-demand). */
  list_keys(): KeyRecord[];
  /** Returns the raw list of all KeyRecord objects from disk (no filtering). Called by active_keys() and list_keys(). */
  load(): KeyRecord[];
}

/** Write-side seam: route handlers + CLI call these; KeyStore is the sole writer of keys.json. */
interface KeyStoreWrite {
  /** Returns both the new key's id and the raw token (token shown to operator once; id needed for KeyCreateResponse). */
  create(ns: string, label?: string, expires_at?: utcDateTime): {id: string, token: string};
  /** Idempotent: revoke on an already-revoked key is a no-op (not KeyError). Raises KeyError only for nonexistent IDs. */
  revoke(id: string): void;
  /** Returns new_key_id + new_token (for KeyRotateResponse) and old_record (null if no previous managed default). Route handler writes .search.env. */
  op rotate_default_key(current_token: string, grace_seconds: int32): {new_key_id: string, new_token: string, old_record?: KeyRecord};
}
