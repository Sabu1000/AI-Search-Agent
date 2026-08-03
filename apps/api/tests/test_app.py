from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.platform.readiness import ReadinessResponse


def test_liveness_reports_versioned_api() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "service": "api",
        "status": "ok",
        "version": "0.1.0",
    }


def test_readiness_succeeds_when_dependencies_are_available() -> None:
    result = ReadinessResponse(
        service="api",
        status="ok",
        version="0.1.0",
        dependencies={
            "database": "ok",
            "redis": "ok",
            "object_storage": "ok",
        },
    )
    with (
        patch(
            "universal_ai_search.api.routes.health.check_readiness",
            new=AsyncMock(return_value=result),
        ),
        TestClient(create_app()) as client,
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == result.model_dump()


def test_readiness_fails_closed_when_a_dependency_is_unavailable() -> None:
    result = ReadinessResponse(
        service="api",
        status="degraded",
        version="0.1.0",
        dependencies={
            "database": "ok",
            "redis": "unavailable",
            "object_storage": "ok",
        },
    )
    with (
        patch(
            "universal_ai_search.api.routes.health.check_readiness",
            new=AsyncMock(return_value=result),
        ),
        TestClient(create_app()) as client,
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == result.model_dump()
