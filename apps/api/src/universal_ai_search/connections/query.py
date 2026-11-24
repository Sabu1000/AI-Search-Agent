"""Read-only, tenant-scoped projections for connection settings and status."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime

from .github import READ_PERMISSIONS
from .google import DRIVE_READONLY_SCOPE, GMAIL_READONLY_SCOPE
from .store import _set_context

ConnectionProvider = Literal["google", "github", "local_files"]
ConnectionStatus = Literal[
    "pending", "active", "reauthorization_required", "error", "deleting", "deleted"
]
OperationStatus = Literal[
    "pending", "running", "retry_wait", "completed", "failed", "blocked"
]
RecoveryAction = Literal["reauthorize", "check_status"]
SortKey = tuple[datetime, UUID]

_DETAIL_COLLECTIONS_LIMIT = 500
_LIST_COLLECTIONS_LIMIT = 20
_ALLOWED_SCOPES = sorted(
    {
        GMAIL_READONLY_SCOPE,
        DRIVE_READONLY_SCOPE,
        *(f"{permission}:{level}" for permission, level in READ_PERMISSIONS.items()),
    }
)
# Only fixed application error codes may make it into the settings UI.
_PUBLIC_ERROR_CODES = frozenset(
    {
        "CURSOR_INVALID",
        "DRIVE_CREDENTIAL_INVALID",
        "GITHUB_INSTALLATION_ACCESS_LOST",
        "MAX_ATTEMPTS_EXCEEDED",
        "OAUTH_REAUTHORIZATION_REQUIRED",
        "RATE_LIMIT",
        "RATE_LIMITED",
    }
)
_PROVIDER_PLACEHOLDER = re.compile(r"GitHub repository [0-9]+")


class ConnectionCollection(ResponseModel):
    id: CanonicalUUID
    kind: Literal["repository", "folder", "mailbox"]
    name: str


class ConnectionOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["sync", "reconcile", "connection_delete"]
    status: OperationStatus
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    deadline_at: UtcDateTime | None = None


class ConnectionStatusResponse(ResponseModel):
    id: CanonicalUUID
    status: ConnectionStatus
    last_successful_sync_at: UtcDateTime | None
    current_operation: ConnectionOperation | None
    error_code: str | None
    recovery_action: RecoveryAction | None
    updated_at: UtcDateTime


class ConnectionResponse(ConnectionStatusResponse):
    provider: ConnectionProvider
    display_label: str
    scopes: list[str]
    selected_collections: list[ConnectionCollection]
    selected_collections_truncated: bool
    created_at: UtcDateTime


@dataclass(frozen=True)
class ConnectionSnapshot:
    body: ConnectionResponse
    lock_version: int


class ConnectionQueryReader(Protocol):
    async def list_connections(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        provider: ConnectionProvider | None,
        before: SortKey | None,
        limit: int,
    ) -> list[ConnectionSnapshot]: ...

    async def get(
        self, *, workspace_id: UUID, user_id: UUID, connection_id: UUID
    ) -> ConnectionSnapshot | None: ...


# Deliberate column allowlist. No connection secrets, installation identifiers,
# collection provider IDs, deletion receipt metadata, or job payloads are read.
_BASE = """
SELECT c.id, c.provider, c.display_label, c.status, c.created_at, c.updated_at,
       c.last_successful_sync_at, c.last_error_code, c.lock_version,
       ARRAY(SELECT s.scope FROM app.connection_scopes AS s
             WHERE s.workspace_id = c.workspace_id AND s.connection_id = c.id
               AND s.scope = ANY(CAST(:allowed_scopes AS text[]))
             ORDER BY s.scope) AS scopes,
       COALESCE(
         (SELECT jsonb_agg(jsonb_build_object(
                     'id', a.id, 'kind', a.kind, 'name', a.name)
                  ORDER BY a.name, a.id)
          FROM (
              SELECT sc.id, sc.kind, sc.name
              FROM app.source_collections AS sc
              WHERE sc.workspace_id = c.workspace_id AND sc.connection_id = c.id
                AND sc.selected AND sc.status = 'active'
              ORDER BY sc.name, sc.id
              LIMIT :collection_limit
          ) AS a),
         '[]'::jsonb
       ) AS selected_collections,
       (SELECT jsonb_build_object(
                   'id', d.id, 'type', 'connection_delete', 'status', d.status,
                   'created_at', d.requested_at, 'started_at', d.started_at,
                   'completed_at', d.completed_at, 'deadline_at', d.deadline_at)
        FROM app.deletion_requests AS d
        WHERE d.workspace_id = c.workspace_id AND d.target_id = c.id
          AND d.target_type = 'connection'
          AND c.status IN ('deleting', 'deleted')
        ORDER BY d.requested_at DESC, d.id DESC LIMIT 1) AS deletion,
       (SELECT jsonb_build_object(
                   'id', j.id, 'type', j.job_type,
                   'status', CASE j.status WHEN 'leased' THEN 'running'
                                           ELSE j.status END,
                   'created_at', j.created_at)
        FROM app.jobs AS j
        WHERE j.workspace_id = c.workspace_id AND j.connection_id = c.id
          AND j.job_type IN ('sync', 'reconcile')
          AND j.status IN ('pending', 'leased', 'retry_wait')
          AND c.status NOT IN ('deleting', 'deleted')
        ORDER BY j.created_at DESC, j.id DESC LIMIT 1) AS sync
