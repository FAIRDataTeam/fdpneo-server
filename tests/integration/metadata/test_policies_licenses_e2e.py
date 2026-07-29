"""End-to-end policy + license management against Oxigraph + Postgres (ADR-0012).

Drives the whole HTTP stack via :func:`fdpneo_server.main.create_app` — real triple store,
Postgres-backed PDP/authz cache, SHACL validator, LDP + the policy/license admin
routers + the publication-state router — with the request context injected
through a ``current_context`` override (no OIDC/JWT needed). The profile is
auto-applied on startup from a tiny bundle whose system-default offer permits
anonymous read and steward modify.

Proves the Phase 14 round-trip:

1. **Policies** — author (`PUT /policies/{id}`), validate (`POST .../validate`,
   plus an out-of-profile body rejected 422), publish (`POST .../state`),
   discover (anonymous `GET /policies` excludes drafts), reference a record via
   ``dct:rights``, and **enforce**: a record under a steward-only policy is
   invisible to anonymous but readable by a steward, while a record under an
   open policy is anonymously readable — isolating the policy as the cause.
   Deleting a still-referenced policy is refused (409).
2. **Licenses** — author / validate (SHACL) / publish / discover (seeded default
   set is present) / reference via ``dct:license`` / delete-guarded (409).

Requires Docker (testcontainers). Marked ``integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from fdpneo_server.shared.context import RequestContext
from tests.integration.conftest import GraphDBStore

BASE_URL = "http://testserver"

pytestmark = pytest.mark.integration


# --- bundle ---------------------------------------------------------------

PROFILE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: policy-license-e2e
  version: 0.1.0
schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
    children:
      - relationUri: dcat:catalog
        target: catalog
        title: Catalogs
  - urlPrefix: catalog
    name: Catalog
    schema: dcat:Catalog
"""

CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""

# System default: anonymous read, steward modify. Lets the admin context
# (roles {admin, steward}) create + publish records and policies, while keeping
# anonymous writes out.
SYSTEM_DEFAULT_OFFER_TTL = """\
@prefix odrl:    <http://www.w3.org/ns/odrl/2/> .
@prefix fdp-pol: <https://specs.fairdatapoint.org/odrl-profile#> .
<http://example.org/offers/system-default>
    a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] ;
    odrl:permission [
        a odrl:Permission ; odrl:action odrl:modify ;
        odrl:constraint [
            odrl:leftOperand fdp-pol:role ; odrl:operator odrl:eq ;
            odrl:rightOperand "steward"
        ]
    ] .
"""


def _authored_offer(*, read_steward_only: bool) -> str:
    """An Offer body (relative ``<>``) permitting steward modify and read.

    When ``read_steward_only`` the read permission is role-constrained, so
    anonymous callers are denied read; otherwise read is unconstrained (open).
    """
    read_constraint = (
        " ;\n        odrl:constraint [ odrl:leftOperand fdp-pol:role ;"
        ' odrl:operator odrl:eq ; odrl:rightOperand "steward" ]'
        if read_steward_only
        else ""
    )
    return f"""\
@prefix odrl:    <http://www.w3.org/ns/odrl/2/> .
@prefix fdp-pol: <https://specs.fairdatapoint.org/odrl-profile#> .
<> a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read{read_constraint} ] ;
    odrl:permission [
        a odrl:Permission ; odrl:action odrl:modify ;
        odrl:constraint [
            odrl:leftOperand fdp-pol:role ; odrl:operator odrl:eq ;
            odrl:rightOperand "steward"
        ]
    ] .
"""


# odrl:use is outside the FDP profile action vocabulary → PUT must reject 422.
OUT_OF_PROFILE_OFFER = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
<> a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:use ] .
"""

LICENSE_TTL = """\
@prefix dct: <http://purl.org/dc/terms/> .
<> a dct:LicenseDocument ;
    dct:title "Custom in-house license" ;
    dct:source <http://example.org/licenses/custom> .
