from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect
from starlette.types import Message

from test_conversation_routes import HEADERS, SESSION, USER, WORKSPACE, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal
from universal_ai_search.api.routes import conversation_stream
from universal_ai_search.conversations.attempts import (
    StreamAlreadyProcessed,
    StreamAttempt,
)
from universal_ai_search.conversations.repository import (
    ConversationMissing,
    ConversationScope,
)
from universal_ai_search.search.planner import SearchFilters

CONVERSATION = uuid4()
ENDPOINT = f"/v1/conversations/{CONVERSATION}/messages:stream"
ATTEMPT = StreamAttempt(uuid4(), uuid4(), uuid4(), "Private user query")
BODY = {"client_message_id": str(uuid4()), "content": ATTEMPT.query}


class CurrentAuth(Auth):
    async def authenticate(self, request: object) -> Principal:
        principal = await super().authenticate(request)
        return principal.model_copy(
            update={"access_expires_at": datetime.now(UTC) + timedelta(minutes=5)}
        )


def fixture() -> tuple[TestClient, SimpleNamespace, SimpleNamespace, Auth]:
    app = create_app()
    auth = CurrentAuth()
    store = SimpleNamespace(
        reserve=AsyncMock(return_value=ATTEMPT),
        finish=AsyncMock(),
        finish_with_pointers=AsyncMock(),
    )
    repo = SimpleNamespace(
        retrieve=AsyncMock(return_value=SimpleNamespace(lanes={"exact": ()})),
        revalidate_candidates=AsyncMock(return_value={}),
    )
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.conversation_stream_store = store
    app.state.conversation_stream_search = repo
    app.include_router(conversation_stream.router, prefix="/v1")
    return TestClient(app), store, repo, auth


def test_preflight_uses_normal_problems_not_sse() -> None:
    client, store, _, auth = fixture()
    with client:
        bad = client.post(
            ENDPOINT,
            headers=HEADERS,
            json={**BODY, "retry_user_message_id": str(uuid4())},
        )
        auth.mode = AuthenticationMode.BROWSER
        csrf = client.post(ENDPOINT, headers=HEADERS, json=BODY)
        assert bad.status_code == 422
        assert csrf.json()["code"] == "CSRF_FAILED"
        store.reserve.assert_not_awaited()
        auth.mode = AuthenticationMode.BEARER
        store.reserve.side_effect = ConversationMissing
        denied = client.post(ENDPOINT, headers=HEADERS, json=BODY)
        store.reserve.side_effect = StreamAlreadyProcessed
        replayed = client.post(ENDPOINT, headers=HEADERS, json=BODY)
    assert denied.status_code == 404
    assert replayed.status_code == 409
    assert replayed.json()["code"] == "STREAM_ALREADY_PROCESSED"
    assert replayed.headers["content-type"].startswith("application/problem+json")
    assert ATTEMPT.query not in bad.text + replayed.text + denied.text


def test_real_empty_branch_persists_before_saying_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, repo, _ = fixture()

    async def delayed(**_: object) -> SimpleNamespace:
        await asyncio.sleep(0.02)
        return SimpleNamespace(lanes={})

    repo.retrieve.side_effect = delayed
    monkeypatch.setattr(conversation_stream, "HEARTBEAT_SECONDS", 0.001)
    with client:
        result = client.post(ENDPOINT, headers=HEADERS, json=BODY)
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert result.headers["x-accel-buffering"] == "no"
    assert result.headers["cache-control"] == "no-store"
    assert ": keepalive" in result.text
    assert [
        line for line in result.text.splitlines() if line.startswith("event: ")
    ] == [
        "event: message.started",
        "event: retrieval.completed",
        "event: message.insufficient",
    ]
    assert ATTEMPT.query not in result.text
    assert str(ATTEMPT.assistant_message_id) in result.text
    store.finish.assert_awaited_once()
    assert store.finish.await_args.kwargs["status"] == "insufficient_evidence"


def test_uncommitted_candidate_content_and_private_errors_never_appear_in_sse() -> None:
    client, store, repo, _ = fixture()
    with client:
        candidate = SimpleNamespace(
            content="private provider extract",
            chunk_id=uuid4(),
            source_id=uuid4(),
            raw_score=1.0,
            chunk_hash=b"test",
            timestamp=None,
        )
        repo.retrieve.return_value = SimpleNamespace(lanes={"exact": (candidate,)})
        store.finish_with_pointers.side_effect = SQLAlchemyError("private SQL data")
        positive = client.post(ENDPOINT, headers=HEADERS, json=BODY)
        repo.retrieve.return_value = SimpleNamespace(lanes={})
        empty = client.post(ENDPOINT, headers=HEADERS, json=BODY)
        repo.retrieve.side_effect = RuntimeError("private infrastructure detail")
        unavailable = client.post(ENDPOINT, headers=HEADERS, json=BODY)
    assert "event: error" in positive.text
    assert "CONVERSATION_STREAM_UNAVAILABLE" in positive.text
    assert "CONVERSATION_STREAM_UNAVAILABLE" in unavailable.text
    assert "event: message.insufficient" in empty.text
    for hidden in (
        "event: claim.completed",
        "event: message.completed",
        "private provider extract",
        "private infrastructure detail",
        "private SQL data",
    ):
        assert hidden not in positive.text + empty.text + unavailable.text
    assert [call.kwargs["status"] for call in store.finish.await_args_list] == [
        "failed",
        "insufficient_evidence",
        "failed",
    ]


async def test_disconnect_before_first_event_cleans_up_pending_row() -> None:
    _, store, repo, _ = fixture()
    stream = conversation_stream.ConversationStream(
        store=store,
        repository=repo,
        scope=ConversationScope(WORKSPACE, USER, SESSION, 7, 2),
        conversation_id=CONVERSATION,
        attempt=ATTEMPT,
        filters=SearchFilters(),
        correlation_id=uuid4(),
    )
    response = conversation_stream.BoundedEventResponse(stream)

    async def send(_message: Message) -> None:
        raise OSError("disconnected")

    async def receive() -> Message:
        raise AssertionError("ASGI 2.4 reports send disconnects")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    repo.retrieve.assert_not_awaited()
    store.finish.assert_awaited_once()
    assert store.finish.await_args.kwargs["status"] == "interrupted"
