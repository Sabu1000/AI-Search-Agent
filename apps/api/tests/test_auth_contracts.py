from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    WorkspaceContext,
    WorkspaceDependency,
)

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000001")


class StaticAuthenticationBackend:
    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal

    async def authenticate(self, request: object) -> Principal | None:
        del request
        return self.principal


class StaticWorkspaceBackend:
    def __init__(self, context: WorkspaceContext | None) -> None:
        self.context = context
        self.calls: list[tuple[Principal, UUID]] = []

    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        self.calls.append((principal, workspace_id))
        return self.context


def _principal() -> Principal:
    return Principal(
        subject_id=USER_ID,
        mode=AuthenticationMode.BEARER,
        authorization_version=3,
    )


def _workspace() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        role="owner",
        authorization_version=3,
    )


def _protected_app(
    authentication: StaticAuthenticationBackend,
    workspace: StaticWorkspaceBackend,
) -> FastAPI:
    app = create_app()
    app.state.authentication_backend = authentication
    app.state.workspace_authorization_backend = workspace

    @app.get("/v1/_contract/protected")
    async def protected(context: WorkspaceDependency) -> dict[str, str]:
        return {"workspace_id": str(context.workspace_id)}

    return app


def test_default_authentication_backend_fails_closed() -> None:
    app = create_app()

    @app.get("/v1/_contract/protected")
    async def protected(context: WorkspaceDependency) -> dict[str, str]:
        return {"workspace_id": str(context.workspace_id)}

    with TestClient(app) as client:
        response = client.get(
            "/v1/_contract/protected", headers={"X-Workspace-ID": str(WORKSPACE_ID)}
        )

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_workspace_header_is_required_after_authentication() -> None:
    workspace = StaticWorkspaceBackend(_workspace())
    app = _protected_app(StaticAuthenticationBackend(_principal()), workspace)

    with TestClient(app) as client:
        response = client.get("/v1/_contract/protected")

    assert response.status_code == 400
    assert response.json()["code"] == "WORKSPACE_REQUIRED"
    assert workspace.calls == []


def test_malformed_workspace_header_is_rejected_before_authorization() -> None:
    workspace = StaticWorkspaceBackend(_workspace())
    app = _protected_app(StaticAuthenticationBackend(_principal()), workspace)

    with TestClient(app) as client:
        response = client.get(
            "/v1/_contract/protected", headers={"X-Workspace-ID": "not-a-uuid"}
        )

    assert response.status_code == 400
    assert response.json()["code"] == "WORKSPACE_HEADER_INVALID"
    assert workspace.calls == []


def test_inaccessible_workspace_is_concealed() -> None:
    workspace = StaticWorkspaceBackend(None)
    principal = _principal()
    app = _protected_app(StaticAuthenticationBackend(principal), workspace)

    with TestClient(app) as client:
        response = client.get(
            "/v1/_contract/protected",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"
    assert workspace.calls == [(principal, WORKSPACE_ID)]


def test_authorized_workspace_context_reaches_handler() -> None:
    context = _workspace()
    workspace = StaticWorkspaceBackend(context)
    app = _protected_app(StaticAuthenticationBackend(_principal()), workspace)

    with TestClient(app) as client:
        response = client.get(
            "/v1/_contract/protected",
            headers={"X-Workspace-ID": str(WORKSPACE_ID)},
        )

    assert response.status_code == 200
    assert response.json() == {"workspace_id": str(WORKSPACE_ID)}
