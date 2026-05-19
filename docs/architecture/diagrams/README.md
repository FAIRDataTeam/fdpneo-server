# Architecture diagrams

This directory contains the diagrams embedded in the [architecture document](../README.md). Each diagram is a self-contained SVG with inline CSS so it renders identically on GitHub, in IDEs that preview Markdown, and when exported.

| File | Used in |
|---|---|
| `01_system_context.svg` | Figure 1 — System context |
| `02_containers.svg` | Figure 2 — Container architecture |
| `03_server_components.svg` | Figure 3 — Server component architecture |
| `04_data_model.svg` | Figure 4 — Named-graph data model per record |
| `05_metrics_privacy.svg` | Figure 9 — Metrics anonymization boundary |
| `06_odrl_offer_agreement.svg` | Figure 5 — ODRL Offer and Agreement lifecycle |
| `07_sparql_access_flow.svg` | Figure 6 — SPARQL access-control flow |
| `08_seq_ldp_patch.svg` | Figure 8 — LDP PATCH sequence |
| `09_deployment_profile.svg` | Figure 10 — Deployment profile bootstrap |
| `10_authorization_index.svg` | Figure 7 — Authorization index lifecycle |

## Regenerating

The SVGs are generated from `generate_svgs.py`. To regenerate (e.g. after a colour palette change or a diagram update):

```bash
python3 generate_svgs.py
```

No external dependencies are needed — the script uses only the Python standard library and writes SVG directly. The shared visual style (palette, typography, marker definitions) is defined once at the top of the script and reused by every diagram, so changes to the visual language touch one location.
