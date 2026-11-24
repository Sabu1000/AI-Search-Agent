"""Authenticated search history, clearing, and query-only autocomplete."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field, field_validator

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
from universal_ai_search.api.pagination import (
    CursorContext,
    CursorSigner,
    Page,
    PageMetadata,
    PaginationQuery,
    normalized_context_hash,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.conversations.repository import (
    ConversationDenied,
    ConversationScope,
)
from universal_ai_search.search.history import (
    CLEAR_CONFIRMATION,
    MAX_SUGGESTIONS,
    SUGGESTION_TEXT_LIMIT,
    HistoryClearPending,
    HistoryKeyConflict,
    HistoryOrder,
    SearchHistoryStore,
    SearchMode,
    SearchState,
)

from .connections import _require_csrf

router = APIRouter(prefix="/search", tags=["search"])

SuggestionFilter = Literal["query"]
PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization, Cookie, X-Workspace-ID",
}


class SuggestionRequest(RequestModel):
    prefix: str = Field(min_length=1, max_length=128)
    filter_types: tuple[SuggestionFilter, ...] = Field(default=("query",), max_length=1)
    limit: int = Field(default=MAX_SUGGESTIONS, ge=1, le=MAX_SUGGESTIONS)

    @field_validator("prefix")
    @classmethod
    def _usable_prefix(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("prefix must contain searchable text")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("prefix must be valid UTF-8") from None
        # Keep trailing whitespace as users type the next search term.
        return value.lstrip()


class SuggestionItem(ResponseModel):
    kind: Literal["query"] = "query"
    value: str = Field(min_length=1, max_length=SUGGESTION_TEXT_LIMIT)


class SuggestionResponse(ResponseModel):
    suggestions: list[SuggestionItem] = Field(max_length=MAX_SUGGESTIONS)


class HistoryResponse(ResponseModel):
    id: CanonicalUUID
    query: str
    mode: SearchMode
    status: SearchState
    result_count: int | None
    created_at: UtcDateTime
    completed_at: UtcDateTime | None


def _store(request: Request) -> SearchHistoryStore:
    store = getattr(request.app.state, "search_history_store", None)
    if store is None:
        raise ProblemError(
            status=503,
            code="SEARCH_HISTORY_UNAVAILABLE",
            title="Search history is unavailable",
            retryable=True,
        )
    return cast(SearchHistoryStore, store)


StoreDependency = Annotated[SearchHistoryStore, Depends(_store)]


def _session(principal: Principal, workspace: WorkspaceContext) -> ConversationScope:
    if principal.session_id is None or principal.mode not in {
        AuthenticationMode.BROWSER,
        AuthenticationMode.BEARER,
    }:
        raise ProblemError(
            status=401, code="SESSION_INVALID", title="Session is invalid"
        )
    if principal.subject_id != workspace.user_id:
        raise _unavailable()
    return ConversationScope(
        workspace.workspace_id,
        principal.subject_id,
        principal.session_id,
        workspace.authorization_version,
        principal.authorization_version,
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=404,
        code="SEARCH_HISTORY_NOT_FOUND",
        title="Search history is unavailable",
    )


def _after(
    cursor: str | None, signer: CursorSigner, context: CursorContext
) -> HistoryOrder | None:
    if cursor is None:
        return None
    values = signer.verify(cursor, expected=context).sort_key
    try:
        if len(values) != 2:
            raise ValueError
        created_at, identifier = values
        if not isinstance(created_at, str) or not isinstance(identifier, str):
            raise ValueError
        stamp = datetime.fromisoformat(created_at)
        if stamp.utcoffset() is None:
            raise ValueError
        return stamp, UUID(identifier)
    except ValueError:
        raise ProblemError(
            status=400, code="CURSOR_INVALID", title="Cursor is invalid"
        ) from None


@router.post("/suggestions", response_model=SuggestionResponse)
async def search_suggestions(
    body: SuggestionRequest,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
) -> SuggestionResponse:
    scope = _session(principal, workspace)
    await _require_csrf(request, principal.mode)
    try:
        if "query" in body.filter_types:
            found = await store.suggest_owned(
                scope=scope, prefix=body.prefix, limit=body.limit
            )
        else:
            found = []
    except ConversationDenied:
        raise _unavailable() from None
    response.headers.update(PRIVATE_HEADERS)
    return SuggestionResponse(
        suggestions=[SuggestionItem(value=value) for value in found]
    )


@router.get("/history", response_model=Page[HistoryResponse])
async def list_search_history(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    query: Annotated[PaginationQuery, Query()],
) -> Page[HistoryResponse]:
    scope = _session(principal, workspace)
    signer = cast(CursorSigner, request.app.state.conversation_cursor_signer)
    context = CursorContext(
        endpoint="search-history",
        workspace_id=scope.workspace_id,
        principal_id=scope.user_id,
        filter_sort_hash=normalized_context_hash(
            filters={}, sort=["created_at:desc", "id:desc"]
        ),
    )
    try:
        rows = await store.list_owned(
            scope=scope,
            limit=query.limit + 1,
            after=_after(query.cursor, signer, context),
        )
    except ConversationDenied:
        raise _unavailable() from None
    next_cursor = None
    if len(rows) > query.limit:
        last = rows[query.limit - 1]
        next_cursor = signer.issue(
            context=context, sort_key=[last.created_at.isoformat(), str(last.id)]
        )
    response.headers.update(PRIVATE_HEADERS)
    return Page[HistoryResponse](
        items=[
            HistoryResponse(
                id=row.id,
                query=row.query,
                mode=row.mode,
                status=row.status,
                result_count=row.result_count,
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
            for row in rows[: query.limit]
        ],
        page=PageMetadata(has_more=next_cursor is not None, next_cursor=next_cursor),
    )


@router.delete("/history", status_code=204)
async def clear_search_history(
    request: Request,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    store: StoreDependency,
    confirmation: Annotated[list[str] | None, Header(alias="X-Confirm-Action")] = None,
    idempotency_keys: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = (
        None
    ),
) -> Response:
    scope = _session(principal, workspace)
    await _require_csrf(request, principal.mode)
    if confirmation != [CLEAR_CONFIRMATION]:
        raise ProblemError(
            status=400,
            code="CONFIRMATION_REQUIRED",
            title="Clearing search history needs exact confirmation",
            detail="Set X-Confirm-Action to clear-search-history.",
        )
    if idempotency_keys and len(idempotency_keys) != 1:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="Idempotency key is invalid",
            detail="Supply exactly one Idempotency-Key header.",
        )
    try:
        await store.clear_owned(
            scope=scope,
            idempotency_key=validate_idempotency_key(
                idempotency_keys[0] if idempotency_keys else None
            ),
            request_id=request_id_for(request),
        )
    except ConversationDenied:
        raise _unavailable() from None
    except HistoryClearPending:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            title="Request is already in progress",
            retryable=True,
            headers={"Retry-After": "2"},
        ) from None
    except HistoryKeyConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
        ) from None
    return Response(status_code=204, headers=PRIVATE_HEADERS)
