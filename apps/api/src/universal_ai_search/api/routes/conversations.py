"""Scoped conversation metadata and message pages; no generation or sharing."""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

import anyio
from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Receive, Scope, Send

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    PrincipalDependency,
    WorkspaceContext,
    WorkspaceDependency,
)
from universal_ai_search.api.idempotency import (
    build_fingerprint,
    validate_idempotency_key,
)
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
from universal_ai_search.conversations.extracts import DELIVERY_SECONDS
from universal_ai_search.conversations.repository import (
    ROUTE,
    Conversation,
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
    Message,
    MessageStatus,
    SortKey,
)

from .connections import _require_csrf
from .search import CitationResponse, ClaimResponse

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationCreate(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("title", mode="before")
    @classmethod
    def _trim_title(cls, value: object) -> object:
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("invalid title")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("invalid title") from None
            return value.strip()
        return value


class ConversationResponse(ResponseModel):
    id: CanonicalUUID
    title: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class MessageResponse(ResponseModel):
    id: CanonicalUUID
    role: Literal["user", "assistant"]
    status: MessageStatus
    content: str | None
    created_at: UtcDateTime
    completed_at: UtcDateTime | None
    claims: list[ClaimResponse] | None = None
    citations: list[CitationResponse] | None = None


class ConversationDetail(ResponseModel):
    conversation: ConversationResponse
    messages: Page[MessageResponse]


class MessagePaginationQuery(PaginationQuery):
    limit: int = Field(default=25, ge=1, le=50)


def _repository(request: Request) -> ConversationRepository:
    return cast(ConversationRepository, request.app.state.conversation_repository)


RepositoryDependency = Annotated[ConversationRepository, Depends(_repository)]


def _scope(principal: Principal, workspace: WorkspaceContext) -> ConversationScope:
    if principal.session_id is None or principal.mode not in {
        AuthenticationMode.BEARER,
        AuthenticationMode.BROWSER,
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


def _cursor_context(
    scope: ConversationScope, conversation_id: UUID | None
) -> CursorContext:
    sort = (
        ["created_at:asc", "id:asc"]
        if conversation_id
        else [
            "created_at:desc",
            "id:desc",
        ]
    )
    return CursorContext(
        endpoint="conversation-messages" if conversation_id else "conversations",
        workspace_id=scope.workspace_id,
        principal_id=scope.user_id,
        filter_sort_hash=normalized_context_hash(
            filters=(
                {"conversation_id": str(conversation_id)} if conversation_id else {}
            ),
            sort=sort,
        ),
    )


def _signer(request: Request) -> CursorSigner:
    return cast(CursorSigner, request.app.state.conversation_cursor_signer)


def _after(
    cursor: str | None, signer: CursorSigner, context: CursorContext
) -> SortKey | None:
    if cursor is None:
        return None
    values = signer.verify(cursor, expected=context).sort_key
    try:
        if len(values) != 2:
            raise ValueError
        value, identifier = values
        if not isinstance(value, str) or not isinstance(identifier, str):
            raise ValueError
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, UUID(identifier)
    except ValueError:
        raise ProblemError(
            status=400, code="CURSOR_INVALID", title="Cursor is invalid"
        ) from None


def _page(
    rows: list[Conversation] | list[Message],
    limit: int,
    signer: CursorSigner,
    context: CursorContext,
) -> PageMetadata:
    if len(rows) <= limit:
        return PageMetadata(has_more=False, next_cursor=None)
    final = rows[limit - 1]
    return PageMetadata(
        has_more=True,
        next_cursor=signer.issue(
            context=context, sort_key=[final.created_at.isoformat(), str(final.id)]
        ),
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        status=404, code="CONVERSATION_NOT_FOUND", title="Conversation is unavailable"
    )


def _model(item: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=item.id,
        title=item.title,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _message_model(item: Message) -> MessageResponse:
    common = MessageResponse(
        id=item.id,
        role=item.role,
        status=item.status,
        content=item.content,
        created_at=item.created_at,
        completed_at=item.completed_at,
    )
    if not item.extracts:
        return common
    claims: list[ClaimResponse] = []
    citations: list[CitationResponse] = []
    for extract in item.extracts:
        projection = extract.claim()
        claims.append(ClaimResponse.model_validate(projection["claim"]))
        for citation in cast(list[object], projection["citations"]):
            citations.append(CitationResponse.model_validate(citation))
    return common.model_copy(update={"claims": claims, "citations": citations})


def _detail(
    conversation: Conversation,
    messages: list[Message],
    limit: int,
    signer: CursorSigner,
    context: CursorContext,
) -> ConversationDetail:
    return ConversationDetail(
        conversation=_model(conversation),
        messages=Page[MessageResponse](
            items=[_message_model(item) for item in messages[:limit]],
            page=_page(messages, limit, signer, context),
        ),
    )


class ProtectedConversationResponse(Response):
    """Retain the authorization snapshot while handing one bounded JSON body to ASGI."""

    def __init__(
        self,
        *,
        repository: ConversationRepository,
        scope: ConversationScope,
        conversation_id: UUID,
        limit: int,
        after: SortKey | None,
        signer: CursorSigner,
        context: CursorContext,
        access_expires_at: datetime,
    ) -> None:
        super().__init__(media_type="application/json")
        self._repository = repository
        self._scope = scope
        self._conversation_id = conversation_id
        self._limit = limit
        self._after = after
        self._signer = signer
        self._context = context
        self._access_expires_at = access_expires_at

    def _require_horizon(self) -> None:
        if self._access_expires_at <= datetime.now(UTC) + timedelta(
            seconds=DELIVERY_SECONDS
        ):
            raise ConversationDenied

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with AsyncExitStack() as stack:
            try:
                with anyio.fail_after(4):
                    self._require_horizon()
                    conversation, messages = await stack.enter_async_context(
                        self._repository.read_for_delivery(
                            scope=self._scope,
                            conversation_id=self._conversation_id,
                            limit=self._limit,
                            after=self._after,
                        )
                    )
                    detail = _detail(
                        conversation, messages, self._limit, self._signer, self._context
                    )
                    payload = JSONResponse(
                        detail.model_dump(mode="json", exclude_unset=True),
                        headers={
                            "Cache-Control": "private, no-store",
                            "Vary": "Authorization, Cookie, X-Workspace-ID",
                        },
                    )
                    self._require_horizon()
            except (ConversationDenied, ConversationMissing):
                raise _unavailable() from None
            except (SQLAlchemyError, TimeoutError):
                raise ProblemError(
                    status=503,
                    code="CONVERSATION_READ_UNAVAILABLE",
                    title="Conversation is temporarily unavailable",
                    retryable=True,
                ) from None
            with anyio.fail_after(2):
                await payload(scope, receive, send)


@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    body: Annotated[ConversationCreate | None, Body()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ConversationResponse:
    scope = _scope(principal, workspace)
    await _require_csrf(request, principal.mode)
    title = (body.title if body else None) or "Untitled conversation"
    fingerprint = build_fingerprint(
        key=validate_idempotency_key(idempotency_key),
        hash_secret=cast(bytes, request.app.state.conversation_idempotency_key),
        workspace_id=scope.workspace_id,
        principal_id=scope.user_id,
        method="POST",
        route_template=ROUTE,
        validated_body={"title": title},
    )
    try:
        result = await repository.create(
            scope=scope,
            title=title,
            key_hash=bytes.fromhex(fingerprint.key_hash),
            request_hash=bytes.fromhex(fingerprint.request_hash),
        )
    except (ConversationDenied, ConversationMissing):
        raise _unavailable() from None
    except ConversationBusy:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            title="Request is already in progress",
            retryable=True,
            headers={"Retry-After": "2"},
        ) from None
    except ConversationKeyConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
        ) from None
    response.headers["Location"] = f"{ROUTE}/{result.id}"
    response.headers["Cache-Control"] = "private, no-store"
    return _model(result)


@router.get("", response_model=Page[ConversationResponse])
async def list_conversations(
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    query: Annotated[PaginationQuery, Query()],
) -> Page[ConversationResponse]:
    scope = _scope(principal, workspace)
    context, signer = _cursor_context(scope, None), _signer(request)
    try:
        found = await repository.list_owned(
            scope=scope,
            limit=query.limit + 1,
            after=_after(query.cursor, signer, context),
        )
    except ConversationDenied:
        raise _unavailable() from None
    response.headers["Cache-Control"] = "private, no-store"
    return Page[ConversationResponse](
        items=[_model(item) for item in found[: query.limit]],
        page=_page(found, query.limit, signer, context),
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    response_model_exclude_unset=True,
)
async def read_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    query: Annotated[MessagePaginationQuery, Query()],
) -> ConversationDetail | Response:
    scope = _scope(principal, workspace)
    context, signer = _cursor_context(scope, conversation_id), _signer(request)
    after = _after(query.cursor, signer, context)
    if (
        getattr(request.app.state, "conversation_stream_store", None) is not None
        and principal.access_expires_at is not None
    ):
        return ProtectedConversationResponse(
            repository=repository,
            scope=scope,
            conversation_id=conversation_id,
            limit=query.limit,
            after=after,
            signer=signer,
            context=context,
            access_expires_at=principal.access_expires_at,
        )
    try:
        conversation, messages = await repository.read(
            scope=scope,
            conversation_id=conversation_id,
            limit=query.limit + 1,
            after=after,
        )
    except (ConversationDenied, ConversationMissing):
        raise _unavailable() from None
    response.headers["Cache-Control"] = "private, no-store"
    return _detail(conversation, messages, query.limit, signer, context)
