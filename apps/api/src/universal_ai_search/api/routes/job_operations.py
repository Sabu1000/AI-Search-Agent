"""Workspace-owned job reads and explicit bounded sync retries."""

from __future__ import annotations

import hmac
from typing import Annotated, Literal, cast
from uuid import UUID, uuid5

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.auth import PrincipalDependency, WorkspaceDependency
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError

from .connections import _require_csrf

router = APIRouter(prefix="/operations", tags=["operations"])

_RETRYABLE = frozenset(
    {
        "RATE_LIMITED",
        "PROVIDER_UNAVAILABLE",
        "MAX_ATTEMPTS_EXCEEDED",
        "GMAIL_SYNC_INTERNAL_ERROR",
    }
)
_STATUS = {
    "pending": "pending",
    "leased": "running",
    "retry_wait": "retry_wait",
    "completed": "completed",
    "failed": "failed",
    "dead_letter": "failed",
}


class JobProgress(ResponseModel):
    completed: int
    total: int | None = None
    unit: str = "items"


class SafeJobError(ResponseModel):
    code: str
    detail: str
    retryable: bool
    recovery_action: str | None


class JobOperation(ResponseModel):
    id: CanonicalUUID
    type: str
    status: str
    progress: JobProgress
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    deadline_at: UtcDateTime | None = None
    error: SafeJobError | None = None
    links: dict[Literal["self", "previous"], str]


class JobOperationResponse(ResponseModel):
    operation: JobOperation


def _not_found() -> ProblemError:
    return ProblemError(
        status=404, code="OPERATION_NOT_FOUND", title="Operation is unavailable"
    )


async def _set_scope(conn: AsyncConnection, workspace_id: UUID, actor_id: UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.user_id', :id, true)"), {"id": str(actor_id)}
    )
    await conn.execute(
        text("SELECT set_config('app.workspace_id', :id, true)"),
        {"id": str(workspace_id)},
    )


