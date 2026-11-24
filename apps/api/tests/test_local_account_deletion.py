from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from test_conversation_routes import HEADERS, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes.local_account_deletion import router
from universal_ai_search.auth.local_account_deletion import (
    IssuedLocalAccountDeletion,
    LocalAccountDeletion,
    LocalAccountDeletionReceiptInvalid,
    LocalAccountDeletionUnsupported,
)

NOW = datetime.now(UTC)
DELETION = LocalAccountDeletion(uuid4(), NOW, NOW + timedelta(hours=24), NOW)
TOKEN = "a" * 43
AUTHORIZATION = {"Authorization": f"DeletionReceipt {TOKEN}"}
BODY = {"confirmation": "DELETE MY ACCOUNT"}
REQUIRED = {
    **HEADERS,
    "X-Confirm-Action": "delete-account",
    "X-Reauth-Token": "z" * 43,
}
PATH = "/v1/account"


def fixture() -> tuple[TestClient, SimpleNamespace, Auth]:
    app = create_app()
    store = SimpleNamespace(
        delete=AsyncMock(return_value=IssuedLocalAccountDeletion(DELETION, TOKEN)),
        read_receipt=AsyncMock(return_value=DELETION),
    )
    auth = Auth()
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.local_account_deletion_store = store
    app.include_router(router, prefix="/v1")
    return TestClient(app), store, auth


def test_delete_requires_exact_confirmation_key_reauth_session_and_browser_csrf() -> (
    None
):
    client, store, auth = fixture()
    with client:
        wrong_body = client.request(
            "DELETE", PATH, headers=REQUIRED, json={"confirmation": "delete my account"}
        )
        missing_header = client.request("DELETE", PATH, headers=HEADERS, json=BODY)
        missing_proof = client.request(
            "DELETE",
            PATH,
            headers={**HEADERS, "X-Confirm-Action": "delete-account"},
            json=BODY,
        )
        missing_key = client.request(
            "DELETE",
            PATH,
            headers={
                key: value
                for key, value in REQUIRED.items()
                if key != "Idempotency-Key"
            },
            json=BODY,
        )
        auth.mode = AuthenticationMode.BROWSER
        csrf = client.request("DELETE", PATH, headers=REQUIRED, json=BODY)
        auth.mode = AuthenticationMode.DELETION_RECEIPT
        non_session = client.request("DELETE", PATH, headers=REQUIRED, json=BODY)
    assert wrong_body.status_code == 422
    assert missing_header.json()["code"] == "CONFIRMATION_REQUIRED"
    assert missing_proof.json()["code"] == "REAUTHENTICATION_REQUIRED"
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert csrf.json()["code"] == "CSRF_FAILED"
    assert non_session.json()["code"] == "SESSION_INVALID"
    store.delete.assert_not_awaited()


def test_issued_token_only_in_initial_response_and_status_is_capability_only() -> None:
    client, store, _ = fixture()
    with client:
        result = client.request("DELETE", PATH, headers=REQUIRED, json=BODY)
        absent = client.get(f"{PATH}/deletion-status", headers=HEADERS)
        invalid = client.get(
            f"{PATH}/deletion-status", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        store.read_receipt.assert_not_awaited()
        status = client.get(f"{PATH}/deletion-status", headers=AUTHORIZATION)
        store.read_receipt.side_effect = LocalAccountDeletionReceiptInvalid
        expired = client.get(f"{PATH}/deletion-status", headers=AUTHORIZATION)
        store.delete.side_effect = LocalAccountDeletionUnsupported
        unsupported = client.request("DELETE", PATH, headers=REQUIRED, json=BODY)
    assert result.status_code == 202
    assert result.json()["deletion_receipt_token"] == TOKEN
    assert result.headers["cache-control"] == "private, no-store"
    assert result.json()["deletion_request_id"] == str(DELETION.id)
    assert absent.status_code == invalid.status_code == expired.status_code == 401
    assert status.status_code == 200
    assert status.json()["deletion_request_id"] == str(DELETION.id)
    assert status.json()["remaining_counts"] == {"active_accounts": 0, "workspaces": 0}
    assert status.headers["cache-control"] == "private, no-store"
    assert TOKEN not in status.text + expired.text
    assert "active storage" in status.json()["support_guidance"]
    assert unsupported.json()["code"] == "ACCOUNT_DELETION_REQUIRES_SUPPORT"
