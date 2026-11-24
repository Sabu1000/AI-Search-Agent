"""Public, enumeration-resistant account recovery endpoints."""

from __future__ import annotations

from contextlib import suppress
from typing import cast

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.models import RequestModel, ResponseModel
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.recovery import (
    InvalidRecoveryToken,
    PasswordRecoveryService,
    RecoveryPasswordRejected,
)

router = APIRouter(prefix="/auth/password", tags=["authentication"])
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class ResetRequest(RequestModel):
    email: str = Field(max_length=320)


class ResetRequestResponse(ResponseModel):
    message: str = "If the account is eligible, a password reset email will be sent."


class CompleteReset(RequestModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


def _service(request: Request) -> PasswordRecoveryService | None:
    return cast(
        PasswordRecoveryService | None,
        getattr(request.app.state, "password_recovery_service", None),
    )


def _valid_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return (
        origin is None or origin == request.app.state.authentication_service.web_origin
    )


@router.post("/request-reset", status_code=202, response_model=ResetRequestResponse)
async def request_password_reset(
    body: ResetRequest, request: Request, response: Response
) -> ResetRequestResponse:
    response.headers.update(_HEADERS)
    service = _service(request)
    if service is not None and _valid_origin(request) and request.client is not None:
        # Dependency failure is fail-closed and doesn't identify an account.
        with suppress(SQLAlchemyError):
            await service.request_reset(email=body.email, client_ip=request.client.host)
    return ResetRequestResponse()


@router.post("/reset", status_code=204, response_class=Response)
async def complete_password_reset(body: CompleteReset, request: Request) -> Response:
    service = _service(request)
    if service is None or not _valid_origin(request):
        raise ProblemError(
            status=503,
            code="PASSWORD_RESET_UNAVAILABLE",
            title="Password reset is temporarily unavailable",
        )
    try:
        await service.reset_password(
            token=body.token,
            password=body.new_password,
            request_id=request_id_for(request),
        )
    except InvalidRecoveryToken:
        raise ProblemError(
            status=400,
            code="PASSWORD_RESET_INVALID",
            title="Password reset link is expired or invalid",
        ) from None
    except RecoveryPasswordRejected:
        raise ProblemError(
            status=422,
            code="NEW_PASSWORD_REJECTED",
            title="Choose a password that meets the requirements",
        ) from None
    except SQLAlchemyError:
        raise ProblemError(
            status=503,
            code="PASSWORD_RESET_UNAVAILABLE",
            title="Password reset is temporarily unavailable",
        ) from None
    return Response(status_code=204, headers=_HEADERS)
