"""Native signing route; requires explicitly verified private staging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from universal_ai_search.api.device_envelope import (
    DeviceBodyTooLarge,
    DeviceEnvelopeError,
)
from universal_ai_search.api.device_upload_authorization import authorize_device_upload
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.upload_signer import VerifiedS3UploadSigner
from universal_ai_search.devices.uploads import (
    DeviceUploadAuthorization,
    UploadQuotaExceeded,
    UploadRateLimited,
    UploadRejected,
    UploadRequestInvalid,
    UploadReservationExpired,
    UploadUnavailable,
)

router = APIRouter(prefix="/devices", tags=["devices"])
_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True, slots=True)
class _UploadDependencies:
    authorizer: DeviceUploadAuthorization
    signer: VerifiedS3UploadSigner


class UploadSignResponse(ResponseModel):
    upload_id: CanonicalUUID
    upload_url: str = Field(repr=False)
    method: Literal["PUT"] = "PUT"
    required_headers: dict[str, str] = Field(repr=False)
    expires_at: UtcDateTime


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DEVICE_UPLOAD_UNAVAILABLE",
        title="Device upload signing unavailable",
        retryable=True,
        headers=_HEADERS,
    )


def _services(request: Request) -> _UploadDependencies:
    authorizer = getattr(request.app.state, "device_upload_authorization", None)
    signer = getattr(request.app.state, "device_upload_signer", None)
    if not isinstance(authorizer, DeviceUploadAuthorization) or not isinstance(
        signer, VerifiedS3UploadSigner
    ):
        raise _unavailable()
    return _UploadDependencies(authorizer, signer)


@router.post("/{device_id}/uploads:sign", response_model=UploadSignResponse)
async def sign_upload(
    device_id: CanonicalUUID,
    request: Request,
    response: Response,
    services: Annotated[_UploadDependencies, Depends(_services)],
) -> UploadSignResponse:
    try:
        await run_in_threadpool(services.signer.require_ready)
        grant = await authorize_device_upload(device_id, request, services.authorizer)
        signed = await run_in_threadpool(services.signer.sign, grant)
    except DeviceBodyTooLarge:
        raise ProblemError(
            status=413,
            code="DEVICE_UPLOAD_BODY_TOO_LARGE",
            title="Upload request too large",
            headers=_HEADERS,
        ) from None
    except (DeviceEnvelopeError, UploadRejected):
        raise ProblemError(
            status=401,
            code="DEVICE_UPLOAD_REJECTED",
            title="Device upload authorization failed",
            headers={**_HEADERS, "WWW-Authenticate": "Device"},
        ) from None
    except UploadRequestInvalid:
        raise ProblemError(
            status=422,
            code="DEVICE_UPLOAD_INVALID",
            title="Invalid device upload request",
            headers=_HEADERS,
        ) from None
    except UploadQuotaExceeded:
        raise ProblemError(
            status=409,
            code="QUOTA_EXCEEDED",
            title="Workspace upload quota exceeded",
            headers=_HEADERS,
        ) from None
    except UploadReservationExpired:
        raise ProblemError(
            status=409,
            code="DEVICE_UPLOAD_EXPIRED",
            title="Upload reservation expired",
            headers=_HEADERS,
        ) from None
    except UploadRateLimited:
        raise ProblemError(
            status=429,
            code="DEVICE_UPLOAD_RATE_LIMITED",
            title="Pause upload signing requests",
            retryable=True,
            headers={**_HEADERS, "Retry-After": "60"},
        ) from None
    except UploadUnavailable:
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return UploadSignResponse(
        upload_id=grant.upload_id,
        upload_url=signed.url,
        required_headers=grant.required_headers(),
        expires_at=signed.expires_at,
    )