async def _job(
    conn: AsyncConnection,
    operation_id: UUID,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    lock: bool,
) -> dict[str, object] | None:
    sql = """
        SELECT j.id,j.job_type,j.status,j.created_at,j.completed_at,j.error_code,
               j.idempotency_key,j.connection_id,c.provider AS provider,
               c.status AS connection_status,j.payload ->> 'mode' AS mode,
               j.payload ->> 'collection_id' AS collection,
               j.payload ->> 'previous_operation_id' AS previous,
               (SELECT min(a.started_at) FROM app.job_attempts a
                 WHERE a.workspace_id=j.workspace_id AND a.job_id=j.id) AS started_at
        FROM app.jobs j
        JOIN app.connections c ON c.workspace_id=j.workspace_id AND c.id=j.connection_id
        JOIN app.workspaces w ON w.id=j.workspace_id AND w.status='active'
        WHERE j.id=:id AND j.workspace_id=:workspace AND c.owner_user_id=:actor
          AND j.job_type IN ('sync','index')
          AND j.payload ->> 'source_reindex_actor' IS NULL
    """
    if lock:
        sql += " FOR UPDATE OF j, c"
    row = (
        (
            await conn.execute(
                text(sql),
                {"id": operation_id, "workspace": workspace_id, "actor": actor_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _retryable(row: dict[str, object]) -> bool:
    return (
        row["status"] in {"failed", "dead_letter"}
        and row["job_type"] == "sync"
        and row["error_code"] in _RETRYABLE
        and row["connection_status"] in {"pending", "active"}
        and (
            (row["provider"] == "github" and row["mode"] == "github_repository")
            or (row["provider"] == "google" and row["mode"] in {"full", "incremental"})
        )
    )


def _response(row: dict[str, object], response: Response) -> JobOperationResponse:
    response.headers["Cache-Control"] = "private, no-store"
    current = _STATUS[str(row["status"])]
    operation_id = str(row["id"])
    safe_error = None
    if current in {"failed", "retry_wait"}:
        retry = _retryable(row)
        safe_error = SafeJobError(
            code="OPERATION_FAILED" if current == "failed" else "RETRY_PENDING",
            detail="The operation could not finish.",
            retryable=retry,
            recovery_action="retry" if retry else None,
        )
    links: dict[Literal["self", "previous"], str] = {
        "self": f"/v1/operations/{operation_id}"
    }
    if row["previous"] is not None:
        links["previous"] = f"/v1/operations/{row['previous']}"
    return JobOperationResponse(
        operation=JobOperation.model_validate(
            {
                "id": row["id"],
                "type": (
                    "connection_sync" if row["job_type"] == "sync" else "source_index"
                ),
                "status": current,
                "progress": JobProgress(completed=1 if current == "completed" else 0),
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "error": safe_error,
                "links": links,
            }
        )
    )


async def get_job_operation(
    request: Request,
    response: Response,
    operation_id: UUID,
    workspace_id: UUID,
    actor_id: UUID,
) -> JobOperationResponse:
    engine = cast(AsyncEngine, request.app.state.database_engine)
    async with engine.begin() as conn:
        await _set_scope(conn, workspace_id, actor_id)
        row = await _job(conn, operation_id, workspace_id, actor_id, lock=False)
    if row is None:
        raise _not_found()
    return _response(row, response)


@router.post(
    "/{operation_id}:retry", response_model=JobOperationResponse, status_code=202
)
async def retry_operation(
    operation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobOperationResponse:
    if principal.session_id is None or principal.subject_id != workspace.user_id:
        raise _not_found()
    await _require_csrf(request, principal.mode)
    key = validate_idempotency_key(idempotency_key)
    secret = cast(bytes, request.app.state.conversation_idempotency_key)
    actor_id = workspace.user_id
    workspace_id = workspace.workspace_id
    replacement = uuid5(operation_id, "manual-sync-retry")
    key_hash = hmac.digest(
        secret, f"operation-retry:{actor_id}:{key}".encode(), "sha256"
    )
    stored_key = f"manual-retry:{replacement}:{key_hash.hex()}"
    engine = cast(AsyncEngine, request.app.state.database_engine)
    async with engine.begin() as conn:
        await _set_scope(conn, workspace_id, actor_id)
        prior = await _job(conn, operation_id, workspace_id, actor_id, lock=True)
        if prior is None:
            raise _not_found()
        existing = await _job(conn, replacement, workspace_id, actor_id, lock=False)
        if existing is not None:
            if existing["idempotency_key"] != stored_key:
                raise ProblemError(
                    status=409,
                    code="OPERATION_ALREADY_RETRIED",
                    title="This operation already has a replacement",
                )
            row = existing
        else:
            if not _retryable(prior):
                raise ProblemError(
                    status=409,
                    code="OPERATION_NOT_RETRYABLE",
                    title="This operation cannot be retried",
                )
            # Copy only a known sync mode from an authorized durable job. A
            # provider worker still verifies its current scopes before fetching.
            if prior["provider"] == "github":
                payload_expr = """jsonb_build_object(
                    'mode','github_repository','collection_id',
                    CAST(:collection AS TEXT),
                    'previous_operation_id',CAST(:previous AS TEXT))"""
                selected = await conn.scalar(
                    text(
                        """SELECT 1 FROM app.source_collections
                           WHERE workspace_id=:workspace
                             AND connection_id=:connection AND id::TEXT=:collection
                             AND kind='repository' AND selected AND status='active'"""
                    ),
                    {
                        "workspace": workspace_id,
                        "connection": prior["connection_id"],
                        "collection": prior["collection"],
                    },
                )
                if selected != 1:
                    raise _not_found()
            else:
                payload_expr = """jsonb_build_object('mode','full','source_families',
                    jsonb_build_array('gmail'),'previous_operation_id',
                    CAST(:previous AS TEXT))"""
                scopes: list[str] = list(
                    (
                        await conn.execute(
                            text(
                                """SELECT scope FROM app.connection_scopes
                               WHERE workspace_id=:workspace
                            AND connection_id=:connection"""
                            ),
                            {
                                "workspace": workspace_id,
                                "connection": prior["connection_id"],
                            },
                        )
                    )
                    .scalars()
                    .all()
                )
                if set(scopes) != {"https://www.googleapis.com/auth/gmail.readonly"}:
                    raise ProblemError(
                        status=409,
                        code="OPERATION_NOT_RETRYABLE",
                        title="This operation cannot be retried",
                    )
            # payload_expr has only the two static server-defined expressions.
            await conn.execute(
                text(
                    """INSERT INTO app.jobs
                       (id,workspace_id,connection_id,job_type,queue,idempotency_key,
                        status,payload)
                       VALUES (:id,:workspace,:connection,'sync','sync',:key,
                               'pending',"""
                    + payload_expr
                    + ")"
                ),
                {
                    "id": replacement,
                    "workspace": workspace_id,
                    "connection": prior["connection_id"],
                    "key": stored_key,
                    "previous": str(operation_id),
                    "collection": prior["collection"],
                },
            )
            created = await _job(conn, replacement, workspace_id, actor_id, lock=False)
            if created is None:
                raise _not_found()
            row = created
    result = _response(row, response)
    response.headers["Location"] = result.operation.links["self"]
    return result
