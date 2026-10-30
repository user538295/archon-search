// C3 — Authorization policy seam: transport-free policy in services/authz, FastAPI glue stays in server/authz.
namespace AuthzPolicyContract;

model AuthIdentity {
  `namespace`: string;
  admin: boolean;
}

// Fixed, sanitized wire text (ADMIN_REQUIRED_DETAIL, ADMIN_KEY_GRANT_DETAIL, FOREIGN_NAMESPACE_DETAIL). Never built from an exception message.
enum ForbiddenDetail {
  adminRequired: "this operation requires an admin API key",
  adminKeyGrant: "only an admin API key can create an admin key",
  foreignNamespace: "a non-admin API key can only manage keys in its own namespace",
}

// ForbiddenError is an exception class whose message is one of the fixed details; it carries no code field.
model ForbiddenError {
  detail: ForbiddenDetail;
}

// Wire code the MCP adapter attaches to a ForbiddenError (the FORBIDDEN_CODE constant, separate from the exception).
const FORBIDDEN_CODE = "forbidden";

// The slice of the core key_manager.KeyRecord the policy reads.
model KeyRecord {
  // Null for TOML synthetic records (they are not managed keys); managed keys always have an id.
  id: string | null;
  `namespace`: string;
  admin: boolean;
}

// The core key_manager.KeyStore port, opaque here; only its async listKeys is used.
model KeyStoreHandle {}

interface AuthorizationPolicy {
  // A null identity (unauthenticated) is refused.
  requireAdmin(identity: AuthIdentity | null): void;
  authorizeKeyCreate(identity: AuthIdentity, `namespace`: string, admin: boolean): void;
  authorizeKeyListingNamespace(identity: AuthIdentity, `namespace`: string | null): string | null;
  canManageKey(identity: AuthIdentity, record: KeyRecord): boolean;
  // Async and reads the key store, which is why the policy is transport-free rather than pure. It moves with the policy
  // because it has no transport coupling and services may import the core key manager.
  callerMayRevoke(keyStore: KeyStoreHandle, identity: AuthIdentity, keyId: string): boolean;
}

// What the transport-specific half (server/authz, stays) supplies: an identity read from the request state, or none.
interface IdentityResolution {
  identityOf(requestNamespace: string | null, requestAdmin: boolean | null): AuthIdentity | null;
}
