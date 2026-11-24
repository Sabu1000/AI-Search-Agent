"""Verified and explicitly confirmed disconnect of an untouched GitHub grant."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.reauth import ReauthenticationFailed
from universal_ai_search.auth.reauth_store import ReauthScope
from universal_ai_search.connections.disconnect import (
    CONFIRMATION,
    ConnectionDisconnectStore,
    DisconnectBusy,
    DisconnectConflict,
    DisconnectMissing,
    DisconnectReceipt,
    DisconnectUnsupported,
)

from .connections import _require_csrf

router = APIRouter(prefix="/connections", tags=["connections"])
_NO_CACHE = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Vary": "Authorization, Cookie, X-Workspace-ID",
}


class DisconnectProgress(ResponseModel):
    completed: Literal[1] = 1
    total: Literal[1] = 1
    unit: Literal["connection"] = "connection"


class DisconnectOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["connection_delete"] = "connection_delete"
    status: Literal["completed"] = "completed"
    progress: DisconnectProgress
    created_at: UtcDateTime
    started_at: UtcDateTime
    completed_at: UtcDateTime
    deadline_at: UtcDateTime
    error: None = None
    links: dict[Literal["self"], str]


class DisconnectResponse(ResponseModel):
    operation: DisconnectOperation


def completed_response(receipt: DisconnectReceipt) -> DisconnectResponse:
    if (
        receipt.status != "completed"
        or receipt.started_at is None
        or receipt.completed_at is None
    ):
        raise ValueError("Incomplete disconnect receipts cannot be exposed")
    return DisconnectResponse(
        operation=DisconnectOperation(
            id=receipt.id,
            progress=DisconnectProgress(),
            created_at=receipt.requested_at,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.deadline_at,
            links={"self": f"/v1/operations/{receipt.id}"},
        )
    )


def manager_scope(
    principal: Principal, workspace: WorkspaceContext
) -> ReauthScope | None:
    if (
        principal.mode not in {AuthenticationMode.BEARER, AuthenticationMode.BROWSER}
        or principal.session_id is None
        or principal.subject_id != workspace.user_id
        or workspace.role not in {"owner", "admin"}
    ):
        return None
    return ReauthScope(
        principal.subject_id,
        principal.session_id,
        workspace.workspace_id,
        principal.authorization_version,
        workspace.authorization_version,
    )


def _missing() -> ProblemError:
    return ProblemError(
        status=404,
        code="CONNECTION_NOT_FOUND",
        title="Connection is unavailable",
        headers=_NO_CACHE,
    )


def _busy() -> ProblemError:
    return ProblemError(
        status=409,
        code="OPERATION_IN_PROGRESS",
        title="Connection work is already in progress",
        retryable=True,
        headers={**_NO_CACHE, "Retry-After": "2"},
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DISCONNECT_UNAVAILABLE",
        title="Disconnect is unavailable",
        headers=_NO_CACHE,
    )


def _reauth_required() -> ProblemError:
    return ProblemError(
        status=401,
        code="REAUTHENTICATION_REQUIRED",
        title="A recent password proof is required",
        headers=_NO_CACHE,
    )


@router.delete("/{connection_id}", response_model=DisconnectResponse, status_code=202)
async def delete_connection(
    connection_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    confirmation: Annotated[list[str] | None, Header(alias="X-Confirm-Action")] = None,
    proof: Annotated[list[str] | None, Header(alias="X-Reauth-Token")] = None,
    keys: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None,
) -> DisconnectResponse:
    scope = manager_scope(principal, workspace)
    if scope is None:
        raise _missing()
    await _require_csrf(request, principal.mode)
    if confirmation != [CONFIRMATION]:
        raise ProblemError(
            status=400,
            code="CONFIRMATION_REQUIRED",
            title="Disconnect needs exact confirmation",
            detail="Set X-Confirm-Action to disconnect.",
            headers=_NO_CACHE,
        )
    if proof is None or len(proof) != 1:
        raise _reauth_required()
    key = validate_idempotency_key(
        keys[0] if keys is not None and len(keys) == 1 else None
    )
    store = cast(
        ConnectionDisconnectStore | None,
        getattr(request.app.state, "connection_disconnect_store", None),
    )
    if store is None:
        raise _unavailable()
    try:
        receipt = await store.delete(
            scope=scope,
            connection_id=connection_id,
            reauth_token=proof[0],
            idempotency_key=key,
            request_id=request_id_for(request),
        )
    except ReauthenticationFailed:
        raise _reauth_required() from None
    except DisconnectMissing:
        raise _missing() from None
    except DisconnectUnsupported:
        raise ProblemError(
            status=409,
            code="DISCONNECT_UNSUPPORTED",
            title="This connection cannot be deleted through this endpoint",
            detail=(
                "Only GitHub connections whose first sync has not started "
                "can be deleted here."
            ),
            headers=_NO_CACHE,
        ) from None
    except DisconnectConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
            headers=_NO_CACHE,
        ) from None
    except DisconnectBusy:
        raise _busy() from None
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise _busy() from None
        raise _unavailable() from None
    except SQLAlchemyError:
        raise _unavailable() from None
    result = completed_response(receipt)
    response.headers.update(_NO_CACHE)
    response.headers["Location"] = result.operation.links["self"]
    return result
