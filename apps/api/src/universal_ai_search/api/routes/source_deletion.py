"""Exact-confirmation route for deleting only an application's inline copy."""

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
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.conversations.deletion import ConversationDeletion
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
)
from universal_ai_search.search.source_deletion import (
    DELETE_SOURCE_CONFIRMATION,
    SourceDeletionStore,
    SourceDeletionUnavailable,
)

from .connections import _require_csrf
from .conversations import _scope
from .source_reindex import SourceOperationLinks, SourceOperationProgress

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceDeletionOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["source_delete"] = "source_delete"
    status: Literal["completed"] = "completed"
    progress: SourceOperationProgress
    created_at: UtcDateTime
    started_at: UtcDateTime
    completed_at: UtcDateTime
    deadline_at: UtcDateTime
    error: None = None
    links: SourceOperationLinks


class SourceDeletionResponse(ResponseModel):
    operation: SourceDeletionOperation


def _store(request: Request) -> SourceDeletionStore:
    return cast(SourceDeletionStore, request.app.state.source_deletion_store)


StoreDependency = Annotated[SourceDeletionStore, Depends(_store)]


def _operation(
    receipt: ConversationDeletion, response: Response
) -> SourceDeletionResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie, X-Workspace-ID"
    return SourceDeletionResponse(
        operation=SourceDeletionOperation(
            id=receipt.id,
            progress=SourceOperationProgress(completed=1),
            created_at=receipt.created_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.deadline_at,
            links=SourceOperationLinks(self=f"/v1/operations/{receipt.id}"),
        )
    )


@router.delete("/{source_id}", response_model=SourceDeletionResponse, status_code=202)
async def delete_source(
    source_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    confirmation: Annotated[list[str] | None, Header(alias="X-Confirm-Action")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SourceDeletionResponse:
    scope = _scope(principal, workspace)
    await _require_csrf(request, principal.mode)
    if confirmation != [DELETE_SOURCE_CONFIRMATION]:
        raise ProblemError(
            status=400,
            code="CONFIRMATION_REQUIRED",
            title="Source deletion needs exact confirmation",
            detail="Set X-Confirm-Action to delete-source.",
        )
    try:
        receipt = await store.delete(
            scope=scope,
            source_id=source_id,
            idempotency_key=validate_idempotency_key(idempotency_key),
            request_id=request_id_for(request),
        )
    except (ConversationDenied, ConversationMissing):
        raise ProblemError(
            status=404, code="SOURCE_NOT_FOUND", title="Source is unavailable"
        ) from None
    except SourceDeletionUnavailable:
        raise ProblemError(
            status=409,
            code="SOURCE_DELETION_UNAVAILABLE",
            title="This indexed copy cannot currently be fully deleted",
        ) from None
    except ConversationBusy:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            title="Request is already in progress",
            retryable=True,
            headers={"Retry-After": "2"},
        ) from None
    except ConversationKeyConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
        ) from None
    result = _operation(receipt, response)
    response.headers["Location"] = result.operation.links.self
    return result


async def read_source_deletion_operation(
    operation_id: UUID,
    response: Response,
    principal: Principal,
    workspace: WorkspaceContext,
    store: SourceDeletionStore,
) -> SourceDeletionResponse | None:
    """Return None for an unknown/foreign receipt; aggregate route provides 404."""
    try:
        receipt = await store.read(
            scope=_scope(principal, workspace), operation_id=operation_id
        )
    except (ConversationDenied, ConversationMissing):
        return None
    return _operation(receipt, response)
