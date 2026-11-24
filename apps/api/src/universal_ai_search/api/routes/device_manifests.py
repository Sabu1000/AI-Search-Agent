"""Unwired native manifest metadata endpoint, not an upload transport."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response

from universal_ai_search.api.device_envelope import (
    DeviceBodyTooLarge,
    DeviceEnvelopeError,
    read_device_envelope,
)
from universal_ai_search.api.models import CanonicalUUID
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.manifests import (
    DeviceManifestService,
    ManifestConflict,
    ManifestInvalid,
    ManifestRateLimited,
    ManifestRejected,
    ManifestResponse,
    ManifestUnavailable,
)
from universal_ai_search.devices.security import MAX_SIGNED_BODY_BYTES

router = APIRouter(prefix="/devices", tags=["devices"])
_HEADERS = {"Cache-Control": "private, no-store"}


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DEVICE_MANIFEST_UNAVAILABLE",
        title="Device manifests unavailable",
        retryable=True,
        headers=_HEADERS,
    )


def _service(request: Request) -> DeviceManifestService:
    service = getattr(request.app.state, "device_manifest_service", None)
    if service is None:
        raise _unavailable()
    return cast(DeviceManifestService, service)


@router.post("/{device_id}/manifests", response_model=ManifestResponse)
async def accept_manifest(
    device_id: CanonicalUUID,
    request: Request,
    response: Response,
    service: Annotated[DeviceManifestService, Depends(_service)],
) -> ManifestResponse:
    try:
        envelope = await read_device_envelope(
            request, max_body_bytes=MAX_SIGNED_BODY_BYTES
        )
        result = await service.accept(
            device_id=device_id,
            credential=envelope.credential,
            raw_target=envelope.target,
            headers=envelope.headers,
            exact_body=envelope.body,
        )
    except DeviceBodyTooLarge:
        raise ProblemError(
            status=413,
            code="DEVICE_MANIFEST_TOO_LARGE",
            title="Manifest page too large",
            headers=_HEADERS,
        ) from None
    except (DeviceEnvelopeError, ManifestRejected):
        raise ProblemError(
            status=401,
            code="DEVICE_MANIFEST_REJECTED",
            title="Manifest authorization failed",
            headers={**_HEADERS, "WWW-Authenticate": "Device"},
        ) from None
    except ManifestInvalid:
        raise ProblemError(
            status=422,
            code="DEVICE_MANIFEST_INVALID",
            title="Invalid manifest page",
            headers=_HEADERS,
        ) from None
    except ManifestConflict as error:
        raise ProblemError(
            status=409,
            code="DEVICE_MANIFEST_CONFLICT",
            title="Manifest page conflicts with committed state",
            detail=f"Next expected sequence: {error.next_sequence}.",
            headers={**_HEADERS, "X-Next-Manifest-Sequence": str(error.next_sequence)},
        ) from None
    except ManifestRateLimited:
        raise ProblemError(
            status=429,
            code="DEVICE_MANIFEST_RATE_LIMITED",
            title="Pause manifest requests",
            retryable=True,
            headers={**_HEADERS, "Retry-After": "60"},
        ) from None
    except ManifestUnavailable:
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return result
