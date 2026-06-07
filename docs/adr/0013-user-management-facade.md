# ADR-0013: User-management facade over the IdP Admin API

**Status:** Accepted
**Date:** 2026-06-07

## Context

ADR-0001 makes the FDP **OIDC-only**: the IdP owns identity, and the server keeps
no internal user store. That holds for *authentication*. But operators still need
to **manage** users — see who exists, grant/revoke the FDP roles `steward` and
`admin`, enable/disable, invite, remove. Today that means logging into the IdP's
own admin console (Keycloak), which:

- the **client cannot offer in-app** (it's a public SPA with no admin credentials,
  and shouldn't have any);
- exposes the full realm-admin surface (passwords, MFA, federation, every realm
  role) when all the FDP needs is a tiny, role-scoped slice.

The client (Phase 9.3) asked for a thin, admin-scoped facade on `fdp-server`,
symmetric with the other managed-config admin surfaces (`/schemas`, `/policies`,
`/licenses`, `/resource-definitions`). It is the last blocker for client Phase 9.

This does **not** reintroduce an internal user store — it **proxies** the IdP's
user-admin operations. But it is a new external dependency (the IdP Admin REST
API) and gives the server privileged credentials, so it warrants an ADR.

## Decision

**1. A `/users` admin facade that proxies the IdP, behind a `UserDirectory` port.**
The `identity` context exposes `/users` (list/search, get, create, update,
delete, and `GET /users/roles`). Every endpoint requires the `admin` role
(`require_auth` + `_require_admin`), exactly like `PUT /policies`. The HTTP layer
talks to a `UserDirectory` protocol; the concrete `KeycloakUserDirectory` adapter
calls Keycloak's Admin REST API. Keeping a port (not Keycloak-direct calls in the
router) matches the storage-adapter pattern and keeps identity vendor-pluggable —
a different IdP can implement the same port later.

**2. The server authorizes via a confidential service-account client.** A
dedicated Keycloak client (`fdp-server`, `serviceAccountsEnabled`) holds a
**least-privilege** subset of `realm-management` roles (`view-users`,
`query-users`, `manage-users`, `view-realm`). The server gets an access token via
the `client_credentials` grant (cached until just before expiry, mirroring the
JWKS client) and calls the Admin API headless. The alternative — forwarding the
logged-in admin's own token (on-behalf/token-exchange) — was rejected: it needs
every admin user granted `realm-management` roles directly in the IdP and depends
on token-exchange being enabled and version-stable.

**3. Capability-gated; off by default.** The facade is built only when the
service-account credentials are configured (`FDP_IDP_ADMIN_CLIENT_ID/SECRET`).
Otherwise the `UserDirectory` is `None`, every `/users` route returns
`503 fdp.service_unavailable`, and `features.user_management` is `false` so the
client hides the admin UI. No deployment is forced to grant the server
realm-admin credentials.

**4. Invite-only creation; passwords never touch the FDP.** `POST /users`
creates the account and triggers Keycloak's `execute-actions-email`
(`UPDATE_PASSWORD` + `VERIFY_EMAIL`). No password field exists in this API.
Passwords, MFA, and federation stay in the IdP's own console.

**5. A curated FDP role set, not the raw realm roles.** `GET /users/roles` and the
`roles` on every `UserInfo` are limited to `{steward, admin}` — the roles the PDP
reads (ADR-0006). IdP machinery roles (`offline_access`, `default-roles-*`, etc.)
are never exposed or assignable here.

**6. Lock-out guards enforced server-side.** The server is the authority: it
rejects removing one's own `admin` role or self-disabling (`409`), and rejects
demoting/disabling/deleting the **last** admin (`409`). The client guards too, but
must not be trusted.

## Alternatives considered

- **On-behalf-of / token-exchange** (use the caller's token) — rejected (see 2).
- **Keycloak-direct in the router** (no port) — rejected; couples the identity
  context to a vendor and the HTTP layer to Admin-API shapes.
- **Allow setting passwords via the API** — rejected; keeps credential material
  off the FDP request path (invite-only).
- **Expose all realm roles** — rejected; needless footgun and leaks IdP internals.
- **Do nothing (link to the IdP console)** — already the fallback for *non-FDP*
  concerns, but it can't give the client in-app role management.

## Consequences

**Easier:** the client gets in-app user/role management symmetric with its other
admin surfaces; operators stop hand-editing roles in Keycloak; the surface is
narrow and auditable.

**Harder / to accept:** the server now holds a privileged service-account
credential (least-privilege `realm-management` subset; secret via env only, never
in any payload — `/info` and `/config` stay secret-free). A new outbound
dependency on the IdP Admin API (mapped to `UpstreamError`/502 on failure).
Invites need SMTP configured in the realm to actually send (the user is still
created, disabled-until-verified, either way). The role-member lookups used for
listing and the last-admin count are unpaged — fine for realm sizes this targets;
revisit with paging if a deployment has very large realms.

**Unchanged:** the authentication / PDP path. Roles still flow from the JWT
`realm_access.roles` (ADR-0006); this facade only *writes* them in the IdP.

## Related decisions

- [ADR-0001](0001-modular-monolith.md) — OIDC-only; the IdP owns identity. This
  facade proxies it without an internal user store.
- [ADR-0006](0006-odrl-profile-permission-prohibition.md) — the PDP reads
  `{steward, admin}`; the curated role set here matches.
- [ADR-0011](0011-api-keys.md) — the other identity-context admin surface; an
  API-key admin can also drive `/users`, and the last-admin guard protects the
  realm even when the caller isn't a realm user.
