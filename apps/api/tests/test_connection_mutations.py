from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    WorkspaceContext,
    require_principal,
    require_workspace,
)
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.connection_mutations import router
from universal_ai_search.connections.mutations import (
    ConnectionMutationBusy,
    ConnectionMutationDenied,
    ConnectionMutationKeyConflict,
    ConnectionMutationMissing,
    ConnectionMutationNotSupported,
    ConnectionMutationStale,
    ConnectionSelectionInvalid,
    SyncReceipt,
    mutation_etag,
)
from universal_ai_search.connections.query import ConnectionResponse, ConnectionSnapshot

WORKSPACE, USER, SESSION, CONNECTION, COLLECTION = (uuid4() for _ in range(5))
URL = f"/v1/connections/{CONNECTION}"
KEY = "synthetic-test-key"
NOW = datetime.now(UTC)


@pytest.fixture()
def context() -> SimpleNamespace:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    install_problem_handlers(app)
    state = SimpleNamespace(
        mode=AuthenticationMode.BEARER, session=SESSION, role="owner"
    )
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject_id=USER,
        mode=state.mode,
        session_id=state.session,
        authorization_version=3,
    )
    app.dependency_overrides[require_workspace] = lambda: WorkspaceContext(
        workspace_id=WORKSPACE,
        user_id=USER,
        role=state.role,
        authorization_version=7,
    )
    selected = ConnectionResponse(
        id=CONNECTION,
        status="active",
        provider="github",
        display_label="GitHub",
        scopes=["contents:read"],
        selected_collections_truncated=False,
        selected_collections=[],
        created_at=NOW,
        updated_at=NOW,
        last_successful_sync_at=None,
        current_operation=None,
        error_code=None,
        recovery_action=None,
    )
    state.receipt = SyncReceipt(uuid4(), NOW, "pending", None)
    state.store = app.state.connection_mutation_store = SimpleNamespace(
        sync=AsyncMock(return_value=state.receipt),
        narrow=AsyncMock(return_value=ConnectionSnapshot(selected, 6)),
    )
    state.client = TestClient(app)
    return state


def test_sync_returns_real_operation_url_and_authorization_scope(
    context: SimpleNamespace,
) -> None:
    response = context.client.post(f"{URL}/sync", headers={"Idempotency-Key": KEY})
    path = f"/v1/operations/{context.receipt.id}"
    assert response.status_code == 202
    assert response.headers["location"] == path
    value = response.json()["operation"]
    assert value["links"]["self"] == path and value["type"] == "sync"
    assert value["status"] == "pending" and value["progress"]["total"] is None
    assert response.headers["cache-control"] == "private, no-store"
    arguments = context.store.sync.call_args.kwargs
    assert arguments["idempotency_key"] == KEY
    assert arguments["scope"].workspace_version == 7
    assert arguments["scope"].user_version == 3
    assert arguments["scope"].session_id == SESSION


def test_selection_requires_one_exact_precondition_and_opaque_ids(
    context: SimpleNamespace,
) -> None:
    request_body = {"collection_ids": [str(COLLECTION)]}
    header = {"Idempotency-Key": KEY, "If-Match": mutation_etag(CONNECTION, 5)}
    missing = context.client.put(
        f"{URL}/selections", json=request_body, headers={"Idempotency-Key": KEY}
    )
    assert missing.status_code == 428 and missing.json()["code"] == "IF_MATCH_REQUIRED"
    extra = context.client.put(
        f"{URL}/selections",
        json={**request_body, "provider_scopes": ["write"]},
        headers=header,
    )
    assert extra.status_code == 422
    response = context.client.put(
        f"{URL}/selections", json=request_body, headers=header
    )
    assert response.status_code == 200
    assert response.headers["etag"] == mutation_etag(CONNECTION, 6)
    assert context.store.narrow.call_args.kwargs["collection_ids"] == (COLLECTION,)


def test_body_key_role_session_and_browser_csrf_are_not_optional(
    context: SimpleNamespace,
) -> None:
    headers = {"Idempotency-Key": KEY}
    assert context.client.post(f"{URL}/sync").status_code == 400
    assert (
        context.client.post(
            f"{URL}/sync",
            headers=headers,
            json={"mode": "write"},
        ).status_code
        == 422
    )
    assert (
        context.client.post(
            f"{URL}/sync", headers=[("Idempotency-Key", KEY), ("Idempotency-Key", KEY)]
        ).status_code
        == 400
    )
    context.role = "member"
    assert context.client.post(f"{URL}/sync", headers=headers).status_code == 403
    context.role = "owner"
    context.session = None
    assert context.client.post(f"{URL}/sync", headers=headers).status_code == 401
    context.session = SESSION
    context.mode = AuthenticationMode.BROWSER
    rejected = context.client.post(f"{URL}/sync", headers=headers)
    assert rejected.status_code == 403 and rejected.json()["code"] == "CSRF_FAILED"
    context.store.sync.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ConnectionMutationDenied(), 403, "CONNECTION_MANAGEMENT_FORBIDDEN"),
        (ConnectionMutationMissing(), 404, "CONNECTION_NOT_FOUND"),
        (ConnectionMutationBusy(), 409, "IDEMPOTENCY_IN_PROGRESS"),
        (ConnectionMutationKeyConflict(), 409, "IDEMPOTENCY_KEY_REUSED"),
        (ConnectionMutationNotSupported(), 409, "CONNECTION_MUTATION_UNSUPPORTED"),
        (ConnectionMutationStale(), 412, "RESOURCE_VERSION_MISMATCH"),
        (ConnectionSelectionInvalid(), 422, "CONNECTION_SELECTION_INVALID"),
    ],
)
def test_stable_error_mapping(
    context: SimpleNamespace, error: Exception, status: int, code: str
) -> None:
    context.store.sync.side_effect = error
    response = context.client.post(f"{URL}/sync", headers={"Idempotency-Key": KEY})
    assert response.status_code == status
    assert response.json()["code"] == code
    if code == "IDEMPOTENCY_IN_PROGRESS":
        assert response.headers["retry-after"] == "2"
