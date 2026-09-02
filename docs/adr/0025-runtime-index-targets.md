# ADR-0025: Runtime-managed FDP Index ping targets

Date: 2026-09-02
Status: Accepted

## Context

The outbound Index ping (ADR-0020/0021) announces this FDP to FDP Index
instances so they harvest it. Targets came exclusively from
`FDP_INDEX_PING_TARGETS` at boot: choosing an index after deployment meant
editing the environment and restarting, and a deployment that booted with zero
targets never even started the ping loop. Operating a live evaluation instance
made the gap concrete: "announce this FDP to home.fairdatapoint.org" should be
an admin action, not a redeploy.

## Decision

**1. Index targets become admin-managed runtime data.** A Postgres table
(`index_targets`, one row per target) unioned with the read-only env set;
dedupe by normalized URL (lowercase scheme+host, no trailing slash — the env
normalization, applied to both sides). The admin surface, mounted under
`/fdp-api/index` and admin-gated on every route (targets reveal deployment
topology, not FAIR metadata):

| Route | Purpose |
|---|---|
| `GET /fdp-api/index/targets` | Effective set, each entry labeled `source: env\|runtime` with its last ping outcome |
| `POST /fdp-api/index/targets` | Add (400 malformed/SSRF, 409 duplicate incl. env) |
| `DELETE /fdp-api/index/targets/{id}` | Remove (env entries have no id and cannot be removed here) |
| `POST /fdp-api/index/ping` | Ping every effective target now; per-target results |

The generic `runtime_settings` key/value registry was rejected: targets need
per-row identity and per-row ping status (`last_ping_at`/`last_ok`/…), i.e. a
REST collection, not a blob under one key.

**2. The pinger reads targets through an injected async `targets_provider`,
and always starts when one is present.** `IndexPinger` already re-read its
targets on every ping; the provider (the service's env-union-rows view) makes
that read dynamic. The old `start()` early-return (no env targets ⇒ no loop, no
event subscriptions) is bypassed when a provider is injected, so **a deployment
that boots with zero targets starts announcing the moment its first target is
added — no restart**. An empty scheduled run is a free no-op that deliberately
does not arm the on-change throttle. Env-only construction (CLI, tests) keeps
the old disabled-when-empty behavior. `fdp index ping` (the cron path for
`FDP_INDEX_PING_IN_PROCESS=false`) unions the table too.

**3. Ping status is recorded via an `on_results` hook** the pinger calls after
every batch (scheduled, on-change, manual). Runtime rows persist their status;
env targets get an in-memory, best-effort status map — they have no row to own,
and a shadow table for read-only entries isn't worth the machinery. Status
bookkeeping never fails a ping (errors logged, swallowed).

**4. Admin-supplied URLs pass the shared SSRF guard** (`assert_public_url`,
the same threat class as steward-supplied download URLs) — translated to
**400** at this boundary: the guard raises `UpstreamError` (502), which is
right for server-supplied metadata but wrong for the caller's own request body.

## Consequences

- Choosing/leaving an index is an admin API call, effective immediately;
  `POST /fdp-api/index/ping` gives deterministic per-target feedback (no
  auto-ping on add).
- The env variable keeps working unchanged and wins dedupe; it remains the
  right place for infrastructure-pinned targets (e.g. a national index baked
  into a deployment profile).
- Env-target status does not survive a restart; runtime-target status does.
- The client gains an admin screen for this surface as a follow-up
  (`npm run generate-api` after this lands).
