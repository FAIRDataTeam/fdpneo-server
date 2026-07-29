"""Tests for ``fdpneo_server.metadata.pid.verify`` — resolution verification."""

from __future__ import annotations

import httpx
import pytest
import respx

from fdpneo_server.metadata.pid.verify import verify_resolution

ID_BASE = "https://w3id.org/myfdp"
SERVING = "https://fdp.example.org"


@pytest.mark.unit
@respx.mock
async def test_pid_redirects_and_resolves() -> None:
    # w3id redirects the canonical IRI to the serving origin, which 200s.
    respx.get(f"{ID_BASE}/catalog/c1").mock(
        return_value=httpx.Response(302, headers={"Location": f"{SERVING}/catalog/c1"})
    )
    respx.get(f"{SERVING}/catalog/c1").mock(
        return_value=httpx.Response(200, text="<> a <Catalog> .")
    )
    async with httpx.AsyncClient() as client:
        report = await verify_resolution(
            identifier_base=ID_BASE,
            serving_base=SERVING,
            iris=[f"{ID_BASE}/catalog/c1"],
            http_client=client,
        )
    assert report.ok
    assert report.checks[0].redirected_to == f"{SERVING}/catalog/c1"
    assert report.checks[0].final_status == 200


@pytest.mark.unit
@respx.mock
async def test_fails_when_landing_off_serving_origin() -> None:
    respx.get(f"{ID_BASE}/catalog/c1").mock(
        return_value=httpx.Response(302, headers={"Location": "https://elsewhere.example/x"})
    )
    respx.get("https://elsewhere.example/x").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        report = await verify_resolution(
            identifier_base=ID_BASE,
            serving_base=SERVING,
            iris=[f"{ID_BASE}/catalog/c1"],
            http_client=client,
        )
    assert not report.ok
    assert "not the serving origin" in (report.checks[0].detail or "")


@pytest.mark.unit
@respx.mock
async def test_fails_on_non_2xx() -> None:
    respx.get(f"{ID_BASE}/missing").mock(
        return_value=httpx.Response(302, headers={"Location": f"{SERVING}/missing"})
    )
    respx.get(f"{SERVING}/missing").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        report = await verify_resolution(
            identifier_base=ID_BASE,
            serving_base=SERVING,
            iris=[f"{ID_BASE}/missing"],
            http_client=client,
        )
    assert not report.ok
    assert report.checks[0].final_status == 404


@pytest.mark.unit
async def test_rejects_iri_outside_identifier_base() -> None:
    async with httpx.AsyncClient() as client:
        report = await verify_resolution(
            identifier_base=ID_BASE,
            serving_base=SERVING,
            iris=["https://doi.org/10.1234/foo"],
            http_client=client,
        )
    assert not report.ok
    assert "not under the identifier base" in (report.checks[0].detail or "")


@pytest.mark.unit
@respx.mock
async def test_dev_no_redirect_still_resolves() -> None:
    # identifier_base == serving (local/dev): no redirect, content resolves.
    respx.get("http://localhost:8000/").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        report = await verify_resolution(
            identifier_base="http://localhost:8000",
            serving_base="http://localhost:8000",
            iris=["http://localhost:8000/"],
            http_client=client,
        )
    assert report.ok
