"""Accept a signed one-upload changes batch and return a durable indexing operation."""

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
from universal_ai_search.devices.upload_completion import (
    MAX_CHANGES_BODY_BYTES,
    ChangeConflict,
    ChangeExpired,
    ChangeInvalidObject,
    ChangeRateLimited,
    DeviceUploadCompletion,
    VerifiedChange,
)
from universal_ai_search.devices.upload_index_operation import (
    NativeUploadIndexOperation,
)
from universal_ai_search.devices.uploads import (
    UploadRejected,
    UploadRequestInvalid,
    UploadUnavailable,
)

from .job_operations import (
    JobOperation,
    JobOperationResponse,
    JobProgress,
    SafeJobError,
)

router = APIRouter(prefix="/devices", tags=["devices"])
_HEADERS = {"Cache-Control": "private, no-store"}


def _error(
    status: int,
    code: str,
    title: str,
    retry_after: str | None = None,
) -> ProblemError:
    headers = dict(_HEADERS)
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    if status == 401:
        headers["WWW-Authenticate"] = "Device"
    return ProblemError(
        status=status,
        code=f"DEVICE_CHANGE_{code}",
        title=title,
        headers=headers,
        retryable=retry_after is not None,
    )


def _service(request: Request) -> DeviceUploadCompletion:
    service = getattr(request.app.state, "device_upload_completion", None)
    if service is None:
        raise _error(503, "UNAVAILABLE", "Device changes are unavailable", "20")
    return cast(DeviceUploadCompletion, service)


def change_operation(
    receipt: VerifiedChange, response: Response
) -> JobOperationResponse:
    response.headers.update(_HEADERS)
    return JobOperationResponse(
        operation=JobOperation(
            id=receipt.id,
            type="device_upload_index",
            status="pending",
            progress=JobProgress(completed=0, total=1, unit="indexed_uploads"),
            created_at=receipt.completed_at,
            links={"self": f"/v1/operations/{receipt.id}"},
        )
    )


def index_operation(
    operation: NativeUploadIndexOperation, response: Response
) -> JobOperationResponse:
    response.headers.update(_HEADERS)
    return JobOperationResponse(
        operation=JobOperation(
            id=operation.id,
            type="device_upload_index",
            status=operation.status,
            progress=JobProgress(
                completed=int(operation.status == "completed"),
                total=1,
                unit="indexed_uploads",
            ),
            created_at=operation.created_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            error=(
                SafeJobError(
                    code=operation.error_code,
                    detail="The verified local upload could not be indexed.",
                    retryable=operation.status == "retry_wait",
                    recovery_action=None,
                )
                if operation.error_code is not None
                else None
            ),
            links={"self": f"/v1/operations/{operation.id}"},
        )
    )


@router.post(
    "/{device_id}/changes", status_code=202, response_model=JobOperationResponse
)
async def accept_device_changes(
    device_id: CanonicalUUID,
    request: Request,
    response: Response,
    service: Annotated[DeviceUploadCompletion, Depends(_service)],
) -> JobOperationResponse:
    try:
        envelope = await read_device_envelope(
            request, max_body_bytes=MAX_CHANGES_BODY_BYTES
        )
        receipt = await service.accept(
            device_id=device_id,
            credential=envelope.credential,
            raw_target=envelope.target,
            headers=envelope.headers,
            exact_body=envelope.body,
        )
    except DeviceBodyTooLarge:
        raise _error(
            413, "TOO_LARGE", "Device change batch exceeds the size limit"
        ) from None
    except (DeviceEnvelopeError, UploadRejected):
        raise _error(401, "REJECTED", "Device change authorization failed") from None
    except UploadRequestInvalid:
        raise _error(
            422, "INVALID", "Expected a batch ID and exactly one upload ID"
        ) from None
    except ChangeConflict:
        raise _error(
            409, "CONFLICT", "Device change batch conflicts with current state", "20"
        ) from None
    except ChangeExpired:
        raise _error(409, "EXPIRED", "Device change window expired") from None
    except ChangeInvalidObject:
        raise _error(
            409, "UNVERIFIED", "The staged upload could not be verified", "20"
        ) from None
    except ChangeRateLimited:
        raise _error(
            429, "RATE_LIMITED", "Pause device changes and try again", "60"
        ) from None
    except UploadUnavailable:
        raise _error(
            503, "UNAVAILABLE", "Device changes are unavailable", "20"
        ) from None
    result = change_operation(receipt, response)
    response.headers["Location"] = result.operation.links["self"]
    return result
