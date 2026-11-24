"""Confirmed device revocation, never a blind connection deletion."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from universal_ai_search.api.auth import AuthenticationMode, PrincipalDependency
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.revocation import (
    CONFIRMATION,
    DeviceCleanupUnsupported,
    DeviceRevocationBusy,
    DeviceRevocationConflict,
    DeviceRevocationReceipt,
    DeviceRevocationRejected,
    DeviceRevocationStore,
    DeviceRevocationUnavailable,
)

from .connections import _require_csrf
from .device_queries import Scope

router = APIRouter(prefix="/devices", tags=["devices"])
_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Origin, Authorization, Cookie, X-Workspace-ID",
}


class DeviceDeletionProgress(ResponseModel):
    completed: Literal[1] = 1
    total: Literal[1] = 1
    unit: Literal["device"] = "device"


class DeviceDeletionLinks(ResponseModel):
    self: str


class DeviceDeletionOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["device_delete"] = "device_delete"
    status: Literal["completed"] = "completed"
    progress: DeviceDeletionProgress
    created_at: UtcDateTime
    started_at: UtcDateTime
    completed_at: UtcDateTime
    deadline_at: UtcDateTime
    error: None = None
    links: DeviceDeletionLinks


class DeviceDeletionResponse(ResponseModel):
    operation: DeviceDeletionOperation


def device_operation(
    receipt: DeviceRevocationReceipt, response: Response
) -> DeviceDeletionResponse:
    response.headers.update(_HEADERS)
    return DeviceDeletionResponse(
        operation=DeviceDeletionOperation(
            id=receipt.id,
            progress=DeviceDeletionProgress(),
            created_at=receipt.created_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.deadline_at,
            links=DeviceDeletionLinks(self=f"/v1/operations/{receipt.id}"),
        )
    )


def _store(request: Request) -> DeviceRevocationStore:
    store = getattr(request.app.state, "device_revocation_store", None)
    if store is None:
        raise _problem(
            503, "DEVICE_REVOCATION_UNAVAILABLE", "Device revocation unavailable"
        )
    return cast(DeviceRevocationStore, store)


Store = Annotated[DeviceRevocationStore, Depends(_store)]


def _problem(
    status: int, code: str, title: str, *, retryable: bool = False
) -> ProblemError:
    return ProblemError(
        status=status,
        code=code,
        title=title,
        retryable=retryable,
        headers={
            **_HEADERS,
            **({"Retry-After": "2"} if status == 409 and retryable else {}),
        },
    )


@router.delete("/{device_id}", status_code=202, response_model=DeviceDeletionResponse)
async def revoke_device(
    device_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    scope: Scope,
    store: Store,
) -> DeviceDeletionResponse:
    # Browser calls require trusted Origin and double-submit CSRF. Bearer
    # calls must not be browser-originated or mix in ambient cookies.
    if principal.mode is AuthenticationMode.BEARER and any(
        name in request.headers
        for name in ("origin", "cookie", "sec-fetch-mode", "sec-fetch-site")
    ):
        raise _problem(
            403, "DEVICE_REVOCATION_FORBIDDEN", "Device revocation is not allowed"
        )
    await _require_csrf(request, principal.mode)
    if request.headers.getlist("x-confirm-action") != [CONFIRMATION]:
        raise _problem(
            400,
            "CONFIRMATION_REQUIRED",
            "Set X-Confirm-Action to revoke-device.",
        )
    values = request.headers.getlist("idempotency-key")
    key = validate_idempotency_key(values[0] if len(values) == 1 else None)
    try:
        receipt = await store.delete(
            scope=scope,
            device_id=device_id,
            idempotency_key=key,
            request_id=request_id_for(request),
        )
    except DeviceRevocationRejected:
        raise _problem(404, "DEVICE_NOT_FOUND", "Device is unavailable") from None
    except DeviceCleanupUnsupported:
        raise _problem(
            409,
            "DEVICE_CLEANUP_UNAVAILABLE",
            "This device needs a cleanup worker before it can be revoked here.",
        ) from None
    except DeviceRevocationConflict:
        raise _problem(
            409, "IDEMPOTENCY_KEY_REUSED", "Idempotency key was already used"
        ) from None
    except DeviceRevocationBusy:
        raise _problem(
            409, "IDEMPOTENCY_IN_PROGRESS", "Revocation is in progress", retryable=True
        ) from None
    except DeviceRevocationUnavailable:
        raise _problem(
            503,
            "DEVICE_REVOCATION_UNAVAILABLE",
            "Device revocation unavailable",
            retryable=True,
        ) from None
    result = device_operation(receipt, response)
    response.headers["Location"] = result.operation.links.self
    return result
