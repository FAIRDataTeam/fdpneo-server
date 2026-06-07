# Security Policy

The FAIR Data Point server is metadata software that may hold sensitive
(including clinical) metadata. We take security seriously and welcome
coordinated disclosure.

## Reporting a vulnerability

**Please report privately — do not open a public issue or PR.**

- **Preferred:** GitHub *private vulnerability reporting* — the **Security** tab →
  **Report a vulnerability** on this repository.
- **Email:** `security@CHANGE-ME.example` (replace with the project's real
  security contact before publishing).

Include the affected version/commit, a reproduction, and the impact. We aim to
acknowledge within **3 business days** and agree a remediation timeline; we
credit reporters unless they ask otherwise. Please give us reasonable time to
fix before any public disclosure.

## Supported versions

Pre-1.0 software: security fixes land on `main`. Pin a commit and watch releases.

| Version | Supported |
|---------|-----------|
| `main`  | ✅        |
| < 0.1   | ❌        |

## Deployment is part of security

Several controls are the operator's responsibility. **Before production, follow
the hardening runbook:** [`docs/security/deployment-hardening.md`](docs/security/deployment-hardening.md).
Highlights: TLS + HSTS at the edge, security headers, request rate limiting, a
**SPARQL-conformant triple store** (GraphDB/Fuseki — the server self-tests
named-graph isolation at boot and fails closed otherwise), secret management +
rotation (the bundled `deploy/` dev credentials are **dev-only**), network
segmentation, and encryption-at-rest for Postgres and the triple store.

## Built-in protections

OIDC-only auth (no password store), ODRL authorization with one-graph-per-record
isolation, a fail-closed SPARQL access rewriter (`SERVICE`/`LOAD` rejected),
anonymized-by-design metrics, CSPRNG API keys, baseline security headers, request
rate/body limits, a JSON-LD remote-context (SSRF) guard, and a consistent error
envelope. See the audit + remediation record:
[`docs/security/audit-2026-06-07.md`](docs/security/audit-2026-06-07.md).

## Automated scanning

CI runs dependency vulnerability scanning (`pip-audit`) and publishes a CycloneDX
SBOM on every pull request, on pushes to `main`, and weekly —
[`.github/workflows/security-scan.yml`](.github/workflows/security-scan.yml).
