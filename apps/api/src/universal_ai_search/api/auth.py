"""Fail-closed authentication and workspace authorization interfaces."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import Depends, Request

from universal_ai_search.api.models import APIModel
from universal_ai_search.api.problems import ProblemError

WORKSPACE_HEADER = "X-Workspace-ID"


class AuthenticationMode(StrEnum):
    BROWSER = "browser"
    BEARER = "bearer"
    DEVICE = "device"
    DELETION_RECEIPT = "deletion_receipt"


class Principal(APIModel):
    model_config = APIModel.model_config | {"frozen": True}

    subject_id: UUID
    mode: AuthenticationMode
    authorization_version: int


class WorkspaceContext(APIModel):
    model_config = APIModel.model_config | {"frozen": True}

    workspace_id: UUID
    user_id: UUID
    role: str
    authorization_version: int


class AuthenticationBackend(Protocol):
    async def authenticate(self, request: Request) -> Principal | None: ...


class WorkspaceAuthorizationBackend(Protocol):
    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None: ...


class RejectingAuthenticationBackend:
    async def authenticate(self, request: Request) -> Principal | None:
        del request
        return None


class RejectingWorkspaceAuthorizationBackend:
    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        del principal, workspace_id
        return None


async def require_principal(request: Request) -> Principal:
    backend = cast(AuthenticationBackend, request.app.state.authentication_backend)
    principal = await backend.authenticate(request)
    if principal is None:
        raise ProblemError(
            status=401,
            code="AUTHENTICATION_REQUIRED",
            title="Authentication required",
            detail="Sign in to continue.",
        )
    return principal


async def require_workspace(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> WorkspaceContext:
    supplied_workspace = request.headers.get(WORKSPACE_HEADER)
    if supplied_workspace is None:
        raise ProblemError(
            status=400,
            code="WORKSPACE_REQUIRED",
            title="Workspace is required",
            detail="Select a workspace and try again.",
        )
    try:
        workspace_id = UUID(supplied_workspace)
    except ValueError as error:
        raise ProblemError(
            status=400,
            code="WORKSPACE_HEADER_INVALID",
            title="Workspace header is invalid",
            detail="Select a workspace and try again.",
        ) from error
    backend = cast(
        WorkspaceAuthorizationBackend,
        request.app.state.workspace_authorization_backend,
    )
    context = await backend.authorize(
        principal=principal,
        workspace_id=workspace_id,
    )
    if context is None:
        raise ProblemError(
            status=404,
            code="WORKSPACE_NOT_FOUND",
            title="Workspace not found",
            detail="The requested workspace is unavailable.",
        )
    request.state.principal = principal
    request.state.workspace = context
    return context


PrincipalDependency = Annotated[Principal, Depends(require_principal)]
WorkspaceDependency = Annotated[WorkspaceContext, Depends(require_workspace)]
