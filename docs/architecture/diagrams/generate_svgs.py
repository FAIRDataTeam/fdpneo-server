"""Generate standalone SVG diagrams for the FDP architecture document.

Each diagram is written to ``/home/claude/fdp-arch/diagrams/<name>.svg`` with
fully self-contained styling so the SVG renders identically whether viewed
standalone or after conversion to PNG for the .docx embedding step.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

OUT = Path(__file__).resolve().parent

# Shared CSS that every SVG embeds. Centralizes the visual language so all
# diagrams share the same palette, typography, and stroke conventions.
STYLE = dedent(
    """
    <style>
      .bg { fill: #ffffff; }
      text { font-family: 'Helvetica Neue', Arial, sans-serif; fill: #1f2937; }
      .th { font-size: 14px; font-weight: 600; fill: #111827; }
      .ts { font-size: 12px; fill: #374151; }
      .tn { font-size: 11px; fill: #6b7280; }
      .box { stroke-width: 1; }
      .gray { fill: #f3f4f6; stroke: #9ca3af; }
      .teal { fill: #ccfbf1; stroke: #0d9488; }
      .blue { fill: #dbeafe; stroke: #2563eb; }
      .green { fill: #dcfce7; stroke: #16a34a; }
      .amber { fill: #fef3c7; stroke: #d97706; }
      .red { fill: #fee2e2; stroke: #dc2626; }
      .purple { fill: #ede9fe; stroke: #7c3aed; }
      .coral { fill: #ffe4e6; stroke: #e11d48; }
      .arr { stroke: #4b5563; stroke-width: 1.5; fill: none; }
      .arr-dashed { stroke: #4b5563; stroke-width: 1.5; fill: none; stroke-dasharray: 4 3; }
      .lbl { font-size: 11px; fill: #4b5563; }
    </style>
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M2 1 L8 5 L2 9" fill="none" stroke="#4b5563" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </marker>
      <marker id="arrowstart" viewBox="0 0 10 10" refX="2" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M8 1 L2 5 L8 9" fill="none" stroke="#4b5563" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </marker>
    </defs>
    """
).strip()


def write_svg(name: str, width: int, height: int, body: str) -> Path:
    """Write a complete SVG file with the shared style block prepended."""
    path = OUT / f"{name}.svg"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  {STYLE}\n'
        f'  <rect class="bg" width="{width}" height="{height}"/>\n'
        f'  {body}\n'
        f'</svg>\n'
    )
    path.write_text(svg)
    return path


# Diagram 1: System context. Shows the four user types, the FDP, and the three
# external boundaries the system is intentionally agnostic about.
write_svg(
    "01_system_context",
    900,
    420,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 1: FDP system context</text>

    <g>
      <rect class="box gray" x="30" y="60" width="200" height="60" rx="8"/>
      <text class="th" x="130" y="86" text-anchor="middle">Data steward</text>
      <text class="ts" x="130" y="106" text-anchor="middle">Curates metadata, policies</text>
    </g>
    <g>
      <rect class="box gray" x="30" y="140" width="200" height="60" rx="8"/>
      <text class="th" x="130" y="166" text-anchor="middle">Data consumer</text>
      <text class="ts" x="130" y="186" text-anchor="middle">Browses, queries, downloads</text>
    </g>
    <g>
      <rect class="box gray" x="30" y="220" width="200" height="60" rx="8"/>
      <text class="th" x="130" y="246" text-anchor="middle">Anonymous user</text>
      <text class="ts" x="130" y="266" text-anchor="middle">Public read-only access</text>
    </g>
    <g>
      <rect class="box gray" x="30" y="300" width="200" height="60" rx="8"/>
      <text class="th" x="130" y="326" text-anchor="middle">Administrator</text>
      <text class="ts" x="130" y="346" text-anchor="middle">Deploys, configures</text>
    </g>

    <g>
      <rect class="box teal" x="340" y="170" width="220" height="110" rx="10"/>
      <text class="th" x="450" y="206" text-anchor="middle">FAIR Data Point</text>
      <text class="ts" x="450" y="228" text-anchor="middle">Metadata repository</text>
      <text class="ts" x="450" y="248" text-anchor="middle">DCAT, SHACL, ODRL, LDP</text>
    </g>

    <g>
      <rect class="box purple" x="670" y="80" width="200" height="60" rx="8"/>
      <text class="th" x="770" y="106" text-anchor="middle">Identity provider</text>
      <text class="ts" x="770" y="126" text-anchor="middle">External OIDC (any)</text>
    </g>
    <g>
      <rect class="box purple" x="670" y="190" width="200" height="60" rx="8"/>
      <text class="th" x="770" y="216" text-anchor="middle">Triple store</text>
      <text class="ts" x="770" y="236" text-anchor="middle">RDF storage backend</text>
    </g>
    <g>
      <rect class="box purple" x="670" y="300" width="200" height="60" rx="8"/>
      <text class="th" x="770" y="326" text-anchor="middle">API consumers</text>
      <text class="ts" x="770" y="346" text-anchor="middle">SPARQL, REST clients</text>
    </g>

    <line x1="230" y1="90"  x2="338" y2="190" class="arr" marker-end="url(#arrow)"/>
    <line x1="230" y1="170" x2="338" y2="215" class="arr" marker-end="url(#arrow)"/>
    <line x1="230" y1="250" x2="338" y2="240" class="arr" marker-end="url(#arrow)"/>
    <line x1="230" y1="330" x2="338" y2="265" class="arr" marker-end="url(#arrow)"/>

    <line x1="562" y1="190" x2="668" y2="112" class="arr" marker-end="url(#arrow)"/>
    <line x1="562" y1="225" x2="668" y2="220" class="arr" marker-start="url(#arrow)" marker-end="url(#arrow)"/>
    <line x1="668" y1="330" x2="562" y2="260" class="arr" marker-end="url(#arrow)"/>
    """,
)


# Diagram 2: Container diagram. Operator-managed units: SPA, API server, triple
# store, Postgres. External: OIDC provider.
write_svg(
    "02_containers",
    900,
    520,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 2: Container architecture</text>

    <g>
      <rect class="box teal" x="330" y="60" width="240" height="80" rx="10"/>
      <text class="th" x="450" y="92" text-anchor="middle">Web client</text>
      <text class="ts" x="450" y="114" text-anchor="middle">Vue 3 + TypeScript SPA</text>
      <text class="tn" x="450" y="130" text-anchor="middle">Static assets, served by CDN or nginx</text>
    </g>

    <g>
      <rect class="box teal" x="180" y="200" width="540" height="120" rx="10"/>
      <text class="th" x="450" y="234" text-anchor="middle">API server</text>
      <text class="ts" x="450" y="256" text-anchor="middle">Python 3.12 + FastAPI, stateless, horizontally scalable</text>
      <text class="ts" x="450" y="276" text-anchor="middle">REST (LDP), SPARQL endpoint, JWT-authenticated</text>
      <text class="tn" x="450" y="298" text-anchor="middle">Containerized; one or more instances behind a load balancer</text>
    </g>

    <g>
      <rect class="box gray" x="760" y="220" width="130" height="80" rx="8"/>
      <text class="th" x="825" y="248" text-anchor="middle">Identity</text>
      <text class="ts" x="825" y="268" text-anchor="middle">External OIDC</text>
      <text class="tn" x="825" y="286" text-anchor="middle">Keycloak, etc.</text>
    </g>

    <g>
      <rect class="box coral" x="80" y="390" width="320" height="90" rx="8"/>
      <text class="th" x="240" y="422" text-anchor="middle">Triple store</text>
      <text class="ts" x="240" y="444" text-anchor="middle">Pluggable SPARQL 1.1 Protocol backend</text>
      <text class="tn" x="240" y="464" text-anchor="middle">GraphDB (recommended), Fuseki, Oxigraph, ...</text>
    </g>

    <g>
      <rect class="box coral" x="500" y="390" width="320" height="90" rx="8"/>
      <text class="th" x="660" y="422" text-anchor="middle">Operational store</text>
      <text class="ts" x="660" y="444" text-anchor="middle">PostgreSQL 16+</text>
      <text class="tn" x="660" y="464" text-anchor="middle">Metrics, audit hashes, auth cache, jobs</text>
    </g>

    <line x1="450" y1="140" x2="450" y2="198" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="460" y="172">HTTPS</text>

    <line x1="720" y1="260" x2="758" y2="260" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="720" y="252">OIDC</text>

    <line x1="320" y1="320" x2="270" y2="388" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="220" y="362">SPARQL 1.1 Protocol</text>

    <line x1="580" y1="320" x2="630" y2="388" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="600" y="362">SQL</text>
    """,
)


# Diagram 3: Server components. Two HTTP entry points, auth middleware,
# four bounded contexts, storage adapter, external stores.
write_svg(
    "03_server_components",
    900,
    640,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 3: Server component architecture</text>

    <g>
      <rect class="box gray" x="170" y="60" width="250" height="60" rx="8"/>
      <text class="th" x="295" y="86" text-anchor="middle">REST API (LDP)</text>
      <text class="ts" x="295" y="106" text-anchor="middle">Containers, records, schemas, policies</text>
    </g>
    <g>
      <rect class="box gray" x="480" y="60" width="250" height="60" rx="8"/>
      <text class="th" x="605" y="86" text-anchor="middle">SPARQL endpoint</text>
      <text class="ts" x="605" y="106" text-anchor="middle">Read and update queries</text>
    </g>

    <g>
      <rect class="box gray" x="60" y="150" width="780" height="50" rx="8"/>
      <text class="th" x="450" y="172" text-anchor="middle">Auth middleware</text>
      <text class="ts" x="450" y="190" text-anchor="middle">OIDC bearer, JWT validation, role resolution, request context</text>
    </g>

    <g>
      <rect class="box teal" x="60" y="230" width="180" height="100" rx="8"/>
      <text class="th" x="150" y="260" text-anchor="middle">Metadata provider</text>
      <text class="ts" x="150" y="280" text-anchor="middle">Records, schemas</text>
      <text class="ts" x="150" y="298" text-anchor="middle">LDP server</text>
      <text class="ts" x="150" y="316" text-anchor="middle">SHACL validation</text>
    </g>
    <g>
      <rect class="box teal" x="260" y="230" width="180" height="100" rx="8"/>
      <text class="th" x="350" y="260" text-anchor="middle">Security enforcer</text>
      <text class="ts" x="350" y="280" text-anchor="middle">ODRL evaluator (PDP)</text>
      <text class="ts" x="350" y="298" text-anchor="middle">Offer / Agreement</text>
      <text class="ts" x="350" y="316" text-anchor="middle">Authorization cache</text>
    </g>
    <g>
      <rect class="box teal" x="460" y="230" width="180" height="100" rx="8"/>
      <text class="th" x="550" y="260" text-anchor="middle">Metrics gatherer</text>
      <text class="ts" x="550" y="280" text-anchor="middle">Anonymized events</text>
      <text class="ts" x="550" y="298" text-anchor="middle">Aggregation, dashboard</text>
      <text class="ts" x="550" y="316" text-anchor="middle">GDPR-safe</text>
    </g>
    <g>
      <rect class="box teal" x="660" y="230" width="180" height="100" rx="8"/>
      <text class="th" x="750" y="260" text-anchor="middle">Data provider</text>
      <text class="ts" x="750" y="280" text-anchor="middle">Open distributions</text>
      <text class="ts" x="750" y="298" text-anchor="middle">Download, SPARQL</text>
      <text class="ts" x="750" y="316" text-anchor="middle">Per-distribution scope</text>
    </g>

    <g>
      <rect class="box amber" x="60" y="370" width="780" height="60" rx="8"/>
      <text class="th" x="450" y="396" text-anchor="middle">Shared kernel</text>
      <text class="ts" x="450" y="416" text-anchor="middle">RDF utilities, namespaces, event bus, identity context, error types</text>
    </g>

    <g>
      <rect class="box gray" x="60" y="470" width="780" height="60" rx="8"/>
      <text class="th" x="450" y="496" text-anchor="middle">Storage adapters</text>
      <text class="ts" x="450" y="514" text-anchor="middle">Triple store port (SPARQL 1.1 Protocol)   ·   Postgres repository</text>
    </g>

    <g>
      <rect class="box purple" x="160" y="570" width="240" height="50" rx="8"/>
      <text class="th" x="280" y="600" text-anchor="middle">Triple store</text>
    </g>
    <g>
      <rect class="box purple" x="500" y="570" width="240" height="50" rx="8"/>
      <text class="th" x="620" y="600" text-anchor="middle">PostgreSQL</text>
    </g>

    <line x1="295" y1="120" x2="295" y2="148" class="arr" marker-end="url(#arrow)"/>
    <line x1="605" y1="120" x2="605" y2="148" class="arr" marker-end="url(#arrow)"/>

    <line x1="150" y1="200" x2="150" y2="228" class="arr" marker-end="url(#arrow)"/>
    <line x1="350" y1="200" x2="350" y2="228" class="arr" marker-end="url(#arrow)"/>
    <line x1="550" y1="200" x2="550" y2="228" class="arr" marker-end="url(#arrow)"/>
    <line x1="750" y1="200" x2="750" y2="228" class="arr" marker-end="url(#arrow)"/>

    <line x1="150" y1="330" x2="150" y2="368" class="arr" marker-end="url(#arrow)"/>
    <line x1="350" y1="330" x2="350" y2="368" class="arr" marker-end="url(#arrow)"/>
    <line x1="550" y1="330" x2="550" y2="368" class="arr" marker-end="url(#arrow)"/>
    <line x1="750" y1="330" x2="750" y2="368" class="arr" marker-end="url(#arrow)"/>

    <line x1="450" y1="430" x2="450" y2="468" class="arr" marker-end="url(#arrow)"/>

    <line x1="280" y1="530" x2="280" y2="568" class="arr" marker-end="url(#arrow)"/>
    <line x1="620" y1="530" x2="620" y2="568" class="arr" marker-end="url(#arrow)"/>
    """,
)


# Diagram 4: Data model. Named-graph layout: one graph per record, sibling
# meta-metadata graph, sibling audit graph for agreements.
write_svg(
    "04_data_model",
    900,
    540,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 4: Named-graph data model per record</text>

    <g>
      <rect class="box blue" x="40" y="80" width="820" height="380" rx="14"/>
      <text class="th" x="60" y="108">Triple store</text>
      <text class="tn" x="60" y="126">Backed by SPARQL 1.1 Protocol; FDP is agnostic to vendor</text>
    </g>

    <g>
      <rect class="box teal" x="80" y="160" width="260" height="240" rx="10"/>
      <text class="th" x="210" y="190" text-anchor="middle">Record graph</text>
      <text class="ts" x="210" y="212" text-anchor="middle">&lt;https://fdp.example/r/123&gt;</text>
      <text class="tn" x="100" y="244">dcat:Dataset / FDP type</text>
      <text class="tn" x="100" y="264">dct:title, dct:description</text>
      <text class="tn" x="100" y="284">dcat:keyword, dcat:theme</text>
      <text class="tn" x="100" y="304">dct:rights → Offer (versioned)</text>
      <text class="tn" x="100" y="324">SHACL-validated on write</text>
      <text class="tn" x="100" y="364">Subject to ODRL policy</text>
      <text class="tn" x="100" y="384">Visible via SPARQL when authorized</text>
    </g>

    <g>
      <rect class="box amber" x="370" y="160" width="220" height="240" rx="10"/>
      <text class="th" x="480" y="190" text-anchor="middle">Meta-metadata graph</text>
      <text class="ts" x="480" y="212" text-anchor="middle">&lt;.../r/123/meta&gt;</text>
      <text class="tn" x="390" y="244">dct:creator (steward URI)</text>
      <text class="tn" x="390" y="264">dct:created, dct:modified</text>
      <text class="tn" x="390" y="284">owl:versionInfo</text>
      <text class="tn" x="390" y="304">prov:wasGeneratedBy</text>
      <text class="tn" x="390" y="324">Custom fields per profile</text>
      <text class="tn" x="390" y="364">SHACL-validated against</text>
      <text class="tn" x="390" y="384">meta-metadata schema</text>
    </g>

    <g>
      <rect class="box red" x="620" y="160" width="220" height="240" rx="10"/>
      <text class="th" x="730" y="190" text-anchor="middle">Audit graph</text>
      <text class="ts" x="730" y="212" text-anchor="middle">&lt;.../r/123/audit&gt;</text>
      <text class="tn" x="640" y="244">odrl:Agreement instances</text>
      <text class="tn" x="640" y="264">odrl:assigner, odrl:assignee</text>
      <text class="tn" x="640" y="284">prov:wasDerivedFrom Offer</text>
      <text class="tn" x="640" y="304">Grant timestamp, action</text>
      <text class="tn" x="640" y="324">Append-only</text>
      <text class="tn" x="640" y="364">Steward visible</text>
      <text class="tn" x="640" y="384">Excluded from public SPARQL</text>
    </g>

    <text class="ts" x="450" y="490" text-anchor="middle">Per-record authorization is set membership over named graph URIs.</text>
    <text class="ts" x="450" y="510" text-anchor="middle">Different access policies can apply to the record, its meta-metadata, and its audit trail.</text>
    """,
)


# Diagram 5: Metrics privacy boundary. Visualizes what is observed and dropped
# versus what is kept as anonymous aggregate.
write_svg(
    "05_metrics_privacy",
    900,
    320,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 5: Metrics anonymization boundary</text>

    <g>
      <rect class="box amber" x="40" y="70" width="380" height="220" rx="10"/>
      <text class="th" x="60" y="100">Observed, then discarded</text>
      <text class="ts" x="60" y="136">IP address</text>
      <text class="ts" x="60" y="160">User agent string</text>
      <text class="ts" x="60" y="184">Authenticated identity</text>
      <text class="ts" x="60" y="208">Query and search text</text>
      <text class="ts" x="60" y="232">Referrer URL</text>
      <text class="tn" x="60" y="268">Stripped at ingress, never persisted</text>
    </g>

    <g>
      <rect class="box green" x="480" y="70" width="380" height="220" rx="10"/>
      <text class="th" x="500" y="100">Stored as aggregate</text>
      <text class="ts" x="500" y="136">Country, region, city (from GeoLite2)</text>
      <text class="ts" x="500" y="160">Daily-rotated visitor hash</text>
      <text class="ts" x="500" y="184">Event type and resource id</text>
      <text class="ts" x="500" y="208">Hourly or daily time bucket</text>
      <text class="ts" x="500" y="232">Counts only</text>
      <text class="tn" x="500" y="268">Persisted to Postgres aggregates</text>
    </g>

    <line x1="424" y1="180" x2="476" y2="180" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="450" y="172" text-anchor="middle">anonymize</text>
    """,
)


# Diagram 6: ODRL offer / agreement lifecycle. Record holds Offer; PDP
# materializes Agreement on permit.
write_svg(
    "06_odrl_offer_agreement",
    900,
    420,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 6: ODRL Offer and Agreement lifecycle</text>

    <g>
      <rect class="box gray" x="40" y="170" width="160" height="80" rx="8"/>
      <text class="th" x="120" y="200" text-anchor="middle">Record</text>
      <text class="ts" x="120" y="222" text-anchor="middle">Metadata resource</text>
      <text class="tn" x="120" y="240" text-anchor="middle">in record graph</text>
    </g>
    <text class="lbl" x="240" y="200" text-anchor="middle">dct:rights</text>
    <line x1="200" y1="210" x2="278" y2="210" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box blue" x="280" y="150" width="240" height="120" rx="10"/>
      <text class="th" x="400" y="180" text-anchor="middle">Offer</text>
      <text class="ts" x="400" y="202" text-anchor="middle">odrl:Offer</text>
      <text class="ts" x="400" y="222" text-anchor="middle">Permissions and Prohibitions</text>
      <text class="ts" x="400" y="242" text-anchor="middle">Versioned, immutable</text>
      <text class="tn" x="400" y="260" text-anchor="middle">stored in offers graph</text>
    </g>

    <text class="lbl" x="570" y="200" text-anchor="middle">PERMIT</text>
    <text class="lbl" x="570" y="218" text-anchor="middle">materializes</text>
    <line x1="520" y1="210" x2="598" y2="210" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box green" x="600" y="80" width="280" height="280" rx="10"/>
      <text class="th" x="740" y="110" text-anchor="middle">Agreement</text>
      <text class="ts" x="740" y="132" text-anchor="middle">odrl:Agreement</text>
      <text class="tn" x="620" y="170">odrl:assigner = rights holder</text>
      <text class="tn" x="620" y="190">odrl:assignee = user URI</text>
      <text class="tn" x="620" y="210">prov:wasDerivedFrom Offer vN</text>
      <text class="tn" x="620" y="230">dct:issued = grant timestamp</text>
      <text class="tn" x="620" y="250">odrl:action = read | modify | ...</text>
      <text class="tn" x="620" y="290">Stored in record audit graph</text>
      <text class="tn" x="620" y="310">Append-only, retention per policy</text>
      <text class="tn" x="620" y="340">Identifies the assignee by design</text>
    </g>
    """,
)


# Diagram 7: SPARQL access flow. Vertical pipeline with five steps.
write_svg(
    "07_sparql_access_flow",
    900,
    580,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 7: SPARQL access-control flow</text>

    <g>
      <rect class="box teal" x="220" y="70" width="460" height="60" rx="8"/>
      <text class="th" x="450" y="96" text-anchor="middle">1. Authenticate request</text>
      <text class="ts" x="450" y="116" text-anchor="middle">Validate JWT, build user context (subject, roles)</text>
    </g>
    <line x1="450" y1="130" x2="450" y2="158" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box teal" x="220" y="160" width="460" height="60" rx="8"/>
      <text class="th" x="450" y="186" text-anchor="middle">2. Parse and classify query</text>
      <text class="ts" x="450" y="206" text-anchor="middle">rdflib algebra; read vs update; referenced graphs</text>
    </g>
    <line x1="450" y1="220" x2="450" y2="248" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box teal" x="220" y="250" width="460" height="60" rx="8"/>
      <text class="th" x="450" y="276" text-anchor="middle">3. Resolve authorized graphs</text>
      <text class="ts" x="450" y="296" text-anchor="middle">PDP lookup against materialized authorization index</text>
    </g>
    <line x1="450" y1="310" x2="450" y2="338" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box teal" x="220" y="340" width="460" height="60" rx="8"/>
      <text class="th" x="450" y="366" text-anchor="middle">4. Rewrite or reject</text>
      <text class="ts" x="450" y="386" text-anchor="middle">Inject FROM NAMED; validate explicit GRAPH references</text>
    </g>
    <line x1="450" y1="400" x2="450" y2="428" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box teal" x="220" y="430" width="460" height="60" rx="8"/>
      <text class="th" x="450" y="456" text-anchor="middle">5. Execute via adapter</text>
      <text class="ts" x="450" y="476" text-anchor="middle">Pass through to triple store, stream results back</text>
    </g>

    <text class="tn" x="80" y="100">Anonymous</text>
    <text class="tn" x="80" y="116">→ read only</text>
    <text class="tn" x="80" y="190">SERVICE</text>
    <text class="tn" x="80" y="206">→ reject (no federation)</text>
    <text class="tn" x="80" y="280">Auth cache</text>
    <text class="tn" x="80" y="296">→ Postgres</text>
    <text class="tn" x="80" y="370">Explicit unauth graph</text>
    <text class="tn" x="80" y="386">→ 403</text>
    <text class="tn" x="80" y="460">Adapter</text>
    <text class="tn" x="80" y="476">→ Fuseki/GraphDB/...</text>
    """,
)


# Diagram 8: Sequence diagram for an LDP PATCH (partial update) — the key
# new capability per the LDP discussion.
write_svg(
    "08_seq_ldp_patch",
    900,
    560,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 8: LDP PATCH sequence (partial record update)</text>

    <text class="th" x="100" y="70" text-anchor="middle">Client</text>
    <text class="th" x="280" y="70" text-anchor="middle">API</text>
    <text class="th" x="430" y="70" text-anchor="middle">PDP</text>
    <text class="th" x="580" y="70" text-anchor="middle">Metadata</text>
    <text class="th" x="730" y="70" text-anchor="middle">Triple store</text>

    <line x1="100" y1="80" x2="100" y2="540" class="arr-dashed"/>
    <line x1="280" y1="80" x2="280" y2="540" class="arr-dashed"/>
    <line x1="430" y1="80" x2="430" y2="540" class="arr-dashed"/>
    <line x1="580" y1="80" x2="580" y2="540" class="arr-dashed"/>
    <line x1="730" y1="80" x2="730" y2="540" class="arr-dashed"/>

    <line x1="100" y1="110" x2="278" y2="110" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="190" y="104" text-anchor="middle">PATCH /r/123 (sparql-update)</text>

    <line x1="280" y1="150" x2="428" y2="150" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="354" y="144" text-anchor="middle">authorize(user, modify, /r/123)</text>

    <line x1="430" y1="190" x2="282" y2="190" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="356" y="184" text-anchor="middle">PERMIT (from auth cache)</text>

    <line x1="280" y1="230" x2="578" y2="230" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="430" y="224" text-anchor="middle">apply_patch(graph, sparql_update)</text>

    <line x1="580" y1="270" x2="728" y2="270" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="654" y="264" text-anchor="middle">CONSTRUCT current state</text>

    <line x1="730" y1="310" x2="582" y2="310" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="656" y="304" text-anchor="middle">triples</text>

    <text class="lbl" x="500" y="340" text-anchor="middle">simulate update, SHACL-validate</text>

    <line x1="580" y1="370" x2="728" y2="370" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="654" y="364" text-anchor="middle">UPDATE graph + meta-metadata</text>

    <line x1="730" y1="410" x2="582" y2="410" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="656" y="404" text-anchor="middle">OK, new ETag</text>

    <line x1="580" y1="450" x2="282" y2="450" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="430" y="444" text-anchor="middle">204 No Content, ETag</text>

    <line x1="280" y1="490" x2="102" y2="490" class="arr" marker-end="url(#arrow)"/>
    <text class="lbl" x="190" y="484" text-anchor="middle">204 No Content</text>

    <text class="tn" x="450" y="525" text-anchor="middle">If SHACL fails, no triples are written; client gets 422 with shape violation report.</text>
    """,
)


# Diagram 9: Deployment profile bootstrap — packaged bundle becomes an
# initialized FDP.
write_svg(
    "09_deployment_profile",
    900,
    420,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 9: Deployment profile bootstrap</text>

    <g>
      <rect class="box purple" x="40" y="80" width="340" height="300" rx="12"/>
      <text class="th" x="60" y="110">Deployment profile</text>
      <text class="tn" x="60" y="128">Versioned, validated bundle</text>
      <text class="ts" x="60" y="172">profile.yaml (manifest)</text>
      <text class="ts" x="60" y="204">SHACL schemas (.ttl)</text>
      <text class="ts" x="60" y="236">ODRL Offer templates (.ttl)</text>
      <text class="ts" x="60" y="268">LDP container hierarchy</text>
      <text class="ts" x="60" y="300">Seed metadata records</text>
      <text class="tn" x="60" y="346">Distributable: git, OCI artifact, tarball</text>
    </g>

    <text class="lbl" x="450" y="220" text-anchor="middle">fdp profile apply</text>
    <text class="lbl" x="450" y="238" text-anchor="middle">at bootstrap</text>
    <line x1="384" y1="230" x2="516" y2="230" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box teal" x="520" y="80" width="340" height="300" rx="12"/>
      <text class="th" x="540" y="110">Initialized FDP</text>
      <text class="tn" x="540" y="128">Profile applied, state committed</text>
      <text class="ts" x="540" y="172">Profile name + version pinned</text>
      <text class="ts" x="540" y="204">Schemas registered, validated</text>
      <text class="ts" x="540" y="236">Offers available as defaults</text>
      <text class="ts" x="540" y="268">Containers ready for POST</text>
      <text class="ts" x="540" y="300">Seed records committed</text>
      <text class="tn" x="540" y="346">Refuses re-apply unless forced</text>
    </g>
    """,
)


# Diagram 10: Authorization data flow — how the materialized index is
# populated and invalidated.
write_svg(
    "10_authorization_index",
    900,
    480,
    """
    <text class="th" x="450" y="30" text-anchor="middle">Figure 10: Authorization index lifecycle</text>

    <g>
      <rect class="box blue" x="40" y="80" width="220" height="80" rx="8"/>
      <text class="th" x="150" y="110" text-anchor="middle">Policy change</text>
      <text class="ts" x="150" y="130" text-anchor="middle">Steward edits Offer</text>
      <text class="ts" x="150" y="148" text-anchor="middle">or record dct:rights</text>
    </g>
    <g>
      <rect class="box blue" x="340" y="80" width="220" height="80" rx="8"/>
      <text class="th" x="450" y="110" text-anchor="middle">User session change</text>
      <text class="ts" x="450" y="130" text-anchor="middle">New role set</text>
      <text class="ts" x="450" y="148" text-anchor="middle">from IdP claims</text>
    </g>
    <g>
      <rect class="box blue" x="640" y="80" width="220" height="80" rx="8"/>
      <text class="th" x="750" y="110" text-anchor="middle">First access</text>
      <text class="ts" x="750" y="130" text-anchor="middle">Subject not in cache</text>
      <text class="ts" x="750" y="148" text-anchor="middle">Lazy population</text>
    </g>

    <line x1="150" y1="160" x2="380" y2="218" class="arr" marker-end="url(#arrow)"/>
    <line x1="450" y1="160" x2="450" y2="218" class="arr" marker-end="url(#arrow)"/>
    <line x1="750" y1="160" x2="520" y2="218" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box amber" x="220" y="220" width="460" height="90" rx="8"/>
      <text class="th" x="450" y="252" text-anchor="middle">PDP recompute</text>
      <text class="ts" x="450" y="272" text-anchor="middle">Resolve effective policy chain (catalog → repository → default)</text>
      <text class="ts" x="450" y="290" text-anchor="middle">Evaluate constraints; write decision rows</text>
    </g>

    <line x1="450" y1="310" x2="450" y2="338" class="arr" marker-end="url(#arrow)"/>

    <g>
      <rect class="box coral" x="180" y="340" width="540" height="100" rx="8"/>
      <text class="th" x="450" y="374" text-anchor="middle">Materialized authorization index (Postgres)</text>
      <text class="ts" x="450" y="396" text-anchor="middle">(subject_key, action, graph_uri, decision, policy_version)</text>
      <text class="tn" x="450" y="418" text-anchor="middle">Set-membership lookups for SPARQL graph filtering and per-record auth</text>
    </g>
    """,
)


print("Generated SVG files:")
for p in sorted(OUT.glob("*.svg")):
    print(f"  {p.name}: {p.stat().st_size} bytes")
