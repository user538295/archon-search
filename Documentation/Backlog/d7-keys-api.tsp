namespace D7KeysApi;

enum KeyStatus {
  active,
  revoked,
}

/** POST /keys request body. expires_at is ISO-8601 UTC datetime; duration strings are parsed by the CLI before calling the REST API. */
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

/** GET /keys response. hidden_revoked_count is shown as a hint when > 0 and status filter is 'active'. */
model KeyListResponse {
  keys: KeyResponse[];
  hidden_revoked_count: int32;
}

/** DELETE /keys/{id} response. */
model KeyRevokeResponse {
  id: string;
  status: KeyStatus;
}

/** POST /keys/rotate request body. */
model KeyRotateRequest {
  grace_seconds?: int32;
}

/** POST /keys/rotate response — token field present once. old_key_id/expires_at present when a previous default key was managed. */
model KeyRotateResponse {
  new_key_id: string;
  token: string;
  old_key_id?: string;
  old_key_expires_at?: utcDateTime;
}
