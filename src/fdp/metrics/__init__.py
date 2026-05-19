"""Metrics module — anonymized event pipeline and dashboard API.

Responsibilities:

* Subscribe to events on the in-process event bus (record views, downloads,
  search activity, SPARQL queries, login events).
* Apply the anonymization layer at ingress: derive country/region/city from
  IP via GeoLite2 and drop the IP, strip user identity, compute the
  daily-rotated visitor hash, discard query text. (See ADR-0002.)
* Aggregate to hourly buckets in Postgres within minutes; roll up to daily
  after 48 hours; discard raw events.
* Expose a dashboard API for the client to render charts.

Non-responsibilities:

* Does *not* observe user-identifying data after ingress. The anonymization
  layer is structural — the rest of this module cannot see what the
  anonymization layer dropped.
* Does *not* persist anything in the triple store. Metrics are operational
  state and belong in Postgres.

Public interface (planned):

* ``api`` — FastAPI router for the dashboard endpoints.
* ``pipeline`` — event-bus subscriber and the anonymization layer.
* ``aggregation`` — rollup logic from raw to hourly to daily.

This module has the strictest review bar: any change that touches the
anonymization boundary needs particularly careful review.

See architecture section 11, ADR-0002, and CLAUDE.md.
"""
