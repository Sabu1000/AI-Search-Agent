"""Read-only authenticated device management, scoped to the registering user."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from universal_ai_search.api.auth import (
    AuthenticationMode,
    PrincipalDependency,
    WorkspaceDependency,
)
from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.api.pagination import (
    CursorContext,
    CursorSigner,
    JsonScalar,
    Page,
    PageMetadata,
    PaginationQuery,
    normalized_context_hash,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.devices.query import (
    DeviceQueryDenied,
    DeviceQueryReader,
    DeviceQueryUnavailable,
    DeviceSortKey,
    DeviceStatus,
    DeviceSummary,
)
from universal_ai_search.devices.registration import DevicePlatform, RegistrationScope

router = APIRouter(prefix="/devices", tags=["devices"])
_ROUTE = "GET /v1/devices"
_SORT = ["created_at:desc", "id:desc"]
_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Origin, Authorization, Cookie, X-Workspace-ID",
}


class DeviceResponse(ResponseModel):
    id: CanonicalUUID
    display_name: str
    platform: DevicePlatform
    app_version: str | None
    status: DeviceStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime
    last_seen_at: UtcDateTime | None
    revoked_at: UtcDateTime | None


def _missing() -> ProblemError:
    return ProblemError(
        status=404,
        code="DEVICE_NOT_FOUND",
        title="Device is unavailable",
        headers=_HEADERS,
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=503,
        code="DEVICE_QUERY_UNAVAILABLE",
        title="Device list is temporarily unavailable",
        retryable=True,
        headers=_HEADERS,
    )


def _scope(
    request: Request, principal: PrincipalDependency, workspace: WorkspaceDependency
) -> RegistrationScope:
    if (
        principal.mode not in {AuthenticationMode.BROWSER, AuthenticationMode.BEARER}
        or principal.session_id is None
    ):
        raise ProblemError(
            status=401,
            code="SESSION_INVALID",
            title="Session is invalid",
            headers=_HEADERS,
        )
    if (
        workspace.user_id != principal.subject_id
        or len(request.headers.getlist("x-workspace-id")) != 1
    ):
        raise _missing()
    return RegistrationScope(
        workspace_id=workspace.workspace_id,
        user_id=principal.subject_id,
        session_id=principal.session_id,
        user_authorization_version=principal.authorization_version,
        workspace_authorization_version=workspace.authorization_version,
    )


def _reader(request: Request) -> DeviceQueryReader:
    reader = getattr(request.app.state, "device_query_reader", None)
    if reader is None:
        raise _unavailable()
    return cast(DeviceQueryReader, reader)


def _signer(request: Request) -> CursorSigner:
    signer = getattr(request.app.state, "device_cursor_signer", None)
    if signer is None:
        raise _unavailable()
    return cast(CursorSigner, signer)


Scope = Annotated[RegistrationScope, Depends(_scope)]
Reader = Annotated[DeviceQueryReader, Depends(_reader)]
Signer = Annotated[CursorSigner, Depends(_signer)]


def _context(scope: RegistrationScope) -> CursorContext:
    return CursorContext(
        endpoint=_ROUTE,
        workspace_id=scope.workspace_id,
        principal_id=scope.user_id,
        filter_sort_hash=normalized_context_hash(
            filters={
                "user_authorization_version": scope.user_authorization_version,
                "workspace_version": scope.workspace_authorization_version,
            },
            sort=_SORT,
        ),
    )


def _key(values: list[JsonScalar]) -> DeviceSortKey:
    try:
        if len(values) != 2:
            raise ValueError
        timestamp, device = values
        if not isinstance(timestamp, str) or not isinstance(device, str):
            raise ValueError
        moment = datetime.fromisoformat(timestamp)
        if moment.tzinfo is None:
            raise ValueError
        return moment, UUID(device)
    except ValueError:
        raise ProblemError(
            status=400,
            code="CURSOR_INVALID",
            title="Cursor is invalid",
            headers=_HEADERS,
        ) from None


def _response(summary: DeviceSummary) -> DeviceResponse:
    return DeviceResponse(
        id=summary.id,
        display_name=summary.name,
        platform=summary.platform,
        app_version=summary.app_version,
        status=summary.status,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        last_seen_at=summary.last_seen_at,
        revoked_at=summary.revoked_at,
    )


@router.get("", response_model=Page[DeviceResponse])
async def list_devices(
    query: Annotated[PaginationQuery, Query()],
    response: Response,
    scope: Scope,
    reader: Reader,
    signer: Signer,
) -> Page[DeviceResponse]:
    context = _context(scope)
    before = (
        _key(signer.verify(query.cursor, expected=context).sort_key)
        if query.cursor
        else None
    )
    try:
        devices = await reader.list_devices(
            scope=scope, before=before, limit=query.limit + 1
        )
    except DeviceQueryDenied:
        raise _missing() from None
    except DeviceQueryUnavailable:
        raise _unavailable() from None
    page = devices[: query.limit]
    has_more = len(devices) > query.limit
    cursor = None
    if has_more:
        last = page[-1]
        cursor = signer.issue(
            context=context,
            sort_key=[last.created_at.isoformat(), str(last.id)],
        )
    response.headers.update(_HEADERS)
    return Page[DeviceResponse](
        items=[_response(device) for device in page],
        page=PageMetadata(has_more=has_more, next_cursor=cursor),
    )


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: UUID, response: Response, scope: Scope, reader: Reader
) -> DeviceResponse:
    try:
        device = await reader.get(scope=scope, device_id=device_id)
    except DeviceQueryDenied:
        raise _missing() from None
    except DeviceQueryUnavailable:
        raise _unavailable() from None
    if device is None:
        raise _missing()
    response.headers.update(_HEADERS)
    return _response(device)
