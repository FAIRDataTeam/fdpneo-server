# ADR-0002: Anonymous-by-design metrics pipeline

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP needs usage metrics — views per record, downloads, search activity, SPARQL query volume, geographic distribution, unique visitors over time — both for stewards (to understand which of their resources are used) and for operators (to understand system health).

Two paths lead to a metrics system:

1. **Log everything, anonymize later.** The system observes and records user-identifying data (IP, identity, user agent, query content) and applies retention rules and pseudonymization at query or report time.
2. **Anonymize at ingress.** The system observes user-identifying data only long enough to derive non-identifying derivatives (country from IP, daily-rotated visitor hash from IP+UA), then discards the source.

GDPR allows path 1 if the lawful basis is clear and retention/access rules are scrupulous. Path 2 is structurally simpler to defend.

## Decision

The metrics pipeline anonymizes at ingress. Personally identifying data is observed in flight to derive aggregate-safe values, then discarded before any event reaches the metrics module. The metrics module is structurally incapable of producing user-identifying reports because user-identifying data never enters its inputs.

Specifically:
- **IP addresses** are looked up against an embedded MaxMind GeoLite2 database to derive country/region/city, then dropped.
- **User identities** are stripped at the event-bus anonymization layer.
- **Unique visitor counting** uses a daily-rotated salted hash of IP+UA, with the salt held only in memory and rotated every 24 hours. The same visitor hashes the same way within a day, differently across days.
- **Query and search text** are not recorded; only the fact that a query happened, against which endpoint, with what latency.

Raw events are aggregated to hourly buckets within minutes and discarded. Hourly buckets roll up to daily after 48 hours. No source data is retained past that window.

## Alternatives considered

**Log everything with retention rules and tight access control.** Rejected. Even with rigorous access control, the existence of identifying data creates audit, breach, and compliance surface area. The benefit (more flexible future analytics) is outweighed by the cost (compliance burden, breach risk, deployments declining to deploy due to data protection concerns).

**Disable metrics entirely.** Rejected. Stewards have a legitimate need to know how their resources are used, and operators need to know how the system is performing.

**Track only authenticated users, with consent.** Considered. Could supplement the anonymous pipeline with a per-user opt-in panel that gives detailed personal usage history to the consenting user. Deferred: real value, no architectural impact on the base pipeline, can be added in v1.x without changing this decision.

## Consequences

**Easier:**
- GDPR posture is straightforward to articulate. No user-identifying data is retained in the metrics pipeline.
- No cookie banner needed for analytics. The OIDC session cookie is independent.
- Breach impact on metrics data is structurally bounded.

**Harder:**
- Some future analytics features are impossible without further design work. "Top searches" requires k-anonymity (separate decision, separate impact assessment). "Personal usage history" requires a separate opt-in pipeline.
- Pseudonymous-identifier interpretation of daily hashes under GDPR varies by jurisdiction. We document the design and provide a configuration option to disable unique-visitor counting for deployments whose legal context requires it.

**Requires discipline:**
- Every new event type added in future must be reviewed for what data it carries through the anonymization boundary. The boundary is a code-level layer with a clear contract; adding fields that bypass it would be a category of bug.
