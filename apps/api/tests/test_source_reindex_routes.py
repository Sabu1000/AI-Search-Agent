from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_conversation_routes import HEADERS, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes.source_reindex import router
from universal_ai_search.search.source_reindex import (
    SourceReindexBusy,
    SourceReindexConflict,
    SourceReindexMissing,
    SourceReindexReceipt,
    SourceReindexUnavailable,
)

WHEN = datetime(2026, 11, 24, tzinfo=UTC)
SOURCE = uuid4()
PATH = f"/v1/sources/{SOURCE}/reindex"
PENDING = SourceReindexReceipt(
    uuid4(), "pending", None, WHEN, None, None, WHEN + timedelta(hours=24)
)


def _client() -> tuple[TestClient, SimpleNamespace, Auth]:
    app = create_app()
    auth = Auth()
    store = SimpleNamespace(request=AsyncMock(return_value=PENDING))
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.source_reindex_store = store
    app.include_router(router, prefix="/v1")
    return TestClient(app), store, auth


def test_reindex_requires_idempotency_and_browser_csrf() -> None:
    client, store, auth = _client()
    with client:
        no_key = client.post(
            PATH, headers={k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
        )
        auth.mode = AuthenticationMode.BROWSER
        no_csrf = client.post(PATH, headers=HEADERS)
    assert no_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert no_csrf.json()["code"] == "CSRF_FAILED"
    store.request.assert_not_awaited()


def test_reindex_returns_only_private_operation_summary() -> None:
    client, store, _ = _client()
    with client:
        response = client.post(PATH, headers=HEADERS)
    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["location"] == f"/v1/operations/{PENDING.id}"
    assert response.json()["operation"]["type"] == "source_reindex"
    assert response.json()["operation"]["progress"] == {
        "completed": 0,
        "total": 1,
        "unit": "source",
    }
    assert str(SOURCE) not in response.text
    assert store.request.await_args.kwargs["source_id"] == SOURCE


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (SourceReindexMissing, 404, "SOURCE_NOT_FOUND"),
        (SourceReindexUnavailable, 409, "SOURCE_REINDEX_UNAVAILABLE"),
        (SourceReindexConflict, 409, "IDEMPOTENCY_KEY_REUSED"),
        (SourceReindexBusy, 409, "IDEMPOTENCY_IN_PROGRESS"),
    ],
)
def test_reindex_errors_not_disclosing_provider_data(
    error: type[Exception], status: int, code: str
) -> None:
    client, store, _ = _client()
    store.request.side_effect = error("private details")
    with client:
        response = client.post(PATH, headers=HEADERS)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private details" not in response.text
