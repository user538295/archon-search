# Security Guide

## Authentication

Tokens are JWT RS256 signed. Verify the signature against the JWKS endpoint (`/.well-known/jwks.json`) on every request. Cache the keys with a 1-hour TTL.

Required claims: `sub` (user ID), `exp` (expiry), `aud` (must equal configured audience).

## Authorization

Role-based access control. Roles are embedded in the JWT `roles` claim as a list of strings. Each endpoint declares its required role. Missing roles → `403 Forbidden`.

## Input Validation

All request bodies are validated against JSON Schema before reaching handler logic. Reject unexpected fields with `additionalProperties: false`.

## Rate Limiting

Per-IP rate limiting: 60 requests/minute for anonymous, 300/minute for authenticated users. Respond with `429` and include a `Retry-After` header.

## Secrets Management

Never log secrets. Never embed secrets in code. Load from environment variables or a secrets manager (Vault, AWS Secrets Manager). Rotate tokens every 90 days.

## TLS

TLS 1.2 minimum. Prefer 1.3. Disable weak ciphers. Use HSTS with a 1-year `max-age`.