"""

# Title on a different subject than the stable IRI → fails the license shape.
BAD_LICENSE_TTL = """\
@prefix dct: <http://purl.org/dc/terms/> .
<urn:elsewhere> dct:title "not the stable subject" .
"""


def _catalog_ttl(*, title: str, rights: str | None = None, license_iri: str | None = None) -> str:
    extra = ""
    if rights is not None:
        extra += f" ;\n    dct:rights <{rights}>"
    if license_iri is not None:
        extra += f" ;\n    dct:license <{license_iri}>"
    return (
        "@prefix dcat: <http://www.w3.org/ns/dcat#> .\n"
        "@prefix dct:  <http://purl.org/dc/terms/> .\n"
        f'<> a dcat:Catalog ;\n    dct:title "{title}"{extra} .\n'
    )


# --- containers + env ------------------------------------------------------


def _async_dsn(container: PostgresContainer) -> str:
    raw = container.get_connection_url()
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "profile.yaml").write_text(PROFILE_MANIFEST, encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "catalog.ttl").write_text(CATALOG_SHAPE_TTL, encoding="utf-8")
    (root / "offers").mkdir()
    (root / "offers" / "system-default.ttl").write_text(SYSTEM_DEFAULT_OFFER_TTL, encoding="utf-8")
    return root


@pytest.fixture
def app_env(
    postgres_container: PostgresContainer,
    graphdb_store: GraphDBStore,
    bundle: Path,
) -> Iterator[None]:
    from fdpneo_server.config import get_settings

    env = {
        "POSTGRES_DSN": _async_dsn(postgres_container),
        "FDP_TRIPLESTORE_QUERY_ENDPOINT": graphdb_store.query,
        "FDP_TRIPLESTORE_UPDATE_ENDPOINT": graphdb_store.update,
        "FDP_TRIPLESTORE_GRAPH_STORE_ENDPOINT": graphdb_store.graph_store,
        "FDP_OIDC_ISSUER": "http://idp.local/realms/fdp",
        "FDP_OIDC_AUDIENCE": "fdp",
        "BASE_URL": BASE_URL,
        "FDP_PROFILE_AUTO_APPLY": "true",
        "FDP_PROFILE_PATH": str(bundle),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    get_settings.cache_clear()

    config = Config(str(files("fdpneo_server") / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        get_settings.cache_clear()


# --- client helper ---------------------------------------------------------


def _admin() -> RequestContext:
    return RequestContext(
        subject="http://idp.local/realms/fdp#admin",
        roles=frozenset({"admin", "steward"}),
        trace_id="it",
    )


class _Client:
    def __init__(self, client: TestClient, holder: dict[str, RequestContext]) -> None:
        self.http = client
        self._holder = holder

    def as_admin(self) -> TestClient:
        self._holder["ctx"] = _admin()
        return self.http

    def as_anonymous(self) -> TestClient:
        self._holder["ctx"] = RequestContext.anonymous(trace_id="it")
        return self.http


def _make_client() -> _Client:
    from fdpneo_server.identity.deps import current_context
    from fdpneo_server.main import create_app

    app = create_app()
    holder: dict[str, RequestContext] = {"ctx": RequestContext.anonymous(trace_id="it")}
    app.dependency_overrides[current_context] = lambda: holder["ctx"]
    return _Client(TestClient(app, base_url=BASE_URL), holder)


_TTL = {"Content-Type": "text/turtle"}


def _publish(c: _Client, path: str) -> None:
    # State transitions live under the reserved API prefix; ``path`` may be a
    # bare LDP record path (``/catalog/x``) or an already-prefixed managed
    # resource (``/fdp-api/policies/x``).
    state_path = path if path.startswith("/fdp-api/") else f"/fdp-api{path}"
    resp = c.as_admin().post(state_path + "/state", json={"to": "PUBLISHED"})
    assert resp.status_code == 200, resp.text


def _create_published_catalog(c: _Client, slug: str, body: str) -> str:
    created = c.as_admin().post("/catalog", content=body, headers={**_TTL, "Slug": slug})
    assert created.status_code == 201, created.text
    location = created.headers["Location"]
    # New records default to DRAFT; publish so the state gate does not mask the
    # ODRL decision we want to observe.
    path = location[len(BASE_URL) :] if location.startswith(BASE_URL) else "/catalog/" + slug
    _publish(c, path)
    return path


# --- tests -----------------------------------------------------------------


def test_policy_round_trip_author_validate_publish_enforce(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # --- author ---------------------------------------------------------
        assert (
            c.as_admin()
            .put(
                "/fdp-api/policies/restricted",
                content=_authored_offer(read_steward_only=True),
                headers=_TTL,
            )
            .status_code
            == 200
        )
        assert (
            c.as_admin()
            .put(
                "/fdp-api/policies/open",
                content=_authored_offer(read_steward_only=False),
                headers=_TTL,
            )
            .status_code
            == 200
        )
        # A draft policy: authored but never published.
        assert (
            c.as_admin()
            .put(
                "/fdp-api/policies/wip",
                content=_authored_offer(read_steward_only=True),
                headers=_TTL,
            )
            .status_code
            == 200
        )

        # --- validate -------------------------------------------------------
        ok = c.as_admin().post(
            "/fdp-api/policies/restricted/validate",
            content=_authored_offer(read_steward_only=True),
            headers=_TTL,
        )
        assert ok.status_code == 200 and ok.json()["conforms"] is True

        # Out-of-profile bodies are rejected on write (422) and flagged by the dry run.
        rejected = c.as_admin().put(
            "/fdp-api/policies/bad", content=OUT_OF_PROFILE_OFFER, headers=_TTL
        )
        assert rejected.status_code == 422, rejected.text
        dry = c.as_admin().post(
            "/fdp-api/policies/restricted/validate", content=OUT_OF_PROFILE_OFFER, headers=_TTL
        )
        assert dry.json()["conforms"] is False

        # --- publish + discover --------------------------------------------
        _publish(c, "/fdp-api/policies/restricted")
        _publish(c, "/fdp-api/policies/open")

        anon_catalog = {
            p["id"] for p in c.as_anonymous().get("/fdp-api/policies").json()["policies"]
        }
        assert {"restricted", "open", "system-default"} <= anon_catalog
        assert "wip" not in anon_catalog, "draft policy leaked into anonymous discovery"
        admin_catalog = {p["id"] for p in c.as_admin().get("/fdp-api/policies").json()["policies"]}
        assert "wip" in admin_catalog, "admin should see draft policies to manage them"

        # --- reference + enforce -------------------------------------------
        restricted_path = _create_published_catalog(
            c,
            "restricted-cat",
            _catalog_ttl(title="Restricted", rights=f"{BASE_URL}/fdp-api/policies/restricted"),
        )
        open_path = _create_published_catalog(
            c, "open-cat", _catalog_ttl(title="Open", rights=f"{BASE_URL}/fdp-api/policies/open")
        )

        # The authored+published policy governs visibility:
        assert c.as_anonymous().get(open_path, headers={"Accept": "text/turtle"}).status_code == 200
        denied = c.as_anonymous().get(restricted_path, headers={"Accept": "text/turtle"})
        assert denied.status_code in (401, 403, 404), denied.text
        assert (
            c.as_admin().get(restricted_path, headers={"Accept": "text/turtle"}).status_code == 200
        )

        # --- delete guard ---------------------------------------------------
        assert c.as_admin().delete("/fdp-api/policies/restricted").status_code == 409
        # The unreferenced draft can be deleted.
        assert c.as_admin().delete("/fdp-api/policies/wip").status_code == 204


def test_license_round_trip_and_delete_guard(app_env: None) -> None:
    c = _make_client()
    with c.http:
        # author + validate (SHACL) -----------------------------------------
        assert (
            c.as_admin()
            .put("/fdp-api/licenses/custom", content=LICENSE_TTL, headers=_TTL)
            .status_code
            == 200
        )
        ok = c.as_admin().post(
            "/fdp-api/licenses/custom/validate", content=LICENSE_TTL, headers=_TTL
        )
        assert ok.status_code == 200 and ok.json()["conforms"] is True
        assert (
            c.as_admin()
            .put("/fdp-api/licenses/bad", content=BAD_LICENSE_TTL, headers=_TTL)
            .status_code
            == 422
        )

        # publish + discover (the seeded default set is present) -------------
        _publish(c, "/fdp-api/licenses/custom")
        anon = {lic["id"] for lic in c.as_anonymous().get("/fdp-api/licenses").json()["licenses"]}
        assert "custom" in anon
        assert {"cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0"} <= anon, "seeded default licenses missing"

        # reference via dct:license + delete guard --------------------------
        _create_published_catalog(
            c,
            "licensed-cat",
            _catalog_ttl(title="Licensed", license_iri=f"{BASE_URL}/fdp-api/licenses/custom"),
        )
        assert c.as_admin().delete("/fdp-api/licenses/custom").status_code == 409
        # A seeded-but-unreferenced license deletes cleanly.
        assert c.as_admin().delete("/fdp-api/licenses/cc-by-sa-4.0").status_code == 204
