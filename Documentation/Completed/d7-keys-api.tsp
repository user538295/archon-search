namespace D7KeysApi;

/** Only two stored statuses. "Expired" is runtime-computed (status=active AND expires_at <= utcNow), never stored. */
enum KeyStatus {
  active,
  revoked,
}

/** POST /keys request body. expires_at is ISO-8601 UTC datetime; duration strings are parsed by the CLI before calling the REST API. namespace must match _validate_namespace() regex (^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$); invalid namespace returns 422 (S13). */
model KeyCreateRequest {
  `namespace`: string;
  label?: string;
  expires_at?: utcDateTime;
}

/** POST /keys response — token field present once only in this response type. */
model KeyCreateResponse {
  id: string;
  token: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}

/** Key record without the token — used in list and revoke responses. TOML synthetic keys have id=null. */
model KeyResponse {
  id?: string;
  `namespace`: string;
  label?: string;
  created_at: utcDateTime;
  expires_at?: utcDateTime;
  status: KeyStatus;
}

/** GET /keys accepts query params: namespace?: string, status?: 'active' | 'revoked' | 'all' (default: 'active') */
/** GET /keys response. hidden_revoked_count is the count of revoked keys excluded when status filter is 'active' (shown as a hint to the operator when > 0). Always 0 when status filter is 'revoked' or 'all'. */
model KeyListResponse {
  keys: KeyResponse[];
  /** Count of revoked keys excluded from the response. Always 0 when status != 'active'. */
  hidden_revoked_count: int32;
}

/** DELETE /keys/{id} response. */
model KeyRevokeResponse {
  id: string;
  status: KeyStatus;
}

/** POST /keys/rotate request body. Returns 409 with FastAPI default {"detail": string} body when ARCHON_SEARCH_API_KEY env var is set (S23) -- not modeled in TypeSpec; HTTP error bodies use FastAPI's standard HTTPException envelope. */
model KeyRotateRequest {
  grace_seconds?: int32;
}

/** POST /keys/rotate response — token field present once. old_key_id/expires_at present when a previous default key was managed. */
model KeyRotateResponse {
  new_key_id: string;
  token: string;
  /** Always 'active' -- the new key's status. */
  status: KeyStatus;
  old_key_id?: string;
  old_key_expires_at?: utcDateTime;
  /** Distinguishes immediately-revoked vs grace-expiring old key. Absent when no previous default key existed. */
  old_key_status?: KeyStatus;
}
