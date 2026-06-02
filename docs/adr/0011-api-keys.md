# ADR-0011: API keys as an alternate credential for an IdP-owned identity

**Status:** Accepted
**Date:** 2026-06-02

## Context

The FDP is OIDC-only (ADR-0001): every request authenticates by presenting a JWT the configured IdP issued, identity is read fresh from each token, and there is no local user table. That is the right default for human, browser-driven access, but it makes **machine-to-machine** use awkward. A CI job that publishes records, a harvesting script, or a cron task has no interactive browser to run the Authorization-Code-with-PKCE flow. The OIDC answer — a `client_credentials` grant — works, but it requires an operator to register and manage a confidential client per automation in Keycloak, which is heavy for what is often "let this one script act as me."

The reference implementation (FAIRDataPoint, Java/Spring) issues long-lived **API keys** per user for exactly this. We want the same affordance without reintroducing the things ADR-0001 deliberately omitted (a password store, FDP-issued session JWTs, a competing notion of identity).

Two design tensions have to be resolved:

1. **Identity.** An API key must not become a second, FDP-owned identity. ADR-0001 says the IdP owns identity.
2. **Authorization freshness.** A long-lived key has no fresh token to read roles from, yet a key must not become a *frozen privilege grant*: if the owner's roles change or their access is revoked at the IdP, the key has to reflect that. But automatically resolving a subject's *current* IdP roles with no token in hand depends on IdP role-introspection / role-to-FDP-role mapping, which architecture §15 explicitly **defers to v1.x**.

## Decision

**1. An API key is a credential, not an identity.** A key always references an existing OIDC `subject` (`<issuer>#<sub>`); minting one requires an authenticated OIDC session, and the key can only ever act *as that subject*. It mints no new identities and issues no FDP JWT. This keeps ADR-0001 intact: the IdP still owns identity; the key is just a second way to present an already-IdP-owned one. (This is the one sanctioned exception to "every request carries an IdP JWT": a key is an FDP-issued *bearer credential that names an IdP subject*, not an FDP-issued *identity*.)

**2. Token format and storage.** A key is `fdpk_` + ~32 bytes of URL-safe randomness (~190 bits). It is shown **once**, at creation, and never stored. We persist `sha256(token)` and look the key up by that hash (indexed equality). A fast hash is correct here — API keys are high-entropy, so the password-hashing defenses (bcrypt/argon2) that exist to slow brute force against *low*-entropy secrets would only add per-request latency for no security gain. A short `display_prefix` (e.g. `fdpk_AB12…wxyz`) is stored so the owner can recognise a key in a list without revealing the secret.

**3. Authentication dispatch is by prefix, not fallback.** The auth middleware routes a bearer token that starts with `fdpk_` to the API-key path and everything else to the JWT path. This is preferred over "try JWT, and on failure try the key": it keeps the JWT hot path free of a database round-trip, and it stops a flood of malformed/expired JWTs from amplifying into API-key table lookups. A `fdpk_` token that does not resolve (unknown, revoked, or expired) is `401`, exactly like a bad JWT.

**4. Roles are resolved live from the subject's last-known IdP assertion, with the mint-time snapshot as a seed/fallback.** This is how a long-lived key stays faithful to the owner's current authorization despite carrying no token:

- A `subject_principal` table records, per subject, the roles and groups the IdP most recently asserted. The auth middleware **upserts it on every successful JWT login** (throttled — only when the roles/groups changed or the row is stale), so it always holds the freshest IdP truth the FDP has seen for that subject.
- API-key authentication builds its `RequestContext` from `subject_principal` for that subject, falling back to the snapshot captured on the key at creation only if the subject has never been seen via JWT.
- Authorization itself is already evaluated live per request by the PDP (ODRL over the current Offer, with the authz cache keyed on a role-set hash), so a reduction in resolved roles reduces capability on the very next request.

