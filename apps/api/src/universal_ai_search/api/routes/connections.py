"""Google connection authorization endpoints."""

from __future__ import annotations

import hmac
from typing import Annotated, Literal, cast
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
from universal_ai_search.auth.backends import CSRF_COOKIE
from universal_ai_search.auth.service import AuthenticationService
from universal_ai_search.auth.store import SessionIdentity
from universal_ai_search.connections.service import (
    GoogleAuthorizationError,
    GoogleAuthorizationUnavailable,
    GoogleConnectionService,
)

router = APIRouter(prefix="/connections/google", tags=["connections"])


def _connection_service(request: Request) -> GoogleConnectionService:
    return cast(GoogleConnectionService, request.app.state.google_connection_service)


ConnectionServiceDependency = Annotated[
    GoogleConnectionService, Depends(_connection_service)
]


class GoogleAuthorizeRequest(RequestModel):
    source_families: tuple[Literal["gmail", "google_drive"], ...] = Field(
        min_length=1, max_length=2
    )
    return_path: str = Field(pattern=r"^/[A-Za-z0-9/_-]*$", max_length=500)


class GoogleAuthorizeResponse(ResponseModel):
    authorization_url: str
    expires_at: UtcDateTime


async def _require_csrf(request: Request, principal_mode: AuthenticationMode) -> None:
    if principal_mode is not AuthenticationMode.BROWSER:
        return
    service = cast(AuthenticationService, request.app.state.authentication_service)
    header = request.headers.get("X-CSRF-Token")
    cookie = request.cookies.get(CSRF_COOKIE)
    identity = getattr(request.state, "session_identity", None)
    if (
        request.headers.get("origin") != service.web_origin
        or header is None
        or cookie is None
        or not hmac.compare_digest(header, cookie)
        or not isinstance(identity, SessionIdentity)
        or not await service.validate_csrf(identity, header)
    ):
        raise ProblemError(
            status=403, code="CSRF_FAILED", title="CSRF validation failed"
        )


@router.post("/authorize", response_model=GoogleAuthorizeResponse)
async def authorize_google(
    body: GoogleAuthorizeRequest,
    request: Request,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    service: ConnectionServiceDependency,
) -> GoogleAuthorizeResponse:
    if workspace.role not in {"owner", "admin"} or principal.session_id is None:
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
            source_families=body.source_families,
            return_path=body.return_path,
        )
    except GoogleAuthorizationUnavailable as error:
        raise ProblemError(
            status=503,
            code="GOOGLE_CONNECTION_UNAVAILABLE",
            title="Google connection is unavailable",
        ) from error
    except GoogleAuthorizationError as error:
        raise ProblemError(
            status=422,
            code="GOOGLE_SOURCE_FAMILIES_INVALID",
            title="Google source selection is invalid",
        ) from error
    return GoogleAuthorizeResponse(
        authorization_url=result.authorization_url, expires_at=result.expires_at
    )


@router.get("/callback", response_model=None)
async def google_callback(
    request: Request,
    principal: PrincipalDependency,
    service: ConnectionServiceDependency,
    state: Annotated[str, Query(min_length=43, max_length=256)],
    code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    if principal.session_id is None:
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    try:
        if error is not None or code is None:
            return_path = await service.consume_error(
                state=state,
                user_id=principal.subject_id,
                session_id=principal.session_id,
            )
            query = urlencode({"result": "authorization_denied"})
        else:
            result = await service.complete(
                state=state,
                code=code,
                user_id=principal.subject_id,
                session_id=principal.session_id,
            )
            return_path = result.return_path
            query = urlencode(
                {"connection_id": str(result.connection_id), "result": "connected"}
            )
    except GoogleAuthorizationUnavailable as failure:
        raise ProblemError(
            status=503,
            code="GOOGLE_CONNECTION_UNAVAILABLE",
            title="Google connection is unavailable",
        ) from failure
    except GoogleAuthorizationError as failure:
        raise ProblemError(
            status=400,
            code="OAUTH_TRANSACTION_INVALID",
            title="Authorization could not be completed",
        ) from failure
    response = RedirectResponse(f"{return_path}?{query}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response
