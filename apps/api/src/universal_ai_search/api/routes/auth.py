"""Application authentication endpoints."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, field_validator

from universal_ai_search.api.auth import AuthenticationMode, PrincipalDependency
from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.backends import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE
from universal_ai_search.auth.service import (
    AuthenticationFailure,
    AuthenticationService,
    InvalidVerificationToken,
    IssuedSession,
)
from universal_ai_search.auth.store import SessionIdentity

router = APIRouter(prefix="/auth", tags=["authentication"])


def _service(request: Request) -> AuthenticationService:
    return cast(AuthenticationService, request.app.state.authentication_service)


ServiceDependency = Annotated[AuthenticationService, Depends(_service)]


class RegisterRequest(RequestModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    terms_version: str = Field(min_length=1, max_length=50)
    locale: str = Field(default="en-US", min_length=2, max_length=35)

    @field_validator("email")
    @classmethod
    def email_shape(cls, value: str) -> str:
        normalized = value.strip()
        if (
            normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
        ):
            raise ValueError("invalid email address")
        return normalized


class GenericRegistrationResponse(ResponseModel):
    status: Literal["verification_if_eligible"]
    message: str


class VerifyEmailRequest(RequestModel):
    token: str = Field(min_length=32, max_length=256)


class LoginRequest(RequestModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    client_type: Literal["browser", "native"] = "browser"


class RefreshRequest(RequestModel):
    client_type: Literal["browser", "native"] = "browser"
    refresh_token: str | None = Field(default=None, min_length=32, max_length=256)


class MembershipResponse(ResponseModel):
    workspace_id: CanonicalUUID
    name: str
    role: str
    status: str


class UserResponse(ResponseModel):
    id: CanonicalUUID
    email: str
    full_name: str
    email_verified: bool


class SessionResponse(ResponseModel):
    user: UserResponse
    memberships: list[MembershipResponse]
    preferred_workspace_id: CanonicalUUID | None
    access_expires_at: UtcDateTime
    csrf_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None


class MeResponse(ResponseModel):
    user: UserResponse
    memberships: list[MembershipResponse]
    preferred_workspace_id: CanonicalUUID | None
    access_expires_at: UtcDateTime


def _set_session_cookies(response: Response, session: IssuedSession) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        session.access_token,
        max_age=15 * 60,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=30 * 24 * 60 * 60,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf_token,
        max_age=30 * 24 * 60 * 60,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=True, samesite="lax")
    for cookie in (REFRESH_COOKIE, CSRF_COOKIE):
        response.delete_cookie(cookie, path="/", secure=True, samesite="strict")


def _mark_private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Origin, Authorization, Cookie"


async def _session_response(
    service: AuthenticationService,
    session: IssuedSession,
    client_type: Literal["browser", "native"],
) -> SessionResponse:
    current = await service.current_account(session.identity.user_id)
    if current is None:
        raise AuthenticationFailure
    memberships = [
        MembershipResponse(
            workspace_id=item.workspace_id,
            name=item.name,
            role=item.role,
            status=item.status,
        )
        for item in current.memberships
    ]
    return SessionResponse(
        user=UserResponse(
            id=current.account.user_id,
            email=current.account.email,
            full_name=current.account.full_name,
            email_verified=current.account.email_verified,
        ),
        memberships=memberships,
        preferred_workspace_id=memberships[0].workspace_id if memberships else None,
        access_expires_at=session.access_expires_at,
        csrf_token=session.csrf_token if client_type == "browser" else None,
        access_token=session.access_token if client_type == "native" else None,
        refresh_token=session.refresh_token if client_type == "native" else None,
    )


@router.post("/register", response_model=GenericRegistrationResponse, status_code=202)
async def register(
    body: RegisterRequest, service: ServiceDependency
) -> GenericRegistrationResponse:
    try:
        await service.register(
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            terms_version=body.terms_version,
            locale=body.locale,
        )
    except ValueError as error:
        raise ProblemError(
            status=422,
            code="PASSWORD_POLICY_FAILED",
            title="Password does not meet requirements",
            detail=str(error),
        ) from error
    return GenericRegistrationResponse(
        status="verification_if_eligible",
        message=(
            "If this address can be registered, verification instructions will follow."
        ),
    )


@router.post("/email/verify", status_code=204)
async def verify_email(
    body: VerifyEmailRequest, service: ServiceDependency
) -> Response:
    try:
        await service.verify_email(body.token)
    except InvalidVerificationToken as error:
        raise ProblemError(
            status=400,
            code="VERIFICATION_TOKEN_INVALID",
            title="Verification link is invalid",
            detail="Request a new verification link and try again.",
        ) from error
    return Response(status_code=204)


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: ServiceDependency,
) -> SessionResponse:
    try:
        session = await service.login(
            email=body.email,
            password=body.password,
            device_metadata={"user_agent": request.headers.get("user-agent", "")[:500]},
        )
        result = await _session_response(service, session, body.client_type)
    except AuthenticationFailure as error:
        raise ProblemError(
            status=401,
            code="INVALID_CREDENTIALS",
            title="Invalid credentials",
            detail="The email or password is incorrect.",
        ) from error
    if body.client_type == "browser":
        _set_session_cookies(response, session)
    _mark_private(response)
    return result


def _csrf_from_request(request: Request, expected_origin: str) -> str | None:
    if request.headers.get("origin") != expected_origin:
        return None
    header = request.headers.get("X-CSRF-Token")
    cookie = request.cookies.get(CSRF_COOKIE)
    if header is None or cookie is None or not hmac.compare_digest(header, cookie):
        return None
    return header


@router.post("/refresh", response_model=SessionResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    response: Response,
    service: ServiceDependency,
) -> SessionResponse:
    if body.client_type == "browser":
        refresh_token = request.cookies.get(REFRESH_COOKIE)
        csrf_token = _csrf_from_request(request, service.web_origin)
        if csrf_token is None:
            raise ProblemError(
                status=403, code="CSRF_FAILED", title="CSRF validation failed"
            )
    else:
        refresh_token = body.refresh_token
        csrf_token = None
    if refresh_token is None:
        raise ProblemError(
            status=401, code="AUTHENTICATION_REQUIRED", title="Authentication required"
        )
    try:
        session = await service.refresh(refresh_token, csrf_token)
        result = await _session_response(service, session, body.client_type)
    except AuthenticationFailure as error:
        _clear_session_cookies(response)
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        ) from error
    if body.client_type == "browser":
        _set_session_cookies(response, session)
    _mark_private(response)
    return result


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> Response:
    identity = cast(SessionIdentity, request.state.session_identity)
    if principal.mode is AuthenticationMode.BROWSER:
        csrf = _csrf_from_request(request, service.web_origin)
        if csrf is None or not await service.validate_csrf(identity, csrf):
            raise ProblemError(
                status=403, code="CSRF_FAILED", title="CSRF validation failed"
            )
    await service.logout(identity)
    _clear_session_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=MeResponse)
async def me(
    response: Response, principal: PrincipalDependency, service: ServiceDependency
) -> MeResponse:
    current = await service.current_account(principal.subject_id)
    if current is None:
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    memberships = [
        MembershipResponse(
            workspace_id=item.workspace_id,
            name=item.name,
            role=item.role,
            status=item.status,
        )
        for item in current.memberships
    ]
    result = MeResponse(
        user=UserResponse(
            id=current.account.user_id,
            email=current.account.email,
            full_name=current.account.full_name,
            email_verified=current.account.email_verified,
        ),
        memberships=memberships,
        preferred_workspace_id=memberships[0].workspace_id if memberships else None,
        access_expires_at=cast(datetime, principal.access_expires_at),
    )
    _mark_private(response)
    return result
