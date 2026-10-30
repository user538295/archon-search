// C3 — Authorization policy seam: pure policy in services/authz, FastAPI glue stays in server/authz.
namespace AuthzPolicyContract;

model AuthIdentity {
  ownerNamespace: string;
  admin: boolean;
}

// Fixed, sanitized wire text. Never built from an exception message.
model ForbiddenError {
  code: string;
  detail: string;
}

interface AuthorizationPolicy {
  requireAdmin(caller: AuthIdentity): void;
  authorizeKeyCreate(caller: AuthIdentity, requestedNamespace: string, requestedAdmin: boolean): void;
  authorizeKeyListingNamespace(caller: AuthIdentity, requestedNamespace: string | null): string | null;
  canManageKey(caller: AuthIdentity, keyId: string): boolean;
  callerMayRevoke(caller: AuthIdentity, keyId: string): boolean;
}

// What the transport-specific half supplies to the policy: an identity resolved from the request.
interface IdentityResolution {
  identityOf(requestNamespace: string | null, requestAdmin: boolean | null): AuthIdentity;
}
