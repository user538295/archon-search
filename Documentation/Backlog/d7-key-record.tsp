namespace D7KeyRecord;

enum KeyStatus {
  active,
  revoked,
}

/** Durable key record persisted in keys.json. token_hash is SHA-256 hex of the raw bearer token. */
model KeyRecord {
  id: string;
  token_hash: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}
