"""Bounded SSE with durable authorization-checked extractive evidence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Literal, cast

import anyio
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Message, Receive, Scope, Send

from universal_ai_search.api.auth import PrincipalDependency, WorkspaceDependency
from universal_ai_search.api.context import request_id_for
from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.api.models import CanonicalUUID, RequestModel
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.conversations.attempts import (
    StreamAlreadyProcessed,
    StreamAttempt,
    StreamAttemptStore,
    StreamRetryUnavailable,
)
from universal_ai_search.conversations.extracts import (
    DELIVERY_SECONDS,
    MAX_CANDIDATES,
    EvidencePointer,
    LiveExtract,
)
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationScope,
)
from universal_ai_search.conversations.user_turn import (
    MAX_USER_CONTENT_LENGTH,
    UserTurnInputError,
)
from universal_ai_search.indexing.pipeline import embed_text
from universal_ai_search.search.planner import SearchFilters, SearchPlan, build_plan
from universal_ai_search.search.ranking import fuse_and_rank
from universal_ai_search.search.repository import SearchRepository

from .connections import _require_csrf
from .conversations import _scope, _unavailable
from .search import Provider, SearchFilterRequest

router = APIRouter(prefix="/conversations", tags=["conversations"])
HEARTBEAT_SECONDS = 10.0
RETRIEVAL_TIMEOUT = 25.0
RESPONSE_TIMEOUT = 45.0
BODY_SEND_TIMEOUT = 2.0
DELIVERY_PREP_TIMEOUT = 4.0


class StreamRequest(RequestModel):
    client_message_id: CanonicalUUID
    retry_user_message_id: CanonicalUUID | None = None
    content: str | None = Field(default=None, max_length=MAX_USER_CONTENT_LENGTH)
    providers: list[Provider] = Field(default_factory=list, max_length=4)
    filters: SearchFilterRequest = Field(default_factory=SearchFilterRequest)

    @model_validator(mode="after")
    def _normal_or_retry(self) -> StreamRequest:
        if self.client_message_id.int == 0:
            raise ValueError("client_message_id must not be nil")
        if self.retry_user_message_id is not None:
            if self.retry_user_message_id.int == 0 or self.content is not None:
                raise ValueError("retry must reference a prior message without content")
        elif self.content is None or not self.content.strip() or "\x00" in self.content:
            raise ValueError("a new turn requires nonempty content")
        else:
            try:
                self.content.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("content must be valid UTF-8") from None
        return self

    def search_filters(self) -> SearchFilters:
        filters = self.filters
        return SearchFilters(
            providers=tuple(self.providers),
            people=tuple(filters.people),
            repository_ids=tuple(filters.repository_ids),
            folder_ids=tuple(filters.folder_ids),
            source_types=tuple(filters.source_types),
            file_types=tuple(filters.file_types),
            date_from=filters.date_from,
            date_to_exclusive=filters.date_to_exclusive,
        )


def _dependencies(request: Request) -> tuple[StreamAttemptStore, SearchRepository]:
    store = getattr(request.app.state, "conversation_stream_store", None)
    repository = getattr(request.app.state, "conversation_stream_search", None)
    if store is None or repository is None:
        raise ProblemError(
            status=503,
            code="CONVERSATION_STREAM_UNAVAILABLE",
            title="Conversation streaming is unavailable",
            retryable=True,
        )
    return cast(StreamAttemptStore, store), cast(SearchRepository, repository)


StreamDependencies = Annotated[
    tuple[StreamAttemptStore, SearchRepository], Depends(_dependencies)
]


class BoundedEventResponse(StreamingResponse):
    """Always close the iterator when ASGI send fails or the client disconnects."""

    def __init__(self, stream: ConversationStream) -> None:
        self._stream = stream
        self._iterator = stream.events()
        super().__init__(
            self._iterator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "Vary": "Authorization, Cookie, X-Workspace-ID",
                "X-Accel-Buffering": "no",
            },
        )

    async def stream_response(self, send: Send) -> None:
        async def send_bounded(message: Message) -> None:
            # Positive content is one bounded body. The stream's database
            # context remains open while Starlette awaits this send.
            with anyio.fail_after(BODY_SEND_TIMEOUT):
                await send(message)

        await super().stream_response(send_bounded)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            with anyio.move_on_after(RESPONSE_TIMEOUT):
                await super().__call__(scope, receive, send)
        finally:
            with anyio.move_on_after(3, shield=True):
                with suppress(SQLAlchemyError, TimeoutError):
                    await self._iterator.aclose()
                await self._stream.interrupt()


class ConversationStream:
    def __init__(
        self,
        *,
        store: StreamAttemptStore,
        repository: SearchRepository,
        scope: ConversationScope,
        conversation_id: CanonicalUUID,
        attempt: StreamAttempt,
        filters: SearchFilters,
        correlation_id: CanonicalUUID,
        access_expires_at: datetime | None = None,
    ) -> None:
        self._store = store
        self._repository = repository
        self._scope = scope
        self._conversation_id = conversation_id
        self._attempt = attempt
        self._filters = filters
        self._correlation_id = correlation_id
        self._started = monotonic()
        self._settled = attempt.replay
        self._access_expires_at = access_expires_at

    def _duration(self) -> int:
        return max(0, round((monotonic() - self._started) * 1000))

    async def _rank_pointers(self) -> tuple[SearchPlan, tuple[EvidencePointer, ...]]:
        plan = build_plan(
            query=self._attempt.query,
            mode="answer",
            filters=self._filters,
            limit=MAX_CANDIDATES,
        )
        async with asyncio.timeout(RETRIEVAL_TIMEOUT):
            snapshot = await self._repository.retrieve(
                workspace_id=self._scope.workspace_id,
                user_id=self._scope.user_id,
                plan=plan,
                embedding=embed_text(plan.normalized_query),
            )
            ranked = fuse_and_rank(snapshot.lanes, limit=plan.limit)
        return plan, tuple(
            EvidencePointer(
                row.candidate.source_id,
                row.candidate.chunk_id,
                row.candidate.chunk_hash,
                row.score,
            )
            for row in ranked
        )

    def _require_token_horizon(self) -> None:
        if self._access_expires_at is None or self._access_expires_at <= datetime.now(
            UTC
        ) + timedelta(seconds=DELIVERY_SECONDS):
            raise ConversationDenied

    async def _finish(
        self, status: Literal["insufficient_evidence", "interrupted", "failed"]
    ) -> None:
        await self._store.finish(
            scope=self._scope,
            conversation_id=self._conversation_id,
            attempt=self._attempt,
            status=status,
            latency_ms=self._duration(),
        )
        self._settled = True

    async def interrupt(self) -> None:
        if not self._settled:
            with suppress(
                ConversationDenied,
                ConversationMissing,
                SQLAlchemyError,
                StreamAlreadyProcessed,
                TimeoutError,
            ):
                await self._finish("interrupted")

    async def events(self) -> AsyncGenerator[bytes, None]:
        yield _event(
            1,
            "message.started",
            {
                "request_id": str(self._attempt.request_id),
                "user_message_id": str(self._attempt.user_message_id),
                "assistant_message_id": str(self._attempt.assistant_message_id),
            },
        )
        found = None
        terminal_sent = False
        try:
            if not self._attempt.replay:
                found = asyncio.create_task(self._rank_pointers())
                while not found.done():
                    done, _ = await asyncio.wait((found,), timeout=HEARTBEAT_SECONDS)
                    if not done:
                        yield b": keepalive\n\n"
                plan, pointers = found.result()
                if not pointers:
                    await self._finish("insufficient_evidence")
                    terminal_sent = True
                    yield self._insufficient()
                    return
                self._require_token_horizon()
                # A successful SQL commit must precede any positive frame.
                await self._store.finish_with_pointers(
                    scope=self._scope,
                    conversation_id=self._conversation_id,
                    attempt=self._attempt,
                    candidates=pointers,
                    plan=plan,
                    latency_ms=self._duration(),
                )
                self._settled = True
            async with AsyncExitStack() as stack:
                # Keep total preparation + send within the database expiry
                # horizon; a per-statement timeout alone is not sufficient.
                with anyio.fail_after(DELIVERY_PREP_TIMEOUT):
                    delivery = await stack.enter_async_context(
                        self._store.delivery(
                            scope=self._scope,
                            conversation_id=self._conversation_id,
                            attempt=self._attempt,
                        )
                    )
                    if delivery.status == "complete":
                        # Recheck the access token's wall-clock expiry separately
                        # from database session authorization.
                        self._require_token_horizon()
                        packet = self._complete(delivery.extracts)
                    else:
                        packet = self._insufficient()
                terminal_sent = True
                yield packet
        except (
            ConversationDenied,
            ConversationMissing,
            SQLAlchemyError,
            StreamAlreadyProcessed,
            TimeoutError,
            ValueError,
            RuntimeError,
        ) as error:
            if terminal_sent:
                return
            try:
                if not self._settled:
                    await self._finish("failed")
            except (
                ConversationDenied,
                ConversationMissing,
                SQLAlchemyError,
                StreamAlreadyProcessed,
                TimeoutError,
            ):
                pass
            yield (
                self._error("CONVERSATION_STREAM_TIMEOUT", 504)
                if isinstance(error, TimeoutError)
                else self._error("CONVERSATION_STREAM_UNAVAILABLE", 503)
            )
        finally:
            if found is not None:
                if not found.done():
                    found.cancel()
                with anyio.move_on_after(2, shield=True):
                    with suppress(
                        asyncio.CancelledError,
                        SQLAlchemyError,
                        TimeoutError,
                        RuntimeError,
                        ValueError,
                    ):
                        await found

    def _insufficient(self) -> bytes:
        return _event(
            2,
            "retrieval.completed",
            {
                "results": [],
                "degradation": {
                    "semantic_search": "ok",
                    "answer_generation": "unavailable",
                },
                "timing": {"retrieval_ms": self._duration(), "generation_ms": 0},
            },
        ) + _event(
            3,
            "message.insufficient",
            {
                "message_id": str(self._attempt.assistant_message_id),
                "reason": "no_authorized_results",
                "results": [],
                "follow_up_queries": [],
            },
        )

    def _complete(self, items: tuple[LiveExtract, ...]) -> bytes:
        if not items:
            raise ValueError("completion requires live evidence")
        duration = self._duration()
        events = [
            _event(
                2,
                "retrieval.completed",
                {
                    "results": [item.result() for item in items],
                    "degradation": {
                        "semantic_search": "ok",
                        "answer_generation": "ok",
                    },
                    "timing": {"retrieval_ms": duration, "generation_ms": 0},
                },
            )
        ]
        events.extend(
            _event(i + 3, "claim.completed", item.claim())
            for i, item in enumerate(items)
        )
        events.append(
            _event(
                3 + len(items),
                "message.completed",
                {
                    "message_id": str(self._attempt.assistant_message_id),
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "timing": {
                        "total_ms": duration,
                        "retrieval_ms": duration,
                        "generation_ms": 0,
                    },
                    "persisted": True,
                },
            )
        )
        # One protected ASGI write: no authorization gap between a title,
        # claim, and its citations, even for a slow or disconnected client.
        return b"".join(events)

    def _error(self, code: str, status: int) -> bytes:
        return _event(
            2,
            "error",
            {
                "type": f"urn:uas:problem:{code.lower().replace('_', '-')}",
                "title": "Conversation stream could not finish",
                "status": status,
                "code": code,
                "request_id": str(self._correlation_id),
                "retryable": True,
            },
        )


def _event(identifier: int, event: str, data: dict[str, object]) -> bytes:
    body = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return f"id: {identifier:08x}\nevent: {event}\ndata: {body}\n\n".encode()


@router.post(
    "/{conversation_id}/messages:stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_conversation_message(
    conversation_id: CanonicalUUID,
    body: StreamRequest,
    request: Request,
    principal: PrincipalDependency,
    workspace: WorkspaceDependency,
    dependencies: StreamDependencies,
    keys: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None,
) -> StreamingResponse:
    scope = _scope(principal, workspace)
    await _require_csrf(request, principal.mode)
    if keys and len(keys) != 1:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="Idempotency key is invalid",
        )
    key = validate_idempotency_key(keys[0] if keys else None)
    store, repository = dependencies
    try:
        attempt = await store.reserve(
            scope=scope,
            conversation_id=conversation_id,
            client_message_id=body.client_message_id,
            retry_user_message_id=body.retry_user_message_id,
            content=body.content,
            options={
                "providers": list(body.providers),
                "filters": body.filters.model_dump(mode="json"),
            },
            idempotency_key=key,
        )
    except (ConversationDenied, ConversationMissing):
        raise _unavailable() from None
    except ConversationKeyConflict:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
            title="Idempotency key was already used",
        ) from None
    except ConversationBusy:
        raise ProblemError(
            status=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            title="Request is already in progress",
            retryable=True,
            headers={"Retry-After": "2"},
        ) from None
    except StreamAlreadyProcessed:
        raise ProblemError(
            status=409,
            code="STREAM_ALREADY_PROCESSED",
            title="Streaming response cannot be replayed",
            detail="Open the conversation to see the saved messages.",
        ) from None
    except StreamRetryUnavailable:
        raise ProblemError(
            status=409,
            code="CONVERSATION_RETRY_UNAVAILABLE",
            title="This message cannot be retried",
        ) from None
    except UserTurnInputError:
        raise ProblemError(
            status=422, code="VALIDATION_ERROR", title="Message request is invalid"
        ) from None
    except SQLAlchemyError:
        raise ProblemError(
            status=503,
            code="CONVERSATION_STREAM_UNAVAILABLE",
            title="Conversation streaming is unavailable",
            retryable=True,
        ) from None
    return BoundedEventResponse(
        ConversationStream(
            store=store,
            repository=repository,
            scope=scope,
            conversation_id=conversation_id,
            attempt=attempt,
            filters=body.search_filters(),
            correlation_id=request_id_for(request),
            access_expires_at=principal.access_expires_at,
        )
    )
