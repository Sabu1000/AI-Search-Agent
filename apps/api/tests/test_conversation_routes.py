from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.routes.conversations import router
from universal_ai_search.conversations.repository import (
    Conversation,
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
    Message,
)

USER = uuid4()
WORKSPACE = uuid4()
SESSION = uuid4()
NOW = datetime(2026, 11, 24, tzinfo=UTC)
PATH = "/v1/conversations"
HEADERS = {"X-Workspace-ID": str(WORKSPACE), "Idempotency-Key": "fixture-request-001"}


class Auth:
    def __init__(self) -> None:
        self.mode = AuthenticationMode.BEARER

    async def authenticate(self, _request: object) -> Principal:
        return Principal(
            subject_id=USER,
            mode=self.mode,
            session_id=SESSION,
            authorization_version=2,
        )


class Workspace:
    async def authorize(
        self, *, principal: Principal, workspace_id: object
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=principal.subject_id,
            role="member",
            authorization_version=7,
        )


def client_factory() -> tuple[TestClient, SimpleNamespace, Conversation, Auth]:
    app = create_app()
    auth = Auth()
    item = Conversation(uuid4(), "Example conversation", NOW, NOW)
    repo = SimpleNamespace(
        create=AsyncMock(return_value=item),
        list_owned=AsyncMock(return_value=[item]),
        read=AsyncMock(return_value=(item, [])),
    )
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.conversation_repository = repo
    app.state.conversation_idempotency_key = b"i" * 32
    app.state.conversation_cursor_signer = CursorSigner(b"c" * 32)
    app.include_router(router, prefix="/v1")
    return TestClient(app), repo, item, auth


def test_create_binds_workspace_user_and_session_and_does_not_pass_raw_key() -> None:
    client, repo, item, _ = client_factory()
    with client:
        response = client.post(
            PATH, headers=HEADERS, json={"title": " Example conversation "}
        )
        missing_key = client.post(
            PATH, headers={"X-Workspace-ID": str(WORKSPACE)}, json={}
        )
        overlong = client.post(PATH, headers=HEADERS, json={"title": "x" * 501})
        nul = client.post(PATH, headers=HEADERS, json={"title": "invalid\0title"})
    assert response.status_code == 201
    assert response.headers["location"] == f"{PATH}/{item.id}"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["id"] == str(item.id)
    call = repo.create.await_args.kwargs
    assert call["scope"].workspace_id == WORKSPACE
    assert call["scope"].user_id == USER
    assert call["scope"].session_id == SESSION
    assert call["scope"].workspace_version == 7
    assert call["scope"].user_version == 2
    assert call["title"] == "Example conversation"
    assert len(call["key_hash"]) == len(call["request_hash"]) == 32
    assert HEADERS["Idempotency-Key"].encode() not in call["key_hash"]
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert overlong.status_code == nul.status_code == 422
    assert "invalid\\u0000title" not in nul.text


def test_create_checks_browser_csrf_and_rejects_non_session_principal() -> None:
    client, repo, _, auth = client_factory()
    with client:
        auth.mode = AuthenticationMode.BROWSER
        csrf = client.post(PATH, headers=HEADERS, json={})
        auth.mode = AuthenticationMode.DEVICE
        device = client.post(PATH, headers=HEADERS, json={})
    assert csrf.json()["code"] == "CSRF_FAILED"
    assert device.json()["code"] == "SESSION_INVALID"
    repo.create.assert_not_awaited()


def test_replay_outcomes_are_stable() -> None:
    client, repo, _, _ = client_factory()
    with client:
        repo.create.side_effect = ConversationBusy
        busy = client.post(PATH, headers=HEADERS)
        repo.create.side_effect = ConversationKeyConflict
        mismatch = client.post(PATH, headers=HEADERS)
        repo.create.side_effect = ConversationMissing
        missing = client.post(PATH, headers=HEADERS)
    assert busy.status_code == mismatch.status_code == 409
    assert busy.headers["retry-after"] == "2"
    assert busy.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert mismatch.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert missing.status_code == 404


def test_conversation_and_message_cursors_cannot_be_reused_across_contexts() -> None:
    client, repo, item, _ = client_factory()
    other = Conversation(uuid4(), "Older", NOW, NOW)
    first = Message(uuid4(), "user", "complete", "Owner text", NOW, NOW)
    second = Message(uuid4(), "assistant", "interrupted", None, NOW, None)
    repo.list_owned.side_effect = [[item, other], [other]]
    repo.read.return_value = (item, [first, second])
    detail_path = f"{PATH}/{item.id}"

    with client:
        page = client.get(PATH, headers=HEADERS, params={"limit": 1})
        cursor = page.json()["page"]["next_cursor"]
        next_page = client.get(
            PATH, headers=HEADERS, params={"limit": 1, "cursor": cursor}
        )
        misuse = client.get(detail_path, headers=HEADERS, params={"cursor": cursor})
        detail = client.get(detail_path, headers=HEADERS, params={"limit": 1})
        msg_cursor = detail.json()["messages"]["page"]["next_cursor"]
        another = client.get(
            f"{PATH}/{other.id}", headers=HEADERS, params={"cursor": msg_cursor}
        )
        malformed = client.get(PATH, headers=HEADERS, params={"cursor": "malformed"})
        invalid_filter = client.get(PATH, headers=HEADERS, params={"sort": "title"})
        oversized = client.get(detail_path, headers=HEADERS, params={"limit": 51})

    assert cursor
    assert page.json()["items"][0]["id"] == str(item.id)
    assert next_page.json()["items"][0]["id"] == str(other.id)
    assert repo.list_owned.await_args.kwargs["after"] == (NOW, item.id)
    assert misuse.json()["code"] == another.json()["code"] == "CURSOR_CONTEXT_MISMATCH"
    assert malformed.json()["code"] == "CURSOR_INVALID"
    assert invalid_filter.status_code == oversized.status_code == 422
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.json()["messages"]["items"][0]["content"] == "Owner text"
