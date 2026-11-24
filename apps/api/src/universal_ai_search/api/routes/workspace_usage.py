"""Read-only indexed data counters and enforced workspace storage bounds."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.auth import PrincipalDependency, WorkspaceDependency
from universal_ai_search.api.models import ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError

router = APIRouter(tags=["usage"])


class UsageResponse(ResponseModel):
    indexed_source_count: int
    extracted_bytes: int
    indexed_source_limit: int = 25000
    extracted_byte_limit: int = 10 * 1024**3
    updated_at: UtcDateTime


@router.get("/usage", response_model=UsageResponse)
async def read_workspace_usage(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
) -> UsageResponse:
    if principal.session_id is None or principal.subject_id != workspace.user_id:
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    engine = cast(AsyncEngine, request.app.state.database_engine)
    async with engine.begin() as conn:
        values = {
            "actor": str(workspace.user_id),
            "workspace": str(workspace.workspace_id),
            "version": workspace.authorization_version,
        }
        await conn.execute(
            text("SELECT set_config('app.user_id', :actor, true)"), values
        )
        await conn.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"), values
        )
        row = (
            (
                await conn.execute(
                    text(
                        """SELECT COALESCE(u.indexed_source_count,0)
                                     AS indexed_source_count,
                              COALESCE(u.extracted_bytes,0) AS extracted_bytes,
                              COALESCE(u.updated_at,w.created_at) AS updated_at
                    FROM app.workspaces w LEFT JOIN app.workspace_usage u
                        ON u.workspace_id = w.id
                    WHERE w.id = CAST(:workspace AS UUID) AND w.status = 'active'
                      AND w.authorization_version = :version"""
                    ),
                    values,
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ProblemError(
            status=404, code="WORKSPACE_NOT_FOUND", title="Workspace not found"
        )
    response.headers["Cache-Control"] = "private, no-store"
    return UsageResponse.model_validate(dict(row))
