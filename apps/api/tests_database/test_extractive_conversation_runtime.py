from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import ClientDisconnect
from starlette.types import Message
from test_conversation_repository import seed_user
from uas_connector_sdk import RawItem
from uas_connector_sdk.testing import FakeConnector

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.routes.conversation_stream import (
    BoundedEventResponse,
    ConversationStream,
    router,
)
from universal_ai_search.conversations.attempts import StreamAttempt, StreamAttemptStore
from universal_ai_search.conversations.extracts import (
    CLAIM_MARKER,
    EXTRACTIVE_MODEL,
    EvidencePointer,
)
from universal_ai_search.conversations.repository import (
    ConversationRepository,
    ConversationScope,
)
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.planner import SearchFilters, SearchPlan
from universal_ai_search.search.repository import SearchRepository

QUERY = "retry algorithm"
SECRET_EXCERPT = "Use capped exponential backoff for the retry algorithm."
SECRET_TITLE = "Synthetic private retry policy"


def _expires() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


@dataclass(frozen=True)
class Data:
    scope: ConversationScope
    other: ConversationScope
    collection_id: UUID
    source_id: UUID
    conversation_id: UUID
    api_engine: AsyncEngine

    def search(self) -> SearchRepository:
        return SearchRepository(self.api_engine)

    def store(self) -> StreamAttemptStore:
        return StreamAttemptStore(self.api_engine, idempotency_secret=b"i" * 32)

    def body(self) -> dict[str, object]:
        return {"client_message_id": str(uuid4()), "content": QUERY}

    def nonce(self) -> dict[str, str]:
        return {
            "X-Workspace-ID": str(self.scope.workspace_id),
            "Idempotency-Key": f"fixture-{uuid4()}",
        }

    @property
    def path(self) -> str:
        return f"/v1/conversations/{self.conversation_id}/messages:stream"


@pytest.fixture
async def data(
    connection: psycopg.Connection[tuple[object, ...]],
) -> AsyncIterator[Data]:
    workspace, connection_id, collection_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Extractive fixture','free','active')""",
        (workspace,),
    )
    scope, other = seed_user(connection, workspace), seed_user(connection, workspace)
    connection.execute(
        """INSERT INTO app.connections
                 (id,workspace_id,owner_user_id,provider,display_label,status)
           VALUES (%s,%s,%s,'github','Synthetic GitHub','active')""",
        (connection_id, workspace, scope.user_id),
    )
    connection.execute(
        """INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)""",
        (workspace,),
    )
    connection.execute(
        """INSERT INTO app.source_collections
           (id,workspace_id,connection_id,provider_external_id,kind,name,
            selected,status,github_verified_at)
           VALUES (%s,%s,%s,'123','repository','Synthetic',true,'active',now())""",
        (collection_id, workspace, connection_id),
    )
    connection.commit()
    document = await FakeConnector().normalize(
        RawItem(
            external_id=f"repo:123:{uuid4()}:synthetic.md",
            payload={"title": SECRET_TITLE, "content": SECRET_EXCERPT},
        )
    )
    index = IndexRepository(database_dsn())
    enqueued = index.enqueue(workspace, connection_id, document)
    connection.execute(
        """INSERT INTO app.source_collection_memberships
           (workspace_id,connection_id,source_id,collection_id,relationship)
           VALUES (%s,%s,%s,%s,'direct')""",
        (workspace, connection_id, enqueued.source_id, collection_id),
    )
    connection.commit()
    assert IndexingRuntime(index, IndexingPipeline()).run_once("extractive-fixture")
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    try:
        item = await ConversationRepository(engine).create(
            scope=scope,
            title="Fixture conversation",
            key_hash=b"k" * 32,
            request_hash=b"h" * 32,
        )
        yield Data(scope, other, collection_id, enqueued.source_id, item.id, engine)
    finally:
        await engine.dispose()


class FakeIdentity:
    def __init__(self, scope: ConversationScope) -> None:
        self.scope = scope

    async def authenticate(self, _request: object) -> Principal:
        return Principal(
            subject_id=self.scope.user_id,
            mode=AuthenticationMode.BEARER,
            authorization_version=self.scope.user_version,
            session_id=self.scope.session_id,
            access_expires_at=_expires(),
        )

    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        if workspace_id != self.scope.workspace_id:
            return None
        return WorkspaceContext(
            workspace_id=workspace_id,
            user_id=principal.subject_id,
            role="member",
            authorization_version=self.scope.workspace_version,
        )


def _events(body: str) -> dict[str, dict[str, object]]:
    return {
        event.removeprefix("event: "): json.loads(payload.removeprefix("data: "))
        for block in body.strip().split("\n\n")
        if (parts := block.splitlines()) and not parts[0].startswith(":")
        for _, event, payload in [parts]
    }


