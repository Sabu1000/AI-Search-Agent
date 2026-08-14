from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.connections.service import (
    AuthorizationComplete,
    AuthorizationStart,
    GoogleAuthorizationError,
    GoogleAuthorizationUnavailable,
)

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("40000000-0000-4000-8000-000000000001")


class Backend:
    async def authenticate(self, request: object) -> Principal:
        return Principal(
            subject_id=USER_ID,
            session_id=SESSION_ID,
            mode=AuthenticationMode.BEARER,
            authorization_version=1,
            access_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )


class WorkspaceBackend:
    def __init__(self, role: str = "owner") -> None:
        self.role = role

    async def authorize(self, **values: object) -> WorkspaceContext:
        return WorkspaceContext(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            role=self.role,
            authorization_version=1,
        )


def _client(role: str = "owner") -> tuple[SimpleNamespace, TestClient]:
    app = create_app()
    service = SimpleNamespace(
        start=AsyncMock(
            return_value=AuthorizationStart(
                "https://accounts.google.test/authorize",
                datetime.now(UTC) + timedelta(minutes=10),
            )
        ),
        complete=AsyncMock(
            return_value=AuthorizationComplete(CONNECTION_ID, "/settings/connections")
        ),
        consume_error=AsyncMock(return_value="/settings/connections"),
    )
    app.state.google_connection_service = service
    app.state.authentication_backend = Backend()
    app.state.workspace_authorization_backend = WorkspaceBackend(role)
    return service, TestClient(app, base_url="https://testserver")


def test_authorize_google_returns_short_lived_provider_url() -> None:
    service, client = _client()
    with client:
        response = client.post(
            "/v1/connections/google/authorize",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json={
                "source_families": ["gmail"],
                "return_path": "/settings/connections",
            },
        )

    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://accounts.google")
    service.start.assert_awaited_once()


def test_authorize_google_requires_manager_and_enabled_provider() -> None:
    _, member_client = _client("member")
    with member_client:
        forbidden = member_client.post(
            "/v1/connections/google/authorize",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json={"source_families": ["gmail"], "return_path": "/connections"},
        )
    assert forbidden.status_code == 403

    service, client = _client()
    service.start.side_effect = GoogleAuthorizationUnavailable
    with client:
        unavailable = client.post(
            "/v1/connections/google/authorize",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
            json={"source_families": ["gmail"], "return_path": "/connections"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "GOOGLE_CONNECTION_UNAVAILABLE"


def test_google_callback_redirects_without_provider_secrets() -> None:
    service, client = _client()
    state = "s" * 43
    with client:
        success = client.get(
            "/v1/connections/google/callback",
            params={"state": state, "code": "private-provider-code"},
            follow_redirects=False,
        )
        denied = client.get(
            "/v1/connections/google/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )

    assert success.status_code == 303
    assert success.headers["location"] == (
        f"/settings/connections?connection_id={CONNECTION_ID}&result=connected"
    )
    assert "private-provider-code" not in success.headers["location"]
    assert denied.headers["location"].endswith("?result=authorization_denied")
    service.complete.assert_awaited_once()
    service.consume_error.assert_awaited_once()


def test_google_callback_maps_invalid_transaction_to_safe_problem() -> None:
    service, client = _client()
    service.complete.side_effect = GoogleAuthorizationError
    with client:
        response = client.get(
            "/v1/connections/google/callback",
            params={"state": "s" * 43, "code": "provider-code"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "OAUTH_TRANSACTION_INVALID"
    assert "provider-code" not in response.text