Consequently: when an owner's IdP roles change, their **next interactive login** refreshes `subject_principal`, and **all of their keys immediately track the new roles** — up or down.

**5. Revocation has two layers.** (a) Per-key `revoke` (the owner, or any admin) flips `revoked_at` and the key fails on its next use — the immediate kill switch. (b) Role reduction at the IdP propagates through `subject_principal` as in (4).

**6. No mandatory expiry.** `expires_at` is optional and caller-set; `FDP_API_KEYS_MAX_TTL_DAYS` caps it. Long-lived keys are allowed (the M2M use case wants them); freshness is handled by (4)+(5), not by forcing rotation.

## Known limitation (the deferred seam)

A **pure service account** — a key whose owner *only ever* uses the key and never logs in interactively again — will not see role changes until either the owner next presents a JWT or an admin revokes/recreates the key, because resolving a subject's current IdP roles with no token in hand requires IdP role-introspection that §15 defers. For human owners who also log in, and for the immediate per-key revoke kill switch, the "reflect changes" requirement is met today. `subject_principal` is deliberately the seam the future IdP-sync (a background refresh, or token introspection) plugs into without touching the key model or the PEPs. This limitation is documented rather than hidden; it does not weaken revocation (layer 5a is always immediate).

## Alternatives considered

**Per-automation `client_credentials` clients in Keycloak.** The pure-OIDC answer; rejected as the *only* option because it pushes per-script confidential-client management onto operators. It remains available and is unaffected by this ADR — a deployment that prefers it simply doesn't issue API keys (`FDP_API_KEYS_ENABLED=false`).

**Freeze roles onto the key at creation (static snapshot).** Simplest, and the literal reading of "snapshot the creator's roles." Rejected as the *effective* source because it makes a long-lived key a permanent privilege grant — exactly what the freshness requirement forbids. We keep the snapshot only as a seed/fallback and audit record; the live `subject_principal` value wins.

**bcrypt/argon2 for the stored key.** Rejected: unnecessary for high-entropy secrets and adds latency to every authenticated request. `sha256` with an indexed lookup is both safe and O(1).

**Store API keys, or the subject-principal record, in the triple store.** Rejected: these are operational/security state, not metadata describing the knowledge graph — they belong in Postgres per ADR-0003, like the authz index and audit log.

## Consequences

**Easier:**

- CI / scripts authenticate with a single `Authorization: Bearer fdpk_…` header, no per-client Keycloak setup.
- Keys track their owner's authorization without a token, via `subject_principal`; admins and owners can revoke immediately.
- The JWT hot path stays DB-free; only `fdpk_` requests hit the key table.

**Harder / costs:**

- One new write appears on the JWT auth path (the throttled `subject_principal` upsert). Bounded by the throttle and opportunistic (a failure to record must never fail the request).
- Correctness of "reflect changes" for pure service accounts is bounded by the deferred IdP-sync (above).
- An API-key request costs one indexed `SELECT` (hash lookup) plus, at most, one throttled `UPDATE` (last-used). Acceptable; a short-TTL in-memory hash→context cache is a possible follow-up, invalidated on revoke.

**Required of operators:**

- Nothing new at the infrastructure level. API-key issuance is gated by `FDP_API_KEYS_ENABLED` and bounded by `FDP_API_KEYS_MAX_PER_USER` / `FDP_API_KEYS_MAX_TTL_DAYS`.

## Related decisions

- [ADR-0001](0001-modular-monolith.md) / architecture §7 — OIDC-only authentication; this ADR adds an *alternate credential* for an IdP-owned identity, not a new identity store.
- [ADR-0003](0003-fixed-postgres-for-operational-state.md) — API-key and subject-principal rows are operational/security state, so they live in Postgres.
- [ADR-0006](0006-odrl-profile-permission-prohibition.md) — ODRL is evaluated live per request, which is what lets resolved-role changes take effect immediately.
- architecture §15 — the deferred IdP role-to-FDP-role mapping that the `subject_principal` seam will eventually use.
