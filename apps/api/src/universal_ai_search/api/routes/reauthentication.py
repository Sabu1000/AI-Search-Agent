"""Authenticated route for a one-time recent-password proof."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
    WorkspaceDependency,
)
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.models import RequestModel, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationLimited,
    ReauthenticationService,
    ReauthenticationUnavailable,
)
from universal_ai_search.auth.reauth_store import ReauthPurpose, ReauthScope

from .connections import _require_csrf

router = APIRouter(prefix="/auth", tags=["authentication"])
_NO_CACHE = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization, Cookie, X-Workspace-ID",
}


class ReauthenticateRequest(RequestModel):
    password: SecretStr = Field(min_length=1, max_length=128)
    purpose: ReauthPurpose


class ReauthenticateResponse(ResponseModel):
    reauth_token: str = Field(repr=False)
    expires_at: UtcDateTime


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="REAUTHENTICATION_UNAVAILABLE",
        title="Reauthentication is unavailable",
        headers=_NO_CACHE,
    )


@router.post("/reauthenticate", response_model=ReauthenticateResponse)
async def reauthenticate(
    body: ReauthenticateRequest,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
) -> ReauthenticateResponse:
    if (
        principal.mode not in {AuthenticationMode.BEARER, AuthenticationMode.BROWSER}
        or principal.session_id is None
        or principal.subject_id != workspace.user_id
    ):
        raise ProblemError(
            status=401,
            code="SESSION_INVALID",
            title="Session is invalid",
            headers=_NO_CACHE,
        )
    await _require_csrf(request, principal.mode)
    service = cast(
        ReauthenticationService | None,
        getattr(request.app.state, "reauth_service", None),
    )
    if service is None:
        raise _unavailable()
    scope = ReauthScope(
        principal.subject_id,
        principal.session_id,
        workspace.workspace_id,
        principal.authorization_version,
        workspace.authorization_version,
    )
    try:
        proof = await service.obtain(
            scope=scope,
            purpose=body.purpose,
            password=body.password.get_secret_value(),
            request_id=request_id_for(request),
        )
    except ReauthenticationFailed:
        raise ProblemError(
            status=401,
            code="REAUTHENTICATION_FAILED",
            title="Reauthentication failed",
            headers=_NO_CACHE,
        ) from None
    except ReauthenticationLimited:
        raise ProblemError(
            status=429,
            code="RATE_LIMITED",
            title="Too many reauthentication attempts",
            retryable=True,
            headers={**_NO_CACHE, "Retry-After": "300"},
        ) from None
    except (ReauthenticationUnavailable, SQLAlchemyError):
        raise _unavailable() from None
    response.headers.update(_NO_CACHE)
    return ReauthenticateResponse(reauth_token=proof.token, expires_at=proof.expires_at)
