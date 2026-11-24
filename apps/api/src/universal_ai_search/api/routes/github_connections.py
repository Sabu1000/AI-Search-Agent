"""Browser-only authorization endpoints for selected GitHub repositories."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import Field

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
    WorkspaceDependency,
)
from universal_ai_search.api.models import RequestModel, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.service import AuthenticationService
from universal_ai_search.connections.github import GitHubAuthorizationError
from universal_ai_search.connections.github_service import GitHubConnectionService

from .connections import _require_csrf

router = APIRouter(prefix="/connections/github", tags=["connections"])


def _service(request: Request) -> GitHubConnectionService:
    service = cast(
        GitHubConnectionService | None, request.app.state.github_connection_service
    )
    if service is None:
        raise ProblemError(
            status=503,
            code="GITHUB_CONNECTION_UNAVAILABLE",
            title="GitHub connection is unavailable",
        )
    return service


ServiceDependency = Annotated[GitHubConnectionService, Depends(_service)]


class GitHubAuthorizeRequest(RequestModel):
    repository_ids: tuple[Annotated[int, Field(strict=True, gt=0, lt=2**53)], ...] = (
        Field(min_length=1, max_length=500)
    )
    return_path: str = Field(max_length=500)


class GitHubAuthorizeResponse(ResponseModel):
    authorization_url: str
    expires_at: UtcDateTime


def _invalid() -> ProblemError:
    return ProblemError(
        status=400,
        code="GITHUB_AUTHORIZATION_INVALID",
        title="GitHub authorization could not be completed",
    )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(
        url,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/authorize", response_model=GitHubAuthorizeResponse)
async def authorize_github(
    body: GitHubAuthorizeRequest,
    request: Request,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    service: ServiceDependency,
) -> GitHubAuthorizeResponse:
    if (
        workspace.role not in {"owner", "admin"}
        or principal.session_id is None
        or principal.mode != AuthenticationMode.BROWSER
    ):
        raise ProblemError(
            status=403,
            code="CONNECTION_MANAGEMENT_FORBIDDEN",
            title="Connection management is not allowed",
        )
    await _require_csrf(request, principal.mode)
    try:
        result = await service.start(
            workspace_id=workspace.workspace_id,
            user_id=principal.subject_id,
            session_id=principal.session_id,
            repository_ids=frozenset(body.repository_ids),
            return_path=body.return_path,
        )
    except GitHubAuthorizationError:
        raise _invalid() from None
    return GitHubAuthorizeResponse(
        authorization_url=result.authorization_url, expires_at=result.expires_at
    )


@router.get("/callback", response_model=None)
async def github_callback(
    request: Request,
    principal: PrincipalDependency,
    service: ServiceDependency,
    state: Annotated[str, Query(min_length=64, max_length=64)],
    installation_id: Annotated[int | None, Query(gt=0, lt=2**53)] = None,
    code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    if principal.session_id is None or principal.mode != AuthenticationMode.BROWSER:
        raise _invalid()
    try:
        if error is None and installation_id is not None and code is None:
            url = await service.begin_consent(
                state=state,
                user_id=principal.subject_id,
                session_id=principal.session_id,
                installation_id=installation_id,
            )
            return _redirect(url)
        if error is None and code is not None and installation_id is None:
            result = await service.complete(
                state=state,
                user_id=principal.subject_id,
                session_id=principal.session_id,
                code=code,
            )
            query = urlencode(
                {"connection_id": str(result.connection_id), "result": "pending_sync"}
            )
            auth = cast(AuthenticationService, request.app.state.authentication_service)
            return _redirect(
                f"{auth.web_origin.rstrip('/')}{result.return_path}?{query}"
            )
        await service.deny(
            state=state,
            user_id=principal.subject_id,
            session_id=principal.session_id,
        )
    except GitHubAuthorizationError:
        raise _invalid() from None
    raise _invalid()
