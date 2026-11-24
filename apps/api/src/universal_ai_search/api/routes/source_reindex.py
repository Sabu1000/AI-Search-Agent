"""Request an asynchronous rebuild of a current authorized source."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from universal_ai_search.api.auth import (
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.search.source_reindex import (
    SourceReindexBusy,
    SourceReindexConflict,
    SourceReindexMissing,
    SourceReindexReceipt,
    SourceReindexStore,
    SourceReindexUnavailable,
)

from .connections import _require_csrf
from .conversations import _scope

router = APIRouter(prefix="/sources", tags=["sources"])

type OperationStatus = Literal[
    "pending", "running", "retry_wait", "completed", "failed"
]

_KNOWN_ERRORS = {
    "AUTHORIZATION_CHANGED": "Source access or contents changed before reindexing.",
    "CHUNK_LIMIT_EXCEEDED": "The source exceeded the indexing size limit.",
    "EMBEDDING_INVALID": "The generated index could not be validated.",
    "INDEXING_INTERNAL_ERROR": "The source could not be reindexed.",
    "INDEX_PROMOTION_CONFLICT": "The source changed while reindexing.",
    "MAX_ATTEMPTS_EXCEEDED": "Reindexing reached the retry limit.",
    "NO_INDEXABLE_TEXT": "The source has no indexable text.",
    "SOURCE_DELETED": "The source is no longer indexed.",
    "UNSUPPORTED_MEDIA_TYPE": "The source format is not supported.",
}


class SourceOperationProgress(ResponseModel):
    completed: int
    total: int = 1
    unit: Literal["source"] = "source"


class SourceOperationError(ResponseModel):
    code: str
    detail: str
    retryable: bool
    recovery_action: str


class SourceOperationLinks(ResponseModel):
    self: str


class SourceOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["source_reindex"] = "source_reindex"
    status: OperationStatus
    progress: SourceOperationProgress
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    completed_at: UtcDateTime | None
    deadline_at: UtcDateTime
    error: SourceOperationError | None
    links: SourceOperationLinks


class SourceOperationResponse(ResponseModel):
    operation: SourceOperation


def _store(request: Request) -> SourceReindexStore:
    return cast(SourceReindexStore, request.app.state.source_reindex_store)


StoreDependency = Annotated[SourceReindexStore, Depends(_store)]


def _operation(
    receipt: SourceReindexReceipt, response: Response
) -> SourceOperationResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie, X-Workspace-ID"
    status: OperationStatus
    if receipt.status == "completed":
        status = "completed"
    elif receipt.status == "leased":
        status = "running"
    elif receipt.status == "pending":
        status = "pending"
    elif receipt.status == "retry_wait":
        status = "retry_wait"
    else:
        status = "failed"
    error = None
    if status in {"failed", "retry_wait"}:
        code = receipt.error_code or ""
        if code not in _KNOWN_ERRORS:
            code = "INDEXING_INTERNAL_ERROR"
        error = SourceOperationError(
            code=code,
            detail=_KNOWN_ERRORS[code],
            retryable=status == "retry_wait",
            recovery_action=(
                "Wait for the scheduled retry."
                if status == "retry_wait"
                else "Check the source before requesting a new reindex."
            ),
        )
    return SourceOperationResponse(
        operation=SourceOperation(
            id=receipt.id,
            status=status,
            progress=SourceOperationProgress(completed=int(status == "completed")),
            created_at=receipt.created_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.deadline_at,
            error=error,
            links=SourceOperationLinks(self=f"/v1/operations/{receipt.id}"),
        )
    )


@router.post(
    "/{source_id}/reindex", response_model=SourceOperationResponse, status_code=202
)
async def reindex_source(
    source_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SourceOperationResponse:
    scope = _scope(principal, workspace)
    await _require_csrf(request, principal.mode)
    try:
        receipt = await store.request(
            scope=scope,
            source_id=source_id,
            idempotency_key=validate_idempotency_key(idempotency_key),
        )
    except SourceReindexMissing:
        raise ProblemError(
            status=404, code="SOURCE_NOT_FOUND", title="Source is unavailable"
        ) from None
    except SourceReindexUnavailable:
        raise ProblemError(
            status=409,
            code="SOURCE_REINDEX_UNAVAILABLE",
            title="This source cannot currently be reindexed",
        ) from None
    except SourceReindexConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
        ) from None
    except SourceReindexBusy:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            title="Request is already in progress",
            retryable=True,
            headers={"Retry-After": "2"},
        ) from None
    result = _operation(receipt, response)
    response.headers["Location"] = result.operation.links.self
    return result


async def read_source_reindex_operation(
    operation_id: UUID,
    response: Response,
    principal: Principal,
    workspace: WorkspaceContext,
    store: SourceReindexStore,
) -> SourceOperationResponse | None:
    """Return None for an unknown/foreign receipt; aggregate route provides 404."""
    try:
        receipt = await store.read(
            scope=_scope(principal, workspace), operation_id=operation_id
        )
    except SourceReindexMissing:
        return None
    return _operation(receipt, response)
