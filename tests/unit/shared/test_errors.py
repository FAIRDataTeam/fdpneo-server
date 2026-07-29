"""Tests for ``fdpneo_server.shared.errors``."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.shared.errors import (
    Conflict,
    FDPError,
    Forbidden,
    NotFound,
    PolicyViolation,
    SchemaViolation,
    Unauthenticated,
    fdp_error_handler,
    register_exception_handlers,
)


@pytest.mark.unit
def test_concrete_error_classes_expose_envelope_metadata() -> None:
    assert NotFound.code == "fdp.not_found"
    assert NotFound.http_status == 404
    assert NotFound.docs_url.endswith("#fdp.not_found")

    assert Forbidden.http_status == 403
    assert Conflict.http_status == 409
    assert SchemaViolation.http_status == 422
    assert PolicyViolation.http_status == 403
    assert PolicyViolation.code == "fdp.policy_violation"
    assert Unauthenticated.http_status == 401
    assert Unauthenticated.code == "fdp.unauthenticated"
    assert Unauthenticated.docs_url.endswith("#fdp.unauthenticated")


@pytest.mark.unit
def test_subclass_without_code_is_rejected() -> None:
    with pytest.raises(TypeError, match="must define class attribute"):

        class BadError(FDPError):  # pyright: ignore[reportUnusedClass]
            http_status = 500
            docs_url = "https://example/x"


@pytest.mark.unit
def test_error_carries_message_and_details() -> None:
    exc = SchemaViolation("invalid", details=[{"focusNode": "a", "message": "b"}])
    assert exc.message == "invalid"
    assert exc.details == [{"focusNode": "a", "message": "b"}]
    assert str(exc) == "invalid"


@pytest.mark.unit
async def test_handler_renders_envelope_for_not_found() -> None:
    response = await fdp_error_handler(None, NotFound("missing thing"))  # type: ignore[arg-type]
    assert response.status_code == 404
    body = bytes(response.body).decode()
    assert '"code":"fdp.not_found"' in body
    assert '"message":"missing thing"' in body
    assert '"details":null' in body
    assert "fdp.not_found" in body  # docs_url tail


@pytest.mark.unit
async def test_handler_round_trips_structured_details() -> None:
    report = [{"focusNode": "ex:Catalog", "message": "missing dct:title"}]
    response = await fdp_error_handler(None, SchemaViolation("invalid", details=report))  # type: ignore[arg-type]
    assert response.status_code == 422
    body = bytes(response.body).decode()
    assert "ex:Catalog" in body
    assert "missing dct:title" in body


@pytest.mark.unit
def test_register_exception_handlers_translates_raised_error_to_envelope() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise Forbidden("nope")

    client = TestClient(app)
    response = client.get("/boom")
    assert response.status_code == 403
    body = response.json()
    assert body == {
        "code": "fdp.forbidden",
        "message": "nope",
        "docs_url": Forbidden.docs_url,
        "details": None,
    }
