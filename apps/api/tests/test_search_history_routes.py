from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from test_conversation_routes import HEADERS, SESSION, USER, WORKSPACE, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.conversations.repository import ConversationDenied
from universal_ai_search.search.history import (
    CLEAR_CONFIRMATION,
    HISTORY_PATH,
    HistoryClearPending,
    HistoryEntry,
    HistoryKeyConflict,
)

SUGGESTIONS = "/v1/search/suggestions"
CONFIRMED = {**HEADERS, "X-Confirm-Action": CLEAR_CONFIRMATION}
NOW = datetime(2026, 11, 24, tzinfo=UTC)
RECENT = HistoryEntry(uuid4(), "Recent query", "results", "completed", 1, NOW, NOW)
OLDER = HistoryEntry(
    uuid4(), "Older query", "answer", "insufficient_evidence", 0, NOW, NOW
)


def client_factory() -> tuple[TestClient, SimpleNamespace, Auth]:
    app = create_app()
    auth = Auth()
    store = SimpleNamespace(
        list_owned=AsyncMock(return_value=[RECENT, OLDER]),
        suggest_owned=AsyncMock(return_value=["recent search"]),
        clear_owned=AsyncMock(),
    )
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.search_history_store = store
    app.state.conversation_cursor_signer = CursorSigner(b"c" * 32)
    return TestClient(app), store, auth


def test_suggestions_require_auth_and_bound_query_only_input() -> None:
    client, store, auth = client_factory()
    with client:
        bad_responses = [
            client.post(SUGGESTIONS, headers=HEADERS, json=body)
            for body in [
                {"prefix": "x" * 129},
                {"prefix": " "},
                {"prefix": "\u0000"},
                {"prefix": "valid", "limit": 11},
                {"prefix": "valid", "filter_types": ["title"]},
            ]
        ]
        assert all(response.status_code == 422 for response in bad_responses)
        store.suggest_owned.assert_not_awaited()
        valid = client.post(
            SUGGESTIONS,
            headers=HEADERS,
            json={"prefix": "  re ", "limit": 2, "filter_types": ["query"]},
        )
        auth.mode = AuthenticationMode.BROWSER
        browser_without_csrf = client.post(
            SUGGESTIONS, headers=HEADERS, json={"prefix": "recent"}
        )
        auth.mode = AuthenticationMode.DEVICE
        device = client.post(SUGGESTIONS, headers=HEADERS, json={"prefix": "recent"})
    assert valid.status_code == 200
    assert valid.json() == {
        "suggestions": [{"kind": "query", "value": "recent search"}]
    }
    assert valid.headers["cache-control"] == "private, no-store"
    assert browser_without_csrf.json()["code"] == "CSRF_FAILED"
    assert device.json()["code"] == "SESSION_INVALID"
    store.suggest_owned.assert_awaited_once()
    call = store.suggest_owned.await_args.kwargs
    assert call["prefix"] == "re "
    assert call["limit"] == 2
    assert call["scope"].user_id == USER
    assert call["scope"].workspace_id == WORKSPACE
    assert call["scope"].session_id == SESSION


def test_private_history_keyset_cursor_contains_no_text() -> None:
    client, store, _ = client_factory()
    with client:
        first = client.get(HISTORY_PATH, headers=HEADERS, params={"limit": 1})
        cursor = first.json()["page"]["next_cursor"]
        store.list_owned.return_value = [OLDER]
        next_page = client.get(
            HISTORY_PATH, headers=HEADERS, params={"limit": 1, "cursor": cursor}
        )
        assert store.list_owned.await_args.kwargs["after"] == (NOW, RECENT.id)
        tampered = client.get(
            HISTORY_PATH, headers=HEADERS, params={"limit": 1, "cursor": "x" + cursor}
        )
        store.list_owned.side_effect = ConversationDenied
        revoked = client.get(HISTORY_PATH, headers=HEADERS)
    assert first.headers["cache-control"] == "private, no-store"
    assert first.json()["items"][0]["query"] == RECENT.query
    assert first.json()["page"]["has_more"]
    assert next_page.json()["items"][0]["query"] == OLDER.query
    assert next_page.json()["page"] == {"next_cursor": None, "has_more": False}
    assert tampered.json()["code"] == "CURSOR_INVALID"
    assert revoked.status_code == 404


def test_clear_is_empty_and_checks_confirmation_key_and_csrf_first() -> None:
    client, store, auth = client_factory()
    request_id = uuid4()
    headers = {**CONFIRMED, "X-Request-ID": str(request_id)}
    with client:
        absent = client.delete(HISTORY_PATH, headers=HEADERS)
        dup = client.delete(
            HISTORY_PATH,
            headers=[*headers.items(), ("X-Confirm-Action", CLEAR_CONFIRMATION)],
        )
        missing_key = client.delete(
            HISTORY_PATH,
            headers={
                key: value for key, value in headers.items() if key != "Idempotency-Key"
            },
        )
        duplicate_key = client.delete(
            HISTORY_PATH,
            headers=[*headers.items(), ("Idempotency-Key", HEADERS["Idempotency-Key"])],
        )
        auth.mode = AuthenticationMode.BROWSER
        csrf = client.delete(HISTORY_PATH, headers=headers)
        store.clear_owned.assert_not_awaited()
        auth.mode = AuthenticationMode.BEARER
        success = client.delete(HISTORY_PATH, headers=headers)
        store.clear_owned.assert_awaited_once()
        assert store.clear_owned.await_args.kwargs["request_id"] == request_id
        store.clear_owned.side_effect = HistoryClearPending
        in_progress = client.delete(HISTORY_PATH, headers=headers)
        store.clear_owned.side_effect = HistoryKeyConflict
        key_conflict = client.delete(HISTORY_PATH, headers=headers)
    assert absent.json()["code"] == dup.json()["code"] == "CONFIRMATION_REQUIRED"
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert duplicate_key.json()["code"] == "IDEMPOTENCY_KEY_INVALID"
    assert csrf.json()["code"] == "CSRF_FAILED"
    assert success.status_code == 204 and success.content == b""
    assert success.headers["cache-control"] == "private, no-store"
    assert in_progress.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert in_progress.headers["retry-after"] == "2"
    assert key_conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
