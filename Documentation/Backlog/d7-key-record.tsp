namespace D7KeyRecord;

/** Only two stored statuses. "Expired" is NOT a stored status -- it is a runtime-computed state when status=active AND expires_at <= utcNow. */
enum KeyStatus {
  active,
  revoked,
}

/** Durable key record persisted in keys.json. token_hash is SHA-256 hex of the raw bearer token. */
model KeyRecord {
  id?: string;
  token_hash: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}
