from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from test_conversation_routes import HEADERS, PATH, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes.conversation_deletion import (
    operations_router,
    router,
)
from universal_ai_search.conversations.deletion import ConversationDeletion
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
)

WHEN = datetime(2026, 11, 24, tzinfo=UTC)
CONVERSATION_ID = uuid4()
ROUTE = f"{PATH}/{CONVERSATION_ID}"
CONFIRMED = {**HEADERS, "X-Confirm-Action": "delete-conversation"}
RECEIPT = ConversationDeletion(uuid4(), WHEN, WHEN, WHEN, WHEN + timedelta(hours=24))


def client_factory() -> tuple[TestClient, SimpleNamespace, Auth]:
    app = create_app()
    auth = Auth()
    store = SimpleNamespace(
        delete=AsyncMock(return_value=RECEIPT), read=AsyncMock(return_value=RECEIPT)
    )
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.conversation_deletion_store = store
    app.include_router(router, prefix="/v1")
    app.include_router(operations_router, prefix="/v1")
    return TestClient(app), store, auth


def test_exact_confirmation_required_before_writing() -> None:
    client, store, auth = client_factory()
    with client:
        absent = client.delete(ROUTE, headers=HEADERS)
        bad = client.delete(ROUTE, headers={**HEADERS, "X-Confirm-Action": "Delete"})
        repeated = client.delete(
            ROUTE,
            headers=[
                *HEADERS.items(),
                ("X-Confirm-Action", "delete-conversation"),
                ("X-Confirm-Action", "delete-conversation"),
            ],
        )
        no_key = client.delete(
            ROUTE,
            headers={
                key: value
                for key, value in CONFIRMED.items()
                if key != "Idempotency-Key"
            },
        )
        auth.mode = AuthenticationMode.BROWSER
        missing_csrf = client.delete(ROUTE, headers=CONFIRMED)
    assert absent.status_code == bad.status_code == repeated.status_code == 400
    assert absent.json()["code"] == bad.json()["code"] == "CONFIRMATION_REQUIRED"
    assert no_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert missing_csrf.json()["code"] == "CSRF_FAILED"
    store.delete.assert_not_awaited()


def test_completed_receipt_is_content_free_and_has_a_private_status_endpoint() -> None:
    client, store, _ = client_factory()
    request_id = uuid4()
    with client:
        deleted = client.delete(
            ROUTE, headers={**CONFIRMED, "X-Request-ID": str(request_id)}
        )
        found = client.get(deleted.headers["location"], headers=HEADERS)
        store.read.side_effect = ConversationMissing
        missing = client.get(deleted.headers["location"], headers=HEADERS)
    assert deleted.status_code == 202
    assert deleted.headers["cache-control"] == "private, no-store"
    operation = deleted.json()["operation"]
    assert operation["id"] == str(RECEIPT.id)
    assert operation["status"] == "completed"
    assert operation["type"] == "conversation_delete"
    assert operation["links"]["self"] == f"/v1/operations/{RECEIPT.id}"
    assert str(CONVERSATION_ID) not in deleted.text
    assert found.status_code == 200
    assert found.json() == deleted.json()
    assert store.delete.await_args.kwargs["request_id"] == request_id
    assert missing.status_code == 404
    assert missing.json()["code"] == "OPERATION_NOT_FOUND"


def test_replay_conflicts_and_unavailable_conversations_are_not_disclosed() -> None:
    client, store, _ = client_factory()
    with client:
        store.delete.side_effect = ConversationBusy
        busy = client.delete(ROUTE, headers=CONFIRMED)
        store.delete.side_effect = ConversationKeyConflict
        reused = client.delete(ROUTE, headers=CONFIRMED)
        store.delete.side_effect = ConversationMissing
        missing = client.delete(ROUTE, headers=CONFIRMED)
    assert busy.status_code == reused.status_code == 409
    assert busy.headers["retry-after"] == "2"
    assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert missing.status_code == 404
    assert missing.json()["code"] == "CONVERSATION_NOT_FOUND"
