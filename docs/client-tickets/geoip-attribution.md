# Client ticket: DB-IP attribution in the metrics dashboard

**Repo:** `fdp-client`
**Type:** UI / compliance (no API change)
**Priority:** Required before the geo-enabled server image ships publicly
**Origin:** `fdp-server` — GeoIP database switched from MaxMind GeoLite2 to DB-IP IP-to-City Lite (see server `docs/adr/0002-anonymous-metrics.md`, note dated 2026-06-16)

## Background

The server now derives geographic metrics (country/region/city distribution) using the **DB-IP IP-to-City Lite** database instead of MaxMind GeoLite2. DB-IP's lite database is licensed under **Creative Commons Attribution 4.0 International (CC BY 4.0)**, which permits redistribution **but requires visible attribution wherever the data — or results derived from it — are displayed**.

The server records this obligation in its `NOTICE` file (covers redistribution). The **user-facing attribution** must live in the client, because that's where the geographic metrics are rendered to users.

## What needs to change

Add a visible credit to DB-IP on the metrics dashboard, specifically wherever geographic data is shown (the country/region/city distribution view). A persistent dashboard footer is acceptable and preferred.

### Required attribution text

> IP geolocation by [DB-IP](https://db-ip.com)

- The link **must** point to `https://db-ip.com`.
- Must be visible (not hidden behind a tooltip/modal-only) on any view that displays geographic metrics.
- A single footer line on the metrics dashboard satisfies this if that footer is present on the geo view.

## Acceptance criteria

- [ ] The metrics dashboard shows "IP geolocation by DB-IP" with a working link to https://db-ip.com on every view that renders geographic distribution.
- [ ] The credit is present in the default theme and remains legible (contrast/size) — it is a license obligation, not optional chrome.
- [ ] If the client has an "about/licenses/attributions" page, DB-IP (CC BY 4.0) is also listed there.

## Notes for the implementing agent

- **No API contract change.** The metrics API already returns the geographic fields (ISO 3166-1 alpha-2 country code, region, city). This is purely a presentation addition — do **not** regenerate API types or expect a new endpoint.
- The country code can be `null` ("unknown") when the lookup produced no answer; that behaviour is unchanged and unrelated to this ticket.
- Scope is limited to attribution. No changes to how geo data is fetched or displayed otherwise.

## References

- License: https://creativecommons.org/licenses/by/4.0/
- Data source: https://db-ip.com
- Server ADR: `fdp-server` `docs/adr/0002-anonymous-metrics.md` (note dated 2026-06-16)
- Server NOTICE: `fdp-server` `NOTICE`
