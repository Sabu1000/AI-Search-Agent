"""Authenticated GET endpoints for safe workspace connection metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.pagination import (
    CursorContext,
    CursorSigner,
    Page,
    PageMetadata,
    PaginationQuery,
    normalized_context_hash,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.connections.query import (
    ConnectionProvider,
    ConnectionQueryReader,
    ConnectionResponse,
    ConnectionSnapshot,
    ConnectionStatusResponse,
    SortKey,
)

router = APIRouter(prefix="/connections", tags=["connections"])
_END_POINT = "GET /v1/connections"
_SORT = ["created_at:desc", "id:desc"]
_NO_STORE = "private, no-store"


class ConnectionPageQuery(PaginationQuery):
    provider: ConnectionProvider | None = None


def _reader(request: Request) -> ConnectionQueryReader:
    return cast(ConnectionQueryReader, request.app.state.connection_query_reader)


def _signer(request: Request) -> CursorSigner:
    return cast(CursorSigner, request.app.state.connection_cursor_signer)


def _session_workspace(
    principal: PrincipalDependency, workspace: WorkspaceDependency
) -> WorkspaceContext:
    if (
        principal.session_id is None
        or principal.mode not in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
        or workspace.user_id != principal.subject_id
    ):
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    return workspace


SessionWorkspaceDependency = Annotated[WorkspaceContext, Depends(_session_workspace)]
ReaderDependency = Annotated[ConnectionQueryReader, Depends(_reader)]
SignerDependency = Annotated[CursorSigner, Depends(_signer)]


def _context(
    workspace: WorkspaceContext, provider: ConnectionProvider | None
) -> CursorContext:
    return CursorContext(
        endpoint=_END_POINT,
        workspace_id=workspace.workspace_id,
        principal_id=workspace.user_id,
        filter_sort_hash=normalized_context_hash(
            filters={
                "provider": provider,
                "authorization_version": workspace.authorization_version,
            },
            sort=_SORT,
        ),
    )


def _decode_sort(values: list[str | int | float | bool | None]) -> SortKey:
    try:
        if len(values) != 2 or not all(isinstance(value, str) for value in values):
            raise ValueError
        timestamp = datetime.fromisoformat(cast(str, values[0]))
        opaque_id = UUID(cast(str, values[1]))
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, opaque_id
    except ValueError:
        raise ProblemError(
            status=400, code="CURSOR_INVALID", title="Cursor is invalid"
        ) from None


@router.get("", response_model=Page[ConnectionResponse])
async def list_connections(
    request: Annotated[ConnectionPageQuery, Query()],
    workspace: SessionWorkspaceDependency,
    reader: ReaderDependency,
    signer: SignerDependency,
    response: Response,
) -> Page[ConnectionResponse]:
    context = _context(workspace, request.provider)
    before = None
    if request.cursor is not None:
        payload = signer.verify(request.cursor, expected=context)
        before = _decode_sort(payload.sort_key)
    snapshots = await reader.list_connections(
        workspace_id=workspace.workspace_id,
        user_id=workspace.user_id,
        provider=request.provider,
        before=before,
        limit=request.limit + 1,
    )
    entries = snapshots[: request.limit]
    next_cursor = None
    has_more = len(snapshots) > request.limit
    if has_more:
        last = entries[-1].body
        next_cursor = signer.issue(
            context=context,
            sort_key=[last.created_at.isoformat(), str(last.id)],
        )
    response.headers["Cache-Control"] = _NO_STORE
    return Page[ConnectionResponse](
        items=[entry.body for entry in entries],
        page=PageMetadata(has_more=has_more, next_cursor=next_cursor),
    )


async def _get(
    reader: ConnectionQueryReader, workspace: WorkspaceContext, connection_id: UUID
) -> ConnectionSnapshot:
    result = await reader.get(
        workspace_id=workspace.workspace_id,
        user_id=workspace.user_id,
        connection_id=connection_id,
    )
    if result is None:
        raise ProblemError(
            status=404,
            code="CONNECTION_NOT_FOUND",
            title="Connection not found",
            headers={"Cache-Control": _NO_STORE},
        )
    return result


def connection_etag(connection_id: UUID, lock_version: int) -> str:
    """Opaque contract for future authorized If-Match writes, not authority."""
    return f'"conn-{connection_id.hex}-{lock_version:x}"'


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: UUID,
    workspace: SessionWorkspaceDependency,
    reader: ReaderDependency,
    response: Response,
) -> ConnectionResponse:
    snapshot = await _get(reader, workspace, connection_id)
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["ETag"] = connection_etag(snapshot.body.id, snapshot.lock_version)
    return snapshot.body


@router.get("/{connection_id}/status", response_model=ConnectionStatusResponse)
async def get_connection_status(
    connection_id: UUID,
    workspace: SessionWorkspaceDependency,
    reader: ReaderDependency,
    response: Response,
) -> ConnectionStatusResponse:
    snapshot = await _get(reader, workspace, connection_id)
    body = snapshot.body
    response.headers["Cache-Control"] = _NO_STORE
    return ConnectionStatusResponse(
        id=body.id,
        status=body.status,
        last_successful_sync_at=body.last_successful_sync_at,
        current_operation=body.current_operation,
        error_code=body.error_code,
        recovery_action=body.recovery_action,
        updated_at=body.updated_at,
    )
