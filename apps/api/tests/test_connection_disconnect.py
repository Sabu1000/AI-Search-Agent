from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from test_conversation_routes import HEADERS, NOW, SESSION, USER, WORKSPACE, Auth
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.routes.connection_disconnect import (
    completed_response,
    router,
)
from universal_ai_search.auth.reauth import ReauthenticationFailed
from universal_ai_search.auth.reauth_store import ReauthScope
from universal_ai_search.connections.disconnect import (
    DisconnectBusy,
    DisconnectConflict,
    DisconnectMissing,
    DisconnectReceipt,
    DisconnectUnsupported,
)

TARGET = uuid4()
RECEIPT = DisconnectReceipt(
    uuid4(), "completed", NOW, NOW, NOW, NOW + timedelta(days=1)
)
PATH = f"/v1/connections/{TARGET}"
PROOF = "a" * 43
AUTH_HEADERS = {**HEADERS, "X-Confirm-Action": "disconnect", "X-Reauth-Token": PROOF}


class ManagerWorkspace:
    role = "owner"

    async def authorize(
        self, *, principal: Principal, workspace_id: object
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=principal.subject_id,
            role=self.role,
            authorization_version=7,
        )


def fixture() -> tuple[TestClient, Auth, ManagerWorkspace, AsyncMock]:
    app = create_app()
    auth, workspace = Auth(), ManagerWorkspace()
    delete = AsyncMock(return_value=RECEIPT)
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = workspace
    app.state.connection_disconnect_store = SimpleNamespace(delete=delete)
    app.include_router(router, prefix="/v1")
    return TestClient(app), auth, workspace, delete


def test_disconnect_returns_only_a_completed_local_receipt() -> None:
    client, _, _, delete = fixture()
    response = client.delete(PATH, headers=AUTH_HEADERS)
    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["location"] == f"/v1/operations/{RECEIPT.id}"
    operation = response.json()["operation"]
    assert operation["type"] == "connection_delete"
    assert operation["status"] == "completed"
    assert operation["progress"] == {"completed": 1, "total": 1, "unit": "connection"}
    kwargs = delete.await_args.kwargs
    assert kwargs["scope"] == ReauthScope(USER, SESSION, WORKSPACE, 2, 7)
    assert kwargs["connection_id"] == TARGET
    assert kwargs["reauth_token"] == PROOF
    assert "allow_pending" not in kwargs
    assert PROOF not in response.text


@pytest.mark.parametrize(
    ("modified", "status"),
    [
        ({"X-Confirm-Action": "Disconnect"}, 400),
        ({"X-Confirm-Action": "disconnect, disconnect"}, 400),
        ({"X-Reauth-Token": None}, 401),
        ({"Idempotency-Key": None}, 400),
    ],
)
def test_disconnect_requires_exact_confirmation_and_all_headers(
    modified: dict[str, str | None], status: int
) -> None:
    client, _, _, delete = fixture()
    headers = [(k, v) for k, v in (AUTH_HEADERS | modified).items() if v is not None]
    assert client.delete(PATH, headers=headers).status_code == status
    delete.assert_not_awaited()


def test_duplicate_headers_are_never_accepted() -> None:
    client, _, _, delete = fixture()
    for name, status in (
        ("X-Confirm-Action", 400),
        ("X-Reauth-Token", 401),
        ("Idempotency-Key", 400),
    ):
        headers = [*AUTH_HEADERS.items(), (name, AUTH_HEADERS[name])]
        assert client.delete(PATH, headers=headers).status_code == status
    delete.assert_not_awaited()


def test_disconnect_rejects_members_devices_and_browser_without_csrf() -> None:
    client, auth, workspace, delete = fixture()
    workspace.role = "member"
    assert client.delete(PATH, headers=AUTH_HEADERS).status_code == 404
    workspace.role = "admin"
    auth.mode = AuthenticationMode.DEVICE
    assert client.delete(PATH, headers=AUTH_HEADERS).status_code == 404
    auth.mode = AuthenticationMode.BROWSER
    assert client.delete(PATH, headers=AUTH_HEADERS).status_code == 403
    delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ReauthenticationFailed(), 401, "REAUTHENTICATION_REQUIRED"),
        (DisconnectMissing(), 404, "CONNECTION_NOT_FOUND"),
        (DisconnectUnsupported(), 409, "DISCONNECT_UNSUPPORTED"),
        (DisconnectConflict(), 409, "IDEMPOTENCY_KEY_REUSED"),
        (DisconnectBusy(), 409, "OPERATION_IN_PROGRESS"),
        (SQLAlchemyError("synthetic private trace"), 503, "DISCONNECT_UNAVAILABLE"),
        (
            DBAPIError("private", {}, Exception("private")),
            503,
            "DISCONNECT_UNAVAILABLE",
        ),
    ],
)
def test_disconnect_maps_only_safe_errors(
    error: Exception, status: int, code: str
) -> None:
    client, _, _, delete = fixture()
    delete.side_effect = error
    response = client.delete(PATH, headers=AUTH_HEADERS)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "synthetic private trace" not in response.text
    assert PROOF not in response.text


def test_pending_receipt_cannot_be_projected_as_complete() -> None:
    with pytest.raises(ValueError, match="Incomplete"):
        completed_response(
            DisconnectReceipt(uuid4(), "pending", NOW, None, None, RECEIPT.deadline_at)
        )