FROM app.connections AS c
WHERE c.workspace_id = :workspace_id
"""


class SQLAlchemyConnectionQueryReader:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_connections(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        provider: ConnectionProvider | None,
        before: SortKey | None,
        limit: int,
    ) -> list[ConnectionSnapshot]:
        if not 1 <= limit <= 101:
            raise ValueError("connection query bound is invalid")
        # SQL fragments are chosen exclusively by local branches, never caller text.
        query = _BASE + " AND c.status <> 'deleted'"
        params: dict[str, object] = {
            "workspace_id": workspace_id,
            "allowed_scopes": _ALLOWED_SCOPES,
            "collection_limit": _LIST_COLLECTIONS_LIMIT + 1,
            "row_limit": limit,
        }
        if provider is not None:
            query += " AND c.provider = :provider"
            params["provider"] = provider
        if before is not None:
            query += " AND (c.created_at, c.id) < (:before_created_at, :before_id)"
            params["before_created_at"], params["before_id"] = before
        query += " ORDER BY c.created_at DESC, c.id DESC LIMIT :row_limit"
        async with self._engine.begin() as conn:
            await _set_context(conn, workspace_id=workspace_id, user_id=user_id)
            rows = (await conn.execute(text(query), params)).mappings().all()
        return [_snapshot(row, _LIST_COLLECTIONS_LIMIT) for row in rows]

    async def get(
        self, *, workspace_id: UUID, user_id: UUID, connection_id: UUID
    ) -> ConnectionSnapshot | None:
        query = _BASE + " AND c.id = :connection_id"
        async with self._engine.begin() as conn:
            await _set_context(conn, workspace_id=workspace_id, user_id=user_id)
            result = await conn.execute(
                text(query),
                {
                    "workspace_id": workspace_id,
                    "connection_id": connection_id,
                    "allowed_scopes": _ALLOWED_SCOPES,
                    "collection_limit": _DETAIL_COLLECTIONS_LIMIT + 1,
                },
            )
            row = result.mappings().one_or_none()
        return _snapshot(row, _DETAIL_COLLECTIONS_LIMIT) if row is not None else None


def _snapshot(row: RowMapping, collection_limit: int) -> ConnectionSnapshot:
    operation = row["deletion"] or row["sync"]
    status = row["status"]
    public_error = row["last_error_code"]
    if public_error is not None and public_error not in _PUBLIC_ERROR_CODES:
        public_error = "CONNECTION_NEEDS_ATTENTION"
    recovery: RecoveryAction | None = None
    if status == "reauthorization_required":
        recovery = "reauthorize"
    elif public_error is not None or status == "error":
        recovery = "check_status"

    # Removed connections can expose deletion progress, not prior provider labels,
    # scopes or repository selections.
    is_removed = status in {"deleting", "deleted"}
    selections = [] if is_removed else row["selected_collections"]
    collections: list[ConnectionCollection] = []
    for selection in selections[:collection_limit]:
        if row["provider"] == "local_files":
            # Do not expose filesystem labels originating outside this service.
            selection = {**selection, "name": "Local folder"}
        elif row["provider"] == "github" and _PROVIDER_PLACEHOLDER.fullmatch(
            selection["name"]
        ):
            selection = {**selection, "name": "GitHub repository"}
        collections.append(ConnectionCollection.model_validate(selection))

    return ConnectionSnapshot(
        body=ConnectionResponse(
            id=row["id"],
            provider=row["provider"],
            display_label=(
                {"google": "Google", "github": "GitHub", "local_files": "Local files"}[
                    row["provider"]
                ]
                if is_removed or row["provider"] == "local_files"
                else row["display_label"]
            ),
            status=status,
            scopes=[] if is_removed else row["scopes"],
            selected_collections=collections,
            selected_collections_truncated=len(selections) > collection_limit,
            last_successful_sync_at=row["last_successful_sync_at"],
            current_operation=(
                ConnectionOperation.model_validate(operation) if operation else None
            ),
            error_code=public_error,
            recovery_action=recovery,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        ),
        lock_version=row["lock_version"],
    )
