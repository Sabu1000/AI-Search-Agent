"""Scoped conversation metadata and message pages; no generation or sharing."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import Field, field_validator

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
        return PageMetadata(has_more=False)
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
    return MessageResponse(
        id=item.id,
        role=item.role,
        status=item.status,
        content=item.content,
        created_at=item.created_at,
        completed_at=item.completed_at,
    )


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


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def read_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    repository: RepositoryDependency,
    query: Annotated[MessagePaginationQuery, Query()],
) -> ConversationDetail:
    scope = _scope(principal, workspace)
    context, signer = _cursor_context(scope, conversation_id), _signer(request)
    try:
        conversation, messages = await repository.read(
            scope=scope,
            conversation_id=conversation_id,
            limit=query.limit + 1,
            after=_after(query.cursor, signer, context),
        )
    except (ConversationDenied, ConversationMissing):
        raise _unavailable() from None
    response.headers["Cache-Control"] = "private, no-store"
    return ConversationDetail(
        conversation=_model(conversation),
        messages=Page[MessageResponse](
            items=[_message_model(item) for item in messages[: query.limit]],
            page=_page(messages, query.limit, signer, context),
        ),
    )
