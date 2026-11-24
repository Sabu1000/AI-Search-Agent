from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from test_conversation_routes import HEADERS, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes.source_deletion import router
from universal_ai_search.conversations.deletion import ConversationDeletion
from universal_ai_search.search.source_deletion import SourceDeletionUnavailable

WHEN = datetime(2026, 11, 24, tzinfo=UTC)
SOURCE = uuid4()
PATH = f"/v1/sources/{SOURCE}"
RECEIPT = ConversationDeletion(uuid4(), WHEN, WHEN, WHEN, WHEN + timedelta(hours=24))
CONFIRMED = {**HEADERS, "X-Confirm-Action": "delete-source"}


def _client() -> tuple[TestClient, SimpleNamespace, Auth]:
    app = create_app()
    auth = Auth()
    store = SimpleNamespace(delete=AsyncMock(return_value=RECEIPT))
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.source_deletion_store = store
    app.include_router(router, prefix="/v1")
    return TestClient(app), store, auth


def test_delete_enforces_a_single_exact_confirmation_key_and_browser_csrf() -> None:
    client, store, auth = _client()
    with client:
        absent = client.delete(PATH, headers=HEADERS)
        wrong = client.delete(PATH, headers={**HEADERS, "X-Confirm-Action": "DELETE"})
        duplicate = client.delete(
            PATH, headers=[*CONFIRMED.items(), ("X-Confirm-Action", "delete-source")]
        )
        no_key = client.delete(
            PATH, headers={k: v for k, v in CONFIRMED.items() if k != "Idempotency-Key"}
        )
        auth.mode = AuthenticationMode.BROWSER
        no_csrf = client.delete(PATH, headers=CONFIRMED)
    assert absent.status_code == wrong.status_code == duplicate.status_code == 400
    assert all(
        result.json()["code"] == "CONFIRMATION_REQUIRED"
        for result in (absent, wrong, duplicate)
    )
    assert no_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert no_csrf.json()["code"] == "CSRF_FAILED"
    store.delete.assert_not_awaited()


def test_delete_returns_a_private_receipt_but_not_for_unpurged_objects() -> None:
    client, store, _ = _client()
    with client:
        response = client.delete(PATH, headers=CONFIRMED)
        store.delete.side_effect = SourceDeletionUnavailable("internal object key")
        unavailable = client.delete(PATH, headers=CONFIRMED)
    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["location"] == f"/v1/operations/{RECEIPT.id}"
    assert response.json()["operation"]["status"] == "completed"
    assert response.json()["operation"]["type"] == "source_delete"
    assert str(SOURCE) not in response.text
    assert unavailable.status_code == 409
    assert unavailable.json()["code"] == "SOURCE_DELETION_UNAVAILABLE"
    assert "internal object key" not in unavailable.text
