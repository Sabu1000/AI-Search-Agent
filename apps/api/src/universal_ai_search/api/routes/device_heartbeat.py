"""Unwired native-only endpoint; requires trusted TLS and canonical ingress."""

from __future__ import annotations

import json
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from universal_ai_search.api.device_envelope import (
    DeviceEnvelopeError,
    read_device_envelope,
)
from universal_ai_search.api.models import CanonicalUUID, RequestModel
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.heartbeat import (
    DeviceHeartbeatRateLimited,
    DeviceHeartbeatRejected,
    DeviceHeartbeatService,
    DeviceHeartbeatUnavailable,
)

from .device_registration import _unique_json_object

router = APIRouter(prefix="/devices", tags=["devices"])
_HEADERS = {"Cache-Control": "private, no-store"}


def _rejected() -> ProblemError:
    return ProblemError(
        status=401,
        code="DEVICE_HEARTBEAT_REJECTED",
        title="Device heartbeat could not be authorized",
        headers={**_HEADERS, "WWW-Authenticate": "Device"},
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DEVICE_HEARTBEAT_UNAVAILABLE",
        title="Device heartbeat is unavailable",
        retryable=True,
        headers=_HEADERS,
    )


def _service(request: Request) -> DeviceHeartbeatService:
    service = getattr(request.app.state, "device_heartbeat_service", None)
    if service is None:
        raise _unavailable()
    return cast(DeviceHeartbeatService, service)


class _QueueState(RequestModel):
    pending: int = Field(strict=True, ge=0, le=100000)
    failed: int = Field(strict=True, ge=0, le=100000)


class _HeartbeatMetadata(RequestModel):
    app_version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,50}$")
    platform: Literal["macos", "windows"]
    queue: _QueueState


@router.post("/{device_id}/heartbeat", status_code=204)
async def heartbeat(
    device_id: CanonicalUUID,
    request: Request,
    service: Annotated[DeviceHeartbeatService, Depends(_service)],
) -> Response:
    try:
        envelope = await read_device_envelope(
            request, max_body_bytes=1024, allow_empty=True
        )
        if envelope.body:
            _HeartbeatMetadata.model_validate(
                json.loads(
                    envelope.body.decode("utf-8"), object_pairs_hook=_unique_json_object
                )
            )
        await service.accept(
            device_id=device_id,
            credential=envelope.credential,
            raw_target=envelope.target,
            headers=envelope.headers,
            exact_body=envelope.body,
        )
    except (DeviceEnvelopeError, DeviceHeartbeatRejected, ValueError, RecursionError):
        raise _rejected() from None
    except DeviceHeartbeatRateLimited:
        raise ProblemError(
            status=429,
            code="DEVICE_HEARTBEAT_RATE_LIMITED",
            title="Wait before sending another heartbeat",
            retryable=True,
            headers={**_HEADERS, "Retry-After": "10"},
        ) from None
    except DeviceHeartbeatUnavailable:
        raise _unavailable() from None
    return Response(status_code=204, headers=_HEADERS)
