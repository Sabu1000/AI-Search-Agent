"""Public verification resend with a uniform response."""

from contextlib import suppress
from typing import cast

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from universal_ai_search.api.models import RequestModel
from universal_ai_search.auth.verification_resend import VerificationResendService

from .auth import GenericRegistrationResponse

router = APIRouter(prefix="/auth/email", tags=["authentication"])


class ResendBody(RequestModel):
    email: str = Field(max_length=320)


@router.post("/resend", status_code=202, response_model=GenericRegistrationResponse)
async def resend(
    body: ResendBody, request: Request, response: Response
) -> GenericRegistrationResponse:
    response.headers["Cache-Control"] = "no-store"
    service = cast(
        VerificationResendService | None,
        getattr(request.app.state, "verification_resend_service", None),
    )
    origin = request.headers.get("origin")
    if (
        service is not None
        and request.client is not None
        and (
            origin is None
            or origin == request.app.state.authentication_service.web_origin
        )
    ):
        with suppress(SQLAlchemyError):
            await service.request(body.email, request.client.host)
    return GenericRegistrationResponse(
        status="verification_if_eligible",
        message="If the account is eligible, verification instructions will follow.",
    )
