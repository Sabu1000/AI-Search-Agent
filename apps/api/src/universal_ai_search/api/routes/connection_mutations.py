"""Manager-authorized writes for bounded, already granted connections."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.backends import CSRF_COOKIE
from universal_ai_search.connections.mutations import (
    ConnectionMutationBusy,
    ConnectionMutationDenied,
    ConnectionMutationError,
    ConnectionMutationKeyConflict,
    ConnectionMutationMissing,
    ConnectionMutationNotSupported,
    ConnectionMutationScope,
    ConnectionMutationStale,
    ConnectionMutationStore,
    ConnectionSelectionInvalid,
    mutation_etag,
)
from universal_ai_search.connections.query import ConnectionResponse

from .connections import _require_csrf

router = APIRouter(prefix="/connections", tags=["connections"])


class SyncRequest(RequestModel):
    """Scope and mode are derived from the server's current durable grant."""


class SelectionsRequest(RequestModel):
    collection_ids: Annotated[list[CanonicalUUID], Field(min_length=1, max_length=500)]


class SyncProgress(ResponseModel):
    completed: Literal[0] = 0
    total: None = None
    unit: Literal["items"] = "items"


class SyncLinks(ResponseModel):
    self: str


class SyncOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["sync"] = "sync"
    status: Literal["pending", "running", "retry_wait"]
    progress: SyncProgress
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    completed_at: None = None
    deadline_at: None = None
    error: None = None
    links: SyncLinks


class SyncResponse(ResponseModel):
    operation: SyncOperation


def _store(request: Request) -> ConnectionMutationStore:
    return cast(ConnectionMutationStore, request.app.state.connection_mutation_store)


StoreDependency = Annotated[ConnectionMutationStore, Depends(_store)]


async def _scope(
    request: Request, principal: Principal, workspace: WorkspaceContext
) -> ConnectionMutationScope:
    if (
        principal.session_id is None
        or principal.mode not in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
        or workspace.user_id != principal.subject_id
    ):
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    if workspace.role not in {"owner", "admin"}:
        raise ProblemError(
            status=403,
            code="CONNECTION_MANAGEMENT_FORBIDDEN",
            title="Connection management requires a workspace manager",
        )
    if principal.mode == AuthenticationMode.BROWSER:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        tokens = request.headers.getlist("X-CSRF-Token")
        if (
            len(tokens) != 1
            or len(request.headers.getlist("Origin")) != 1
            or not tokens[0].isascii()
            or not cookie.isascii()
        ):
            raise ProblemError(
                status=403, code="CSRF_FAILED", title="CSRF validation failed"
            )
    await _require_csrf(request, principal.mode)
    return ConnectionMutationScope(
        workspace.workspace_id,
        principal.subject_id,
        principal.session_id,
        workspace.authorization_version,
        principal.authorization_version,
    )


def _error(error: ConnectionMutationError) -> ProblemError:
    details = {
        ConnectionMutationDenied: (
            403,
            "CONNECTION_MANAGEMENT_FORBIDDEN",
            "Connection management is not allowed",
        ),
        ConnectionMutationMissing: (
            404,
            "CONNECTION_NOT_FOUND",
            "Connection not found",
        ),
        ConnectionMutationBusy: (
            409,
            "IDEMPOTENCY_IN_PROGRESS",
            "Request is already in progress",
        ),
        ConnectionMutationKeyConflict: (
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "Idempotency key was already used",
        ),
        ConnectionMutationNotSupported: (
            409,
            "CONNECTION_MUTATION_UNSUPPORTED",
            "This connection or grant does not support the requested action",
        ),
        ConnectionMutationStale: (
            412,
            "RESOURCE_VERSION_MISMATCH",
            "Connection changed",
        ),
        ConnectionSelectionInvalid: (
            422,
            "CONNECTION_SELECTION_INVALID",
            "Select only currently selected repository collection IDs",
        ),
    }
    status, code, title = details[type(error)]
    busy = isinstance(error, ConnectionMutationBusy)
    return ProblemError(
        status=status,
        code=code,
        title=title,
        retryable=busy,
        headers={"Retry-After": "2"} if busy else None,
    )


def _key(values: list[str] | None) -> str:
    if values and len(values) != 1:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="Supply one idempotency key",
        )
    return validate_idempotency_key(values[0] if values else None)


def _privacy(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie, X-Workspace-ID"


@router.post(
    "/{connection_id}/sync",
    response_model=SyncResponse,
    status_code=202,
    description=(
        "Request sync for a Gmail-only grant or a GitHub connection with one selected "
        "repository. Returns the real queued or in-flight job. Other grants are not "
        "supported by this operation."
    ),
)
async def sync_connection(
    connection_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    body: SyncRequest | None = None,
    idempotency_key: Annotated[
        list[str] | None, Header(alias="Idempotency-Key")
    ] = None,
) -> SyncResponse:
    del body  # An empty body is equivalent; any supplied fields are rejected.
    scope = await _scope(request, principal, workspace)
    try:
        receipt = await store.sync(
            scope=scope,
            connection_id=connection_id,
            idempotency_key=_key(idempotency_key),
            request_id=request_id_for(request),
        )
    except ConnectionMutationError as error:
        raise _error(error) from None
    _privacy(response)
    location = f"/v1/operations/{receipt.id}"
    response.headers["Location"] = location
    return SyncResponse(
        operation=SyncOperation(
            id=receipt.id,
            status=receipt.status,
            progress=SyncProgress(),
            created_at=receipt.created_at,
            started_at=receipt.started_at,
            links=SyncLinks(self=location),
        )
    )


@router.put(
    "/{connection_id}/selections",
    response_model=ConnectionResponse,
    description=(
        "Narrow a GitHub connection to a nonempty subset of its already selected "
        "repository collection IDs. Does not add provider grants or restore "
        "revoked access."
    ),
)
async def update_connection_selections(
    connection_id: UUID,
    request: Request,
    response: Response,
    body: SelectionsRequest,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    if_match: Annotated[list[str] | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[
        list[str] | None, Header(alias="Idempotency-Key")
    ] = None,
) -> ConnectionResponse:
    scope = await _scope(request, principal, workspace)
    if not if_match:
        raise ProblemError(
            status=428, code="IF_MATCH_REQUIRED", title="A connection ETag is required"
        )
    if len(if_match) != 1 or len(if_match[0]) > 96 or not if_match[0].isascii():
        raise _error(ConnectionMutationStale())
    try:
        snapshot = await store.narrow(
            scope=scope,
            connection_id=connection_id,
            collection_ids=tuple(body.collection_ids),
            if_match=if_match[0],
            idempotency_key=_key(idempotency_key),
            request_id=request_id_for(request),
        )
    except ConnectionMutationError as error:
        raise _error(error) from None
    _privacy(response)
    response.headers["ETag"] = mutation_etag(snapshot.body.id, snapshot.lock_version)
    return snapshot.body
