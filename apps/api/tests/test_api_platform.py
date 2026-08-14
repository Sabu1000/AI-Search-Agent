from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import Field

from universal_ai_search.api.app import create_app
from universal_ai_search.api.context import REQUEST_ID_HEADER
from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)

REQUEST_ID = "018f4f3d-22f8-7c51-9f31-c22e1a7d9461"


class ExampleRequest(RequestModel):
    name: str = Field(min_length=2, max_length=20)


class ExampleResponse(ResponseModel):
    id: CanonicalUUID
    created_at: UtcDateTime
    optional: str | None = None


def _contract_app() -> FastAPI:
    app = create_app()

    @app.post(
        "/v1/_contract/example",
        response_model=ExampleResponse,
        response_model_exclude_none=True,
    )
    async def example(payload: ExampleRequest) -> ExampleResponse:
        del payload
        return ExampleResponse(
            id=UUID(REQUEST_ID),
            created_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
        )

    @app.get("/v1/_contract/failure")
    async def failure() -> None:
        raise RuntimeError("private database detail")

    @app.get("/v1/_contract/http-failure")
    async def http_failure() -> None:
        raise HTTPException(
            status_code=403,
            detail="private authorization reason",
            headers={"X-Internal-Reason": "secret"},
        )

    return app


def test_openapi_is_31_and_only_completed_product_routes_are_open() -> None:
    app = create_app()
    schema = app.openapi()

    assert schema["openapi"] == "3.1.0"
    assert set(schema["paths"]) == {
        "/health/live",
        "/health/ready",
        "/v1/auth/register",
        "/v1/auth/email/verify",
        "/v1/auth/login",
        "/v1/auth/refresh",
        "/v1/auth/logout",
        "/v1/auth/me",
        "/v1/search",
    }


def test_request_id_accepts_and_normalizes_a_valid_uuid() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/health/live", headers={REQUEST_ID_HEADER: REQUEST_ID.upper()}
        )

    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID


def test_request_id_replaces_an_invalid_value() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live", headers={REQUEST_ID_HEADER: "not-a-uuid"})

    generated = UUID(response.headers[REQUEST_ID_HEADER])
    assert str(generated) == response.headers[REQUEST_ID_HEADER]


def test_unknown_route_uses_problem_json_and_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/not-implemented", headers={REQUEST_ID_HEADER: REQUEST_ID}
        )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json() == {
        "type": "urn:uas:problem:http-404",
        "title": "Not Found",
        "status": 404,
        "code": "HTTP_404",
        "request_id": REQUEST_ID,
        "retryable": False,
    }


def test_strict_request_rejects_unknown_fields_without_echoing_input() -> None:
    with TestClient(_contract_app()) as client:
        response = client.post(
            "/v1/_contract/example",
            headers={REQUEST_ID_HEADER: REQUEST_ID},
            json={"name": "valid", "secret": "must-not-echo"},
        )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"] == [
        {"pointer": "/secret", "code": "INVALID_VALUE"}
    ]
    assert "must-not-echo" not in response.text


def test_malformed_json_returns_400_problem() -> None:
    with TestClient(_contract_app()) as client:
        response = client.post(
            "/v1/_contract/example",
            content=b'{"name":',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_JSON"


def test_response_serializes_uuid_utc_and_omits_optional_null() -> None:
    with TestClient(_contract_app()) as client:
        response = client.post("/v1/_contract/example", json={"name": "valid"})

    assert response.status_code == 200
    assert response.json() == {
        "id": REQUEST_ID,
        "created_at": "2026-08-03T16:00:00Z",
    }


def test_unexpected_error_is_opaque() -> None:
    with TestClient(_contract_app(), raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/_contract/failure", headers={REQUEST_ID_HEADER: REQUEST_ID}
        )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert response.json()["request_id"] == REQUEST_ID
    assert "database" not in response.text


def test_generic_http_exception_does_not_echo_details_or_unsafe_headers() -> None:
    with TestClient(_contract_app()) as client:
        response = client.get("/v1/_contract/http-failure")

    assert response.status_code == 403
    assert response.json()["code"] == "HTTP_403"
    assert "private authorization reason" not in response.text
    assert "X-Internal-Reason" not in response.headers