async def test_sse_happy_path_replays_current_citations_only(
    connection: psycopg.Connection[tuple[object, ...]], data: Data
) -> None:
    app = create_app()
    identity = FakeIdentity(data.scope)
    app.state.authentication_backend = identity
    app.state.workspace_authorization_backend = identity
    app.state.conversation_stream_store = data.store()
    app.state.conversation_repository = ConversationRepository(data.api_engine)
    app.state.conversation_cursor_signer = CursorSigner(b"c" * 32)
    app.state.conversation_stream_search = data.search()
    app.include_router(router, prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://configured.test"
    ) as client:
        body, headers = data.body(), data.nonce()
        original = await client.post(data.path, headers=headers, json=body)
        assert original.status_code == 200
        found = _events(original.text)
        assert list(found) == [
            "message.started",
            "retrieval.completed",
            "claim.completed",
            "message.completed",
        ], original.text
        assert SECRET_EXCERPT in original.text
        assert SECRET_TITLE in original.text
        claim = found["claim.completed"]["claim"]
        cites = found["claim.completed"]["citations"]
        assert isinstance(claim, dict) and isinstance(cites, list)
        assert claim["citation_ids"] == [cites[0]["id"]]
        assert cites[0]["excerpt"] == SECRET_EXCERPT
        assert cites[0]["source_id"] == str(data.source_id)
        assert found["message.completed"]["persisted"] is True
        read = await client.get(
            f"/v1/conversations/{data.conversation_id}", headers=headers
        )
        assert read.status_code == 200
        items = read.json()["messages"]["items"]
        assert [item["role"] for item in items] == ["user", "assistant"]
        assert items[1]["content"] == found["claim.completed"]["markdown_fragment"]
        assert items[1]["claims"] == [claim]
        assert items[1]["citations"] == cites
        assert read.headers["cache-control"] == "private, no-store"
        assert (
            connection.execute(
                """SELECT m.content,m.model,m.status,cl.text,c.excerpt
               FROM app.messages m JOIN app.message_claims cl ON cl.message_id = m.id
               JOIN app.citations c ON c.message_id = m.id AND c.claim_id = cl.id
               WHERE m.conversation_id = %s""",
                (data.conversation_id,),
            ).fetchall()
            == [("", EXTRACTIVE_MODEL, "complete", CLAIM_MARKER, CLAIM_MARKER)]
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM app.api_idempotency_keys
               WHERE workspace_id = %s AND response_body IS NOT NULL""",
                (data.scope.workspace_id,),
            ).fetchone()
            == (0,)
        )

        replay = await client.post(data.path, headers=headers, json=body)
        replay_events = _events(replay.text)
        assert replay_events["message.started"] == found["message.started"]
        assert replay_events["claim.completed"] == found["claim.completed"]
        assert connection.execute(
            "SELECT count(*) FROM app.messages WHERE conversation_id = %s",
            (data.conversation_id,),
        ).fetchone() == (2,)

        identity.scope = data.other
        outside = await client.post(data.path, headers=headers, json=body)
        assert outside.status_code == 404
        assert SECRET_TITLE not in outside.text

        identity.scope = data.scope
        connection.execute(
            "UPDATE app.source_collections SET selected = false WHERE id = %s",
            (data.collection_id,),
        )
        connection.execute(
            """UPDATE app.workspaces
                 SET authorization_version = authorization_version + 1
               WHERE id = %s""",
            (data.scope.workspace_id,),
        )
        connection.commit()
        identity.scope = replace(
            data.scope, workspace_version=data.scope.workspace_version + 1
        )
        replay = await client.post(data.path, headers=headers, json=body)
        assert replay.status_code == 200
        assert "message.insufficient" in _events(replay.text)
        assert SECRET_EXCERPT not in replay.text and SECRET_TITLE not in replay.text
        assert (
            "claim.completed" not in replay.text
            and "message.completed" not in replay.text
        )
        read = await client.get(
            f"/v1/conversations/{data.conversation_id}", headers=headers
        )
        assistant = read.json()["messages"]["items"][1]
        assert assistant["status"] == "insufficient_evidence"
        assert assistant["content"] is None
        assert "claims" not in assistant and "citations" not in assistant
        assert SECRET_EXCERPT not in read.text and SECRET_TITLE not in read.text


async def _response(data: Data) -> BoundedEventResponse:
    store = data.store()
    attempt = await store.reserve(
        scope=data.scope,
        conversation_id=data.conversation_id,
        client_message_id=uuid4(),
        retry_user_message_id=None,
        content=QUERY,
        options={},
        idempotency_key=f"asgi-client-{uuid4()}",
    )
    return BoundedEventResponse(
        ConversationStream(
            store=store,
            repository=data.search(),
            scope=data.scope,
            correlation_id=uuid4(),
            conversation_id=data.conversation_id,
            attempt=attempt,
            filters=SearchFilters(),
            access_expires_at=_expires(),
        )
    )


async def _no_receive() -> Message:
    raise AssertionError("ASGI 2.4 uses send to signal disconnect")


async def test_actual_asgi_write_holds_grant_lock_until_handoff(
    data: Data,
) -> None:
    response = await _response(data)
    bodies: list[bytes] = []

    async def send(message: Message) -> None:
        body = message.get("body", b"")
        if b"event: claim.completed" in body:
            # No application workspace fence involved in this SQL attack test:
            # the exact deletion row itself must be locked across ASGI handoff.
            conn = await psycopg.AsyncConnection.connect(database_dsn())
            async with conn, conn.cursor() as cursor:
                await cursor.execute("SET LOCAL lock_timeout = '120ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await cursor.execute(
                        """UPDATE app.source_collections SET selected = false
                               WHERE id = %s""",
                        (data.collection_id,),
                    )
            assert b"event: retrieval.completed" in body
            assert b"event: message.completed" in body
        bodies.append(body)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, _no_receive, send)
    assert SECRET_EXCERPT.encode() in b"".join(bodies)


async def test_disconnect_marks_real_attempt_interrupted(
    connection: psycopg.Connection[tuple[object, ...]], data: Data
) -> None:
    response = await _response(data)

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("synthetic disconnect")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, _no_receive, send
        )
    assert (
        connection.execute(
            """SELECT role,status,content FROM app.messages WHERE conversation_id = %s
           ORDER BY created_at""",
            (data.conversation_id,),
        ).fetchall()
        == [("user", "complete", QUERY), ("assistant", "interrupted", "")]
    )
    assert connection.execute(
        "SELECT status FROM app.search_requests WHERE conversation_id = %s",
        (data.conversation_id,),
    ).fetchone() == ("cancelled",)


class RevokingStore(StreamAttemptStore):
    def __init__(self, fixture: Data) -> None:
        super().__init__(fixture.api_engine, idempotency_secret=b"i" * 32)
        self.collection_id = fixture.collection_id

    async def finish_with_pointers(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        attempt: StreamAttempt,
        candidates: tuple[EvidencePointer, ...],
        plan: SearchPlan,
        latency_ms: int,
    ) -> None:
        await super().finish_with_pointers(
            scope=scope,
            conversation_id=conversation_id,
            attempt=attempt,
            candidates=candidates,
            plan=plan,
            latency_ms=latency_ms,
        )
        # Act after the pointer-only commit, before any content disclosure.
        # Only change the exact provider scope, not the workspace version.
        peer = await psycopg.AsyncConnection.connect(database_dsn())
        async with peer:
            await peer.execute(
                "UPDATE app.source_collections SET selected = false WHERE id = %s",
                (self.collection_id,),
            )


async def test_revocation_between_commit_and_handoff_cannot_disclose(
    connection: psycopg.Connection[tuple[object, ...]], data: Data
) -> None:
    store = RevokingStore(data)
    attempt = await store.reserve(
        scope=data.scope,
        conversation_id=data.conversation_id,
        client_message_id=uuid4(),
        retry_user_message_id=None,
        content=QUERY,
        options={},
        idempotency_key=f"race-client-{uuid4()}",
    )
    stream = ConversationStream(
        store=store,
        repository=data.search(),
        scope=data.scope,
        correlation_id=uuid4(),
        conversation_id=data.conversation_id,
        attempt=attempt,
        filters=SearchFilters(),
        access_expires_at=_expires(),
    )
    result = b"".join([item async for item in stream.events()]).decode()
    assert SECRET_TITLE not in result and SECRET_EXCERPT not in result
    assert list(_events(result)) == [
        "message.started",
        "retrieval.completed",
        "message.insufficient",
    ]
    assert (
        connection.execute(
            """SELECT m.content,m.status,c.excerpt
           FROM app.messages m JOIN app.citations c ON c.message_id=m.id
           WHERE m.id=%s""",
            (attempt.assistant_message_id,),
        ).fetchone()
        == ("", "complete", CLAIM_MARKER)
    )


async def test_disconnect_during_positive_body_releases_proof_locks(
    connection: psycopg.Connection[tuple[object, ...]], data: Data
) -> None:
    response = await _response(data)
    attempted = False

    async def send(message: Message) -> None:
        nonlocal attempted
        if b"event: claim.completed" in message.get("body", b""):
            attempted = True
            raise OSError("synthetic disconnect during handoff")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            _no_receive,
            send,
        )
    assert attempted
    # If cancellation stranded the dedicated delivery transaction, this
    # bounded write would hang/fail on the previously locked grant row.
    connection.execute("SET LOCAL lock_timeout = '200ms'")
    connection.execute(
        "UPDATE app.source_collections SET selected = false WHERE id = %s",
        (data.collection_id,),
    )
    connection.commit()
    assert (
        connection.execute(
            """SELECT status,content FROM app.messages
           WHERE conversation_id = %s AND role = 'assistant'""",
            (data.conversation_id,),
        ).fetchone()
        == ("complete", "")
    )
