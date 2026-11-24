"""Owner-private conversation deletion and its completed operation receipt."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
    WorkspaceDependency,
)
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.local_account_export import (
    ExportMissing,
    LocalAccountExportStore,
)
from universal_ai_search.connections.disconnect import (
    ConnectionDisconnectStore,
    DisconnectMissing,
)
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
from universal_ai_search.devices.registration import RegistrationScope
from universal_ai_search.devices.revocation import (
    DeviceRevocationRejected,
    DeviceRevocationStore,
    DeviceRevocationUnavailable,
)
from universal_ai_search.search.source_deletion import SourceDeletionStore
from universal_ai_search.search.source_reindex import SourceReindexStore

from .connection_disconnect import (
    DisconnectResponse,
    completed_response,
    manager_scope,
)
from .connections import _require_csrf
from .conversations import _scope, _unavailable
from .device_revocation import DeviceDeletionResponse, device_operation
from .job_operations import JobOperationResponse, get_job_operation
from .local_account_export import AccountExportResponse, export_operation, export_scope
from .source_deletion import SourceDeletionResponse, read_source_deletion_operation
from .source_reindex import SourceOperationResponse, read_source_reindex_operation

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
    "/{operation_id}",
    response_model=DeletionResponse
    | JobOperationResponse
    | DeviceDeletionResponse
    | DisconnectResponse
    | SourceOperationResponse
    | SourceDeletionResponse
    | AccountExportResponse,
)
async def read_conversation_deletion_operation(
    operation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
) -> (
    DeletionResponse
    | JobOperationResponse
    | DeviceDeletionResponse
    | DisconnectResponse
    | SourceOperationResponse
    | SourceDeletionResponse
    | AccountExportResponse
):
    scope = _scope(principal, workspace)
    try:
        receipt = await store.read(scope=scope, receipt_id=operation_id)
    except ConversationDenied:
        raise ProblemError(
            status=404, code="OPERATION_NOT_FOUND", title="Operation is unavailable"
        ) from None
    except ConversationMissing:
        devices = cast(DeviceRevocationStore, request.app.state.device_revocation_store)
        try:
            device = await devices.read(
                scope=RegistrationScope(
                    scope.workspace_id,
                    scope.user_id,
                    scope.session_id,
                    scope.user_version,
                    scope.workspace_version,
                ),
                receipt_id=operation_id,
            )
        except DeviceRevocationRejected:
            pass
        except DeviceRevocationUnavailable:
            raise _unavailable() from None
        else:
            return device_operation(device, response)
        disconnect_scope = manager_scope(principal, workspace)
        if disconnect_scope is not None:
            disconnect_store = cast(
                ConnectionDisconnectStore, request.app.state.connection_disconnect_store
            )
            try:
                disconnected = await disconnect_store.read(
                    scope=disconnect_scope,
                    receipt_id=operation_id,
                )
            except DisconnectMissing:
                pass
            else:
                if disconnected is not None and disconnected.status == "completed":
                    response.headers["Cache-Control"] = "private, no-store"
                    return completed_response(disconnected)
        reindex = await read_source_reindex_operation(
            operation_id,
            response,
            principal,
            workspace,
            cast(SourceReindexStore, request.app.state.source_reindex_store),
        )
        if reindex is not None:
            return reindex
        source_deletion_store = getattr(
            request.app.state, "source_deletion_store", None
        )
        if isinstance(source_deletion_store, SourceDeletionStore):
            deleted = await read_source_deletion_operation(
                operation_id, response, principal, workspace, source_deletion_store
            )
            if deleted is not None:
                return deleted
        export_store = getattr(request.app.state, "local_account_export_store", None)
        if (
            isinstance(export_store, LocalAccountExportStore)
            and workspace.role == "owner"
            and principal.mode
            in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
            and principal.session_id is not None
            and principal.subject_id == workspace.user_id
        ):
            try:
                export = await export_store.read(
                    scope=export_scope(principal, workspace), operation_id=operation_id
                )
            except ExportMissing:
                pass
            else:
                return export_operation(export, response)
        return await get_job_operation(
            request, response, operation_id, workspace.workspace_id, workspace.user_id
        )
    return _operation(receipt, response)
