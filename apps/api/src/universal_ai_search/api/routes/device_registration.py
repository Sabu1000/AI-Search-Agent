"""Native-bearer-only device registration routes; no browser pairing bridge."""

from __future__ import annotations

import json
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, ValidationError
from starlette.requests import ClientDisconnect

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.registration import (
    DevicePlatform,
    DeviceRegistrationRateLimited,
    DeviceRegistrationRejected,
    DeviceRegistrationService,
    DeviceRegistrationUnavailable,
    RegistrationScope,
)

router = APIRouter(prefix="/devices", tags=["devices"])
_MAX_BODY_BYTES = 2048
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Origin, Authorization, Cookie, X-Workspace-ID",
}


class DeviceChallengeResponse(ResponseModel):
    challenge_id: CanonicalUUID
    challenge: str = Field(repr=False)
    expires_at: UtcDateTime


class DeviceRegisterRequest(RequestModel):
    challenge_id: CanonicalUUID
    public_key: str = Field(min_length=43, max_length=43, repr=False)
    signature: str = Field(min_length=86, max_length=86, repr=False)
    platform: DevicePlatform
    app_version: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=150)


class DeviceRegisterResponse(ResponseModel):
    device_id: CanonicalUUID
    device_credential: str = Field(repr=False)


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DEVICE_REGISTRATION_UNAVAILABLE",
        title="Device registration is unavailable",
        retryable=True,
        headers=_PRIVATE_HEADERS,
    )


def _invalid_body() -> ProblemError:
    return ProblemError(
        status=422,
        code="DEVICE_REGISTRATION_BODY_INVALID",
        title="Device registration request is invalid",
        headers=_PRIVATE_HEADERS,
    )


def _rejected() -> ProblemError:
    return ProblemError(
        status=403,
        code="DEVICE_REGISTRATION_REJECTED",
        title="Device registration could not be authorized",
        headers=_PRIVATE_HEADERS,
    )


def _service(request: Request) -> DeviceRegistrationService:
    service = getattr(request.app.state, "device_registration_service", None)
    if service is None:
        raise _unavailable()
    return cast(DeviceRegistrationService, service)


RegistrationDependency = Annotated[DeviceRegistrationService, Depends(_service)]


def _native_scope(
    request: Request, principal: Principal, workspace: WorkspaceContext
) -> RegistrationScope:
    # Only native process bearer clients can receive the device credential.
    # Browser CSRF is ruled out, not bypassed: cookies, Origin and fetch
    # metadata are not accepted, even when accompanied by a bearer token.
    if (
        principal.mode is not AuthenticationMode.BEARER
        or principal.session_id is None
        or workspace.user_id != principal.subject_id
        or len(request.headers.getlist("authorization")) != 1
        or len(request.headers.getlist("x-workspace-id")) != 1
        or any(
            name in request.headers
            for name in ("origin", "cookie", "sec-fetch-mode", "sec-fetch-site")
        )
    ):
        raise _rejected()
    return RegistrationScope(
        workspace_id=workspace.workspace_id,
        user_id=principal.subject_id,
        session_id=principal.session_id,
        user_authorization_version=principal.authorization_version,
        workspace_authorization_version=workspace.authorization_version,
    )


def _unique_json_object(items: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name, value in items:
        if name in fields:
            raise ValueError("duplicate JSON field")
        fields[name] = value
    return fields


async def _read_bounded_json(request: Request, *, allow_empty: bool) -> object:
    encodings = request.headers.getlist("content-encoding")
    types = request.headers.getlist("content-type")
    if encodings and encodings != ["identity"]:
        raise _invalid_body()
    if types and (
        len(types) != 1
        or types[0].lower()
        not in ("application/json", "application/json; charset=utf-8")
    ):
        raise _invalid_body()
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1
        or not lengths[0].isascii()
        or not lengths[0].isdigit()
        or len(lengths[0]) > 4
        or int(lengths[0]) > _MAX_BODY_BYTES
    ):
        raise _invalid_body()
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_BODY_BYTES:
                raise _invalid_body()
            raw.extend(chunk)
    except ClientDisconnect:
        raise _invalid_body() from None
    if not raw and allow_empty:
        return {}
    if not types or not raw:
        raise _invalid_body()
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (ValueError, RecursionError):
        raise _invalid_body() from None


@router.post(
    "/registration-challenges", response_model=DeviceChallengeResponse, status_code=201
)
async def create_challenge(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    service: RegistrationDependency,
) -> DeviceChallengeResponse:
    scope = _native_scope(request, principal, workspace)
    if await _read_bounded_json(request, allow_empty=True) != {}:
        raise _invalid_body()
    try:
        result = await service.issue(scope)
    except DeviceRegistrationRateLimited:
        raise ProblemError(
            status=429,
            code="DEVICE_REGISTRATION_RATE_LIMITED",
            title="Wait before requesting another challenge",
            retryable=True,
            headers={**_PRIVATE_HEADERS, "Retry-After": "10"},
        ) from None
    except DeviceRegistrationRejected:
        raise _rejected() from None
    except DeviceRegistrationUnavailable:
        raise _unavailable() from None
    response.headers.update(_PRIVATE_HEADERS)
    return DeviceChallengeResponse(
        challenge_id=result.challenge_id,
        challenge=result.challenge,
        expires_at=result.expires_at,
    )


@router.post(
    "/register",
    response_model=DeviceRegisterResponse,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": DeviceRegisterRequest.model_json_schema()
                }
            },
        }
    },
)
async def register_device(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    service: RegistrationDependency,
) -> DeviceRegisterResponse:
    scope = _native_scope(request, principal, workspace)
    try:
        body = DeviceRegisterRequest.model_validate(
            await _read_bounded_json(request, allow_empty=False)
        )
        result = await service.complete(
            scope=scope,
            challenge_id=body.challenge_id,
            public_key=body.public_key,
            signature=body.signature,
            platform=body.platform,
            app_version=body.app_version,
            display_name=body.display_name,
        )
    except ValidationError:
        raise _invalid_body() from None
    except DeviceRegistrationRejected:
        raise _rejected() from None
    except DeviceRegistrationUnavailable:
        raise _unavailable() from None
    response.headers.update(_PRIVATE_HEADERS)
    return DeviceRegisterResponse(
        device_id=result.device_id, device_credential=result.device_credential
    )
