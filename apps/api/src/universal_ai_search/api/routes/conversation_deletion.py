"""Owner-private conversation deletion and its completed operation receipt."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from universal_ai_search.api.auth import PrincipalDependency, WorkspaceDependency
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.conversations.deletion import (
    DELETE_CONFIRMATION,
    ConversationDeletion,
    ConversationDeletionStore,
)
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
)

from .connections import _require_csrf
from .conversations import _scope, _unavailable
from .job_operations import JobOperationResponse, get_job_operation

router = APIRouter(prefix="/conversations", tags=["conversations"])
operations_router = APIRouter(prefix="/operations", tags=["operations"])


class DeletionProgress(ResponseModel):
    completed: int = 1
    total: int = 1
    unit: Literal["conversation"] = "conversation"


class DeletionLinks(ResponseModel):
    self: str


class DeletionOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["conversation_delete"] = "conversation_delete"
    status: Literal["completed"] = "completed"
    progress: DeletionProgress
    created_at: UtcDateTime
    started_at: UtcDateTime
    completed_at: UtcDateTime
    deadline_at: UtcDateTime
    error: None = None
    links: DeletionLinks


class DeletionResponse(ResponseModel):
    operation: DeletionOperation


def _store(request: Request) -> ConversationDeletionStore:
    return cast(
        ConversationDeletionStore, request.app.state.conversation_deletion_store
    )


StoreDependency = Annotated[ConversationDeletionStore, Depends(_store)]


def _operation(receipt: ConversationDeletion, response: Response) -> DeletionResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie, X-Workspace-ID"
    return DeletionResponse(
        operation=DeletionOperation(
            id=receipt.id,
            progress=DeletionProgress(),
            created_at=receipt.created_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.deadline_at,
            links=DeletionLinks(self=f"/v1/operations/{receipt.id}"),
        )
    )


@router.delete("/{conversation_id}", response_model=DeletionResponse, status_code=202)
async def delete_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    confirmation: Annotated[list[str] | None, Header(alias="X-Confirm-Action")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DeletionResponse:
    scope = _scope(principal, workspace)
    await _require_csrf(request, principal.mode)
    if confirmation != [DELETE_CONFIRMATION]:
        raise ProblemError(
            status=400,
            code="CONFIRMATION_REQUIRED",
            title="Conversation deletion needs exact confirmation",
            detail="Set X-Confirm-Action to delete-conversation.",
        )
    try:
        receipt = await store.delete(
            scope=scope,
            conversation_id=conversation_id,
            idempotency_key=validate_idempotency_key(idempotency_key),
            request_id=request_id_for(request),
        )
    except (ConversationDenied, ConversationMissing):
        raise _unavailable() from None
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


@operations_router.get(
    "/{operation_id}", response_model=DeletionResponse | JobOperationResponse
)
async def read_conversation_deletion_operation(
    operation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
) -> DeletionResponse | JobOperationResponse:
    scope = _scope(principal, workspace)
    try:
        receipt = await store.read(scope=scope, receipt_id=operation_id)
    except ConversationDenied:
        raise ProblemError(
            status=404, code="OPERATION_NOT_FOUND", title="Operation is unavailable"
        ) from None
    except ConversationMissing:
        return await get_job_operation(
            request, response, operation_id, workspace.workspace_id, workspace.user_id
        )
    return _operation(receipt, response)
