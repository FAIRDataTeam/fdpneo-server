# ADR-0027: Open access by default, ODRL evaluation opt-in

**Status:** Accepted
**Date:** 2026-10-29

## Context

Every authorization decision in the FDP goes through the ODRL policy decision point (ADR-0006, architecture §8): the PDP resolves the Offer in force by walking `dct:isPartOf` up to the system default, evaluates it against the request context, and materializes the decision in the Postgres authorization index (§9.4). The bundled profile seeds a system-default Offer that grants public read and steward modify, so a fresh deployment *behaves* openly — but it gets there through the full engine.

That is a lot of machinery for the common case. Feedback from first-time administrators and the TRL assessment pointed at the same friction:

- A beginner cannot tell whether a record is invisible because of a draft state, a mis-authored Offer, an inherited Offer two levels up, or a stale cache row. Each of these has surfaced as a support question.
- The ODRL editor is the first admin surface many operators meet, and it reads as a prerequisite rather than an option.
- Upgrades that moved the Offer IRI (ADR-0012) produced "policy denies modify" on every write until the profile was re-applied, because *no* decision can be made without an Offer.
- Operationally, every uncached decision is a store round-trip plus a Postgres write, and the search index's `anon_read` flag and the authorization cache both lag behind inherited policy changes.

Most deployments want exactly one rule: everyone may read, our stewards may write. They should get it without authoring, inheriting or caching anything.

## Decision

Access control has a **mode**, and the default is **open**.

| Mode | `read` | `modify` / `delete` / `distribute` | Engine |
|---|---|---|---|
| `open` (default) | PERMIT for every subject, anonymous included | PERMIT iff the subject holds one of the configured *writer roles* (`admin`, `steward` by default) | Pure function. No Offer resolution, no cache. |
| `odrl` | Full ODRL evaluation as in ADR-0006 / §8 | Full ODRL evaluation | Resolver + evaluator + authorization index, unchanged. |

**Where the switch lives.** The mode is the runtime setting `access.control` (`{"mode": "open" | "odrl", "writer_roles": [...]}`), editable by administrators through the existing `PUT /settings/{key}` API and the client's Instance settings page. Its registered default is seeded at composition time from `FDP_ACCESS_CONTROL_MODE`, so a docker-compose variable sets the initial mode and `DELETE /settings/access.control` reverts to it. The runtime override wins over the environment.

**Where the branch lives.** Inside the policy module. `RequestScopedPDP` resolves the effective configuration per call (through a short-TTL cached reader that the settings repository invalidates in-process on write) and constructs the `PDP` with it; `PDP.authorize` / `authorize_many` / `authorized_graphs` branch on the mode *before* touching the resolver or the cache. Every PEP keeps calling `authorize(subject, action, resource)` and is unaware of the mode. The invariant "all policy decisions go through the security enforcer" holds — the enforcer simply has two engines.

**What open mode does not change.**

- Publication state (ADR-0010) still gates visibility. A draft is 404 to anyone who cannot curate it; "curate" is still an ODRL-`modify` question, which open mode answers with the writer-role check.
- Managed policies, licenses and records' `dct:rights` are stored, validated and served exactly as before. They are not consulted. The Policies page says so.
- Admin-only surfaces (schemas, resource definitions, users, settings, factory reset) remain gated by the `admin` role as today — they never went through ODRL.
- Internal graphs are stripped from bulk answers in both modes.

**Derived state on a mode switch.** The authorization index holds ODRL decisions and the search index holds `anon_read` flags evaluated under whichever mode was active. A switch (in either direction) drops the authorization cache and rebuilds the search index in a background task. Open mode writes nothing to the cache, so rows that survive a crash mid-rebuild are at worst stale ODRL decisions, which the next ODRL-mode write invalidates as before.

**Bulk lookups without a candidate set.** `authorized_graphs(ctx, action)` — the cache-only enumeration — returns the empty set in open mode: there is no cache and no candidate set to apply the open rule to. Every production PEP already goes through the state gate, which passes explicit candidates to `authorize_many`. The one consumer without candidates, the dashboard's "editable" tab, is empty in open mode; the "owned" and "recent" tabs are unaffected.

## Alternatives considered

**Keep ODRL always on and just make the default Offer simpler.** This is the status quo; the default Offer already *is* "public read, steward modify". The friction is not the Offer's content but the engine between the admin and the answer: inheritance, cache, Offer IRIs that must exist. Rejected.

**A per-record "public" flag instead of a mode.** Still requires ODRL for the non-public records, so the beginner still meets the engine on day two; and two overlapping mechanisms for the same decision are harder to explain than one switch. Rejected.

**Replace ODRL with role-based access control.** Communities around the FDP are aligned on ODRL for interoperable access conditions (ADR-0006), and existing deployments depend on party, group and time constraints. Open mode is deliberately a *subset* of what the default Offer expresses, not a third model. Rejected.

**Environment variable only, no runtime switch.** Simpler, but it makes the first deliberate step into ODRL a redeploy, and it hides the current mode from the admin UI. The settings key already exists for exactly this kind of operator choice (ADR-0012 era). Rejected.

**Decide the mode in each PEP.** Would leak the mode into the metadata, access and data modules and violate the "no module evaluates policy outside `policy`" boundary. Rejected.

## Consequences

**Easier:**
- A new deployment is useful immediately: no policy to author, no Offer to seed, and no "why can't I see my record" caused by authorization. Open-mode decisions are O(1), with no store or Postgres round-trip.
- The ODRL editor becomes an advanced feature with a clear entry point (one setting) and a visible status on its own page.
- Upgrades and profile re-applies cannot lock stewards out through a missing Offer while the deployment is in open mode.

**Harder:**
- **Existing ODRL-reliant deployments must opt in on upgrade** (`FDP_ACCESS_CONTROL_MODE=odrl` or the setting). The changelog calls this out as the breaking consequence of changing the default.
- Two engines to reason about in security review. Mitigated by the open engine being ~20 lines with no I/O and by the test suite running every historic policy test under `odrl` while open mode has its own tests.
- The dashboard's "editable" list is empty in open mode until it gains a candidate set (a follow-up, not blocking).
- In multi-replica deployments the mode converges within the cached reader's TTL (five seconds) rather than instantly; the in-process invalidation is immediate on the replica that took the write.

**Evolution path:**
- A future "simple" mode between the two (e.g. per-container writer roles) would be a third `AccessControlConfig`, not a new PEP seam.
- The dashboard's editable tab can pass the state reader's record set as candidates to `authorize_many`, making it complete in both modes.
