"""Create portable account exports; redeem once via current session, not a URL token."""

from __future__ import annotations

import re
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import SQLAlchemyError

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
from universal_ai_search.auth.local_account_export import (
    ExportBusy,
    ExportMissing,
    ExportOperation,
    ExportUnsupported,
    LocalAccountExportStore,
)
from universal_ai_search.auth.reauth import ReauthenticationFailed
from universal_ai_search.auth.reauth_store import ReauthScope

from .connections import _require_csrf

router = APIRouter(prefix="/account", tags=["account"])
_PROOF = re.compile(r"[A-Za-z0-9_-]{43}")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization, Cookie, X-Workspace-ID",
    "X-Content-Type-Options": "nosniff",
}


class ExportProgress(ResponseModel):
    completed: int = 1
    total: int = 1
    unit: Literal["account"] = "account"


class ExportLinks(ResponseModel):
    self: str
    download: str | None = None


class AccountExportOperation(ResponseModel):
    id: CanonicalUUID
    type: Literal["account_export"] = "account_export"
    status: Literal["completed"] = "completed"
    progress: ExportProgress
    created_at: UtcDateTime
    started_at: UtcDateTime
    completed_at: UtcDateTime
    deadline_at: UtcDateTime
    error: None = None
    links: ExportLinks


class AccountExportResponse(ResponseModel):
    operation: AccountExportOperation


def export_operation(
    receipt: ExportOperation, response: Response
) -> AccountExportResponse:
    response.headers.update(_HEADERS)
    return AccountExportResponse(
        operation=AccountExportOperation(
            id=receipt.id,
            progress=ExportProgress(),
            created_at=receipt.created_at,
            started_at=receipt.created_at,
            completed_at=receipt.completed_at,
            deadline_at=receipt.download_expires_at,
            links=ExportLinks(
                self=f"/v1/operations/{receipt.id}",
                download=(
                    f"/v1/account/exports/{receipt.id}:download"
                    if receipt.downloadable
                    else None
                ),
            ),
        )
    )


def export_scope(principal: Principal, workspace: WorkspaceContext) -> ReauthScope:
    if (
        principal.mode not in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
        or principal.session_id is None
        or principal.subject_id != workspace.user_id
        or workspace.role != "owner"
    ):
        raise _missing()
    return ReauthScope(
        principal.subject_id,
        principal.session_id,
        workspace.workspace_id,
        principal.authorization_version,
        workspace.authorization_version,
    )


async def _protect(request: Request, principal: Principal) -> None:
    if any(
        len(request.headers.getlist(header)) > 1
        for header in ("authorization", "origin", "x-csrf-token", "x-workspace-id")
    ) or (
        principal.mode is AuthenticationMode.BEARER
        and ("origin" in request.headers or "cookie" in request.headers)
    ):
        raise _missing()
    await _require_csrf(request, principal.mode)


def _missing() -> ProblemError:
    return ProblemError(
        status=404,
        code="ACCOUNT_EXPORT_NOT_FOUND",
        title="Account export is unavailable",
        headers=_HEADERS,
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="ACCOUNT_EXPORT_UNAVAILABLE",
        title="Account export is unavailable",
        headers=_HEADERS,
    )


def _store(request: Request) -> LocalAccountExportStore:
    store = cast(
        LocalAccountExportStore | None,
        getattr(request.app.state, "local_account_export_store", None),
    )
    if store is None:
        raise _unavailable()
    return store


@router.post("/export", status_code=202, response_model=AccountExportResponse)
async def start_account_export(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
) -> AccountExportResponse:
    scope = export_scope(principal, workspace)
    await _protect(request, principal)
    proofs = request.headers.getlist("x-reauth-token")
    keys = request.headers.getlist("idempotency-key")
    try:
        if len(proofs) != 1 or not _PROOF.fullmatch(proofs[0]):
            raise ReauthenticationFailed
        # No request body is defined (and none is read into archive or replay).
        if request.headers.get("content-length") not in {None, "0"} or (
            "transfer-encoding" in request.headers
        ):
            raise ProblemError(
                status=400,
                code="UNEXPECTED_REQUEST_BODY",
                title="Account export does not accept a request body",
                headers=_HEADERS,
            )
        receipt = await _store(request).start(
            scope=scope,
            proof=proofs[0],
            idempotency_key=validate_idempotency_key(
                keys[0] if len(keys) == 1 else None
            ),
            request_id=request_id_for(request),
        )
    except ReauthenticationFailed:
        raise ProblemError(
            status=401,
            code="REAUTHENTICATION_FAILED",
            title="Reauthentication failed",
            headers=_HEADERS,
        ) from None
    except ExportMissing:
        raise _missing() from None
    except ExportUnsupported:
        raise ProblemError(
            status=409,
            code="ACCOUNT_EXPORT_SCOPE_UNSUPPORTED",
            title="This account is outside the supported export scope",
            headers=_HEADERS,
        ) from None
    except ExportBusy:
        raise ProblemError(
            status=409,
            code="ACCOUNT_EXPORT_BUSY",
            title="An export is already available or the request limit was reached",
            headers={**_HEADERS, "Retry-After": "300"},
        ) from None
    except SQLAlchemyError:
        raise _unavailable() from None
    result = export_operation(receipt, response)
    response.headers["Location"] = result.operation.links.self
    return result


@router.post("/exports/{operation_id}:download", response_class=Response)
async def download_account_export(
    operation_id: UUID,
    request: Request,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
) -> Response:
    scope = export_scope(principal, workspace)
    await _protect(request, principal)
    try:
        archive = await _store(request).download(
            scope=scope, operation_id=operation_id, request_id=request_id_for(request)
        )
    except ExportMissing:
        raise _missing() from None
    except SQLAlchemyError:
        raise _unavailable() from None
    return Response(
        archive.content,
        media_type="application/json",
        headers={
            **_HEADERS,
            "Content-Disposition": 'attachment; filename="account-export.json"',
        },
    )
