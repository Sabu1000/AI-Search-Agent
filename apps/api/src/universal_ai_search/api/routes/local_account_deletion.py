"""Restricted database-only account deletion and separate receipt capability."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
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
from universal_ai_search.auth.local_account_deletion import (
    LocalAccountDeletionReceiptInvalid,
    LocalAccountDeletionStore,
    LocalAccountDeletionUnsupported,
)
from universal_ai_search.auth.reauth import ReauthenticationFailed
from universal_ai_search.auth.reauth_store import ReauthScope

from .auth import _clear_session_cookies
from .connections import _require_csrf

router = APIRouter(prefix="/account", tags=["privacy"])

_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization, Cookie, X-Workspace-ID",
}


class DeleteLocalAccountRequest(RequestModel):
    confirmation: Literal["DELETE MY ACCOUNT"]


class DeleteLocalAccountResponse(ResponseModel):
    deletion_request_id: CanonicalUUID
    deadline_at: UtcDateTime
    deletion_receipt_token: str = Field(repr=False)


class DeletionReceiptResponse(ResponseModel):
    deletion_request_id: CanonicalUUID
    status: Literal["completed"] = "completed"
    requested_at: UtcDateTime
    deadline_at: UtcDateTime
    completed_at: UtcDateTime
    remaining_counts: dict[str, int] = Field(
        default_factory=lambda: {"active_accounts": 0, "workspaces": 0}
    )
    failure_codes: tuple[str, ...] = ()
    support_guidance: str = (
        "Deletion from active storage completed. Keep this receipt private. "
        "Contact support if you need help with retention or recovery."
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="ACCOUNT_DELETION_UNAVAILABLE",
        title="Account deletion is unavailable",
        retryable=True,
        headers=_HEADERS,
    )


def _store(request: Request) -> LocalAccountDeletionStore:
    # This deliberately requires separate application wiring. It is not a
    # production deletion workflow without backup/restore reconciliation.
    store = getattr(request.app.state, "local_account_deletion_store", None)
    if store is None:
        raise _unavailable()
    return cast(LocalAccountDeletionStore, store)


StoreDependency = Annotated[LocalAccountDeletionStore, Depends(_store)]


@router.delete("", status_code=202, response_model=DeleteLocalAccountResponse)
async def delete_local_account(
    body: DeleteLocalAccountRequest,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    confirm: Annotated[list[str] | None, Header(alias="X-Confirm-Action")] = None,
    proof: Annotated[list[str] | None, Header(alias="X-Reauth-Token")] = None,
    keys: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None,
) -> DeleteLocalAccountResponse:
    # FastAPI validates the exact body confirmation before invoking this handler.
    del body
    if (
        principal.session_id is None
        or principal.subject_id != workspace.user_id
        or principal.mode not in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
    ):
        raise ProblemError(
            status=401,
            code="SESSION_INVALID",
            title="Session is invalid",
            headers=_HEADERS,
        )
    await _require_csrf(request, principal.mode)
    if confirm != ["delete-account"]:
        raise ProblemError(
            status=400,
            code="CONFIRMATION_REQUIRED",
            title="Account deletion needs exact confirmation",
            detail="Set X-Confirm-Action to delete-account.",
            headers=_HEADERS,
        )
    if not proof or len(proof) != 1:
        raise ProblemError(
            status=403,
            code="REAUTHENTICATION_REQUIRED",
            title="Recent password confirmation is required",
            headers=_HEADERS,
        )
    if keys and len(keys) != 1:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="Idempotency key is invalid",
            headers=_HEADERS,
        )
    key = validate_idempotency_key(keys[0] if keys else None)
    try:
        receipt = await store.delete(
            scope=ReauthScope(
                principal.subject_id,
                principal.session_id,
                workspace.workspace_id,
                principal.authorization_version,
                workspace.authorization_version,
            ),
            reauth_token=proof[0],
            idempotency_key=key,
            correlation_id=request_id_for(request),
        )
    except ReauthenticationFailed:
        raise ProblemError(
            status=403,
            code="REAUTHENTICATION_REQUIRED",
            title="Recent password confirmation is required",
            headers=_HEADERS,
        ) from None
    except LocalAccountDeletionUnsupported:
        raise ProblemError(
            status=409,
            code="ACCOUNT_DELETION_REQUIRES_SUPPORT",
            title="Account deletion requires support",
            detail="No data was removed. The account is not eligible for self-service.",
            headers=_HEADERS,
        ) from None
    except SQLAlchemyError:
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    if principal.mode is AuthenticationMode.BROWSER:
        _clear_session_cookies(response)
    return DeleteLocalAccountResponse(
        deletion_request_id=receipt.deletion.id,
        deadline_at=receipt.deletion.deadline_at,
        deletion_receipt_token=receipt.token,
    )


@router.get("/deletion-status", response_model=DeletionReceiptResponse)
async def local_account_deletion_status(
    response: Response,
    store: StoreDependency,
    authorization: Annotated[list[str] | None, Header()] = None,
) -> DeletionReceiptResponse:
    try:
        if not authorization or len(authorization) != 1:
            raise LocalAccountDeletionReceiptInvalid
        scheme, separator, token = authorization[0].partition(" ")
        if scheme.casefold() != "deletionreceipt" or not separator:
            raise LocalAccountDeletionReceiptInvalid
        deletion = await store.read_receipt(token)
    except LocalAccountDeletionReceiptInvalid:
        raise ProblemError(
            status=401,
            code="DELETION_RECEIPT_INVALID",
            title="Deletion receipt is unavailable",
            headers=_HEADERS,
        ) from None
    except SQLAlchemyError:
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return DeletionReceiptResponse(
        deletion_request_id=deletion.id,
        requested_at=deletion.requested_at,
        deadline_at=deletion.deadline_at,
        completed_at=deletion.completed_at,
    )
