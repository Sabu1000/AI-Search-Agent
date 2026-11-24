from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.auth.recovery import (
    InvalidRecoveryToken,
    RecoveryPasswordRejected,
)

BASE = "/v1/auth/password"


def test_request_always_has_generic_response_and_blocks_cross_origin() -> None:
    app = create_app()
    recovery = SimpleNamespace(request_reset=AsyncMock())
    with TestClient(app) as client:
        disabled = client.post(
            f"{BASE}/request-reset", json={"email": "a@example.test"}
        )
        app.state.password_recovery_service = recovery
        same_origin = client.post(
            f"{BASE}/request-reset",
            json={"email": "a@example.test"},
            headers={"origin": "http://localhost:3000"},
        )
        other_origin = client.post(
            f"{BASE}/request-reset",
            json={"email": "a@example.test"},
            headers={"origin": "https://unrelated.example"},
        )
    assert disabled.status_code == same_origin.status_code == other_origin.status_code
    assert same_origin.status_code == 202
    assert disabled.json() == same_origin.json() == other_origin.json()
    assert same_origin.headers["cache-control"] == "no-store"
    recovery.request_reset.assert_awaited_once_with(
        email="a@example.test", client_ip="testclient"
    )


def test_reset_accepts_credentials_only_in_body_and_never_echoes_them() -> None:
    app = create_app()
    request_id = uuid4()
    service = SimpleNamespace(reset_password=AsyncMock())
    payload = {"token": "redacted-fixture", "new_password": "test-only-password"}
    with TestClient(app) as client:
        unavailable = client.post(f"{BASE}/reset", json=payload)
        app.state.password_recovery_service = service
        accepted = client.post(
            f"{BASE}/reset", json=payload, headers={"x-request-id": str(request_id)}
        )
        service.reset_password.assert_awaited_once_with(
            token="redacted-fixture",
            password="test-only-password",
            request_id=request_id,
        )
        service.reset_password.side_effect = InvalidRecoveryToken
        invalid = client.post(f"{BASE}/reset", json=payload)
        service.reset_password.side_effect = RecoveryPasswordRejected
        rejected = client.post(f"{BASE}/reset", json=payload)
    assert unavailable.status_code == 503
    assert accepted.status_code == 204
    assert not accepted.content
    assert accepted.headers["referrer-policy"] == "no-referrer"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "PASSWORD_RESET_INVALID"
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "NEW_PASSWORD_REJECTED"
    for response in (unavailable, invalid, rejected):
        assert not any(value in response.text for value in payload.values())
