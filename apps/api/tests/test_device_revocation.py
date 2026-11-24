from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_device_queries import DEVICE, HEADERS, fixture
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes import device_revocation
from universal_ai_search.devices.revocation import (
    DeviceCleanupUnsupported,
    DeviceRevocationBusy,
    DeviceRevocationConflict,
    DeviceRevocationReceipt,
    DeviceRevocationRejected,
    DeviceRevocationUnavailable,
)

REVOCATION_URL = f"/v1/devices/{DEVICE}"
DELETION_HEADERS = {
    **HEADERS,
    "X-Confirm-Action": "revoke-device",
    "Idempotency-Key": "private-device-revoke-key-1",
}
NOW = datetime(2026, 11, 24, tzinfo=UTC)


def app_with_revocation(monkeypatch: pytest.MonkeyPatch):
    app, _ = fixture()
    app.include_router(device_revocation.router, prefix="/v1")
    csrf = AsyncMock()
    monkeypatch.setattr(device_revocation, "_require_csrf", csrf)
    return app, csrf


def test_exact_confirmation_and_idempotency_are_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, csrf = app_with_revocation(monkeypatch)
    store = AsyncMock()
    app.state.device_revocation_store = store
    client = TestClient(app)
    for invalid in (
        {**HEADERS, "Idempotency-Key": DELETION_HEADERS["Idempotency-Key"]},
        {**DELETION_HEADERS, "X-Confirm-Action": "REVOKE-DEVICE"},
    ):
        response = client.delete(REVOCATION_URL, headers=invalid)
        assert response.status_code == 400
        assert response.json()["code"] == "CONFIRMATION_REQUIRED"
    response = client.delete(
        REVOCATION_URL,
        headers=[*DELETION_HEADERS.items(), ("X-Confirm-Action", "revoke-device")],
    )
    assert response.status_code == 400
    response = client.delete(
        REVOCATION_URL, headers={**HEADERS, "X-Confirm-Action": "revoke-device"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert csrf.await_count == 4
    store.delete.assert_not_awaited()


def test_completed_receipt_has_no_device_credentials_or_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = app_with_revocation(monkeypatch)
    store = AsyncMock()
    receipt_id = uuid4()
    store.delete.return_value = DeviceRevocationReceipt(receipt_id, NOW, NOW, NOW, NOW)
    app.state.device_revocation_store = store
    response = TestClient(app).delete(REVOCATION_URL, headers=DELETION_HEADERS)
    assert response.status_code == 202
    assert response.headers["location"] == f"/v1/operations/{receipt_id}"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["operation"]["status"] == "completed"
    assert response.json()["operation"]["progress"] == {
        "completed": 1,
        "total": 1,
        "unit": "device",
    }
    assert str(DEVICE) not in response.text
    assert DELETION_HEADERS["Idempotency-Key"] not in response.text


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (DeviceRevocationRejected, 404, "DEVICE_NOT_FOUND"),
        (DeviceCleanupUnsupported, 409, "DEVICE_CLEANUP_UNAVAILABLE"),
        (DeviceRevocationConflict, 409, "IDEMPOTENCY_KEY_REUSED"),
        (DeviceRevocationBusy, 409, "IDEMPOTENCY_IN_PROGRESS"),
        (DeviceRevocationUnavailable, 503, "DEVICE_REVOCATION_UNAVAILABLE"),
    ],
)
def test_storage_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception], status: int, code: str
) -> None:
    app, _ = app_with_revocation(monkeypatch)
    store = AsyncMock()
    store.delete.side_effect = failure("private detail")
    app.state.device_revocation_store = store
    response = TestClient(app).delete(REVOCATION_URL, headers=DELETION_HEADERS)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private detail" not in response.text


def test_fails_closed_if_not_configured_or_browser_uses_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = app_with_revocation(monkeypatch)
    client = TestClient(app)
    assert client.delete(REVOCATION_URL, headers=DELETION_HEADERS).status_code == 503
    app.state.authentication_backend.mode = AuthenticationMode.BEARER
    app.state.device_revocation_store = AsyncMock()
    response = client.delete(REVOCATION_URL, headers=DELETION_HEADERS)
    # Ambient cookies are not allowed when using bearer auth.
    assert response.status_code == 403


async def test_store_executes_cutoff_and_receipt_under_verified_scope() -> None:
    from unittest.mock import MagicMock

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncEngine

    from test_device_queries import SCOPE
    from universal_ai_search.devices.revocation import DeviceRevocationStore

    connection = AsyncMock()
    engine = MagicMock(spec=AsyncEngine)
    engine.begin.return_value.__aenter__.return_value = connection
    receipt_id, connection_id, source_id = (uuid4() for _ in range(3))
    receipt_row = {
        "id": receipt_id,
        "requested_at": NOW,
        "started_at": NOW,
        "completed_at": NOW,
        "deadline_at": NOW,
    }

    async def execute(statement, params=None):
        del params
        query = str(statement)
        rows = MagicMock()
        if "SELECT request_hash" in query:
            rows.mappings.return_value.one_or_none.return_value = None
        if "SELECT c.id, c.provider" in query:
            rows.mappings.return_value.one_or_none.return_value = {
                "id": connection_id,
                "provider": "local_files",
                "owner_user_id": SCOPE.user_id,
            }
        if "SELECT id, provider, current_document_version_id" in query:
            rows.mappings.return_value.all.return_value = [
                {
                    "id": source_id,
                    "provider": "local_files",
                    "current_document_version_id": uuid4(),
                }
            ]
        if "AS external_content" in query:
            rows.mappings.return_value.one.return_value = {
                "external_content": False,
                "unknown_jobs": False,
                "chunks": 1,
                "versions": 1,
                "jobs": 1,
                "citations": 0,
                "extracted_bytes": 120,
            }
        if "INSERT INTO app.deletion_requests" in query:
            rows.mappings.return_value.one.return_value = receipt_row
        if "FROM app.deletion_requests r" in query:
            rows.mappings.return_value.one_or_none.return_value = receipt_row
        return rows

    async def scalar(statement, params=None):
        del params
        query = str(statement)
        if "SELECT EXISTS (SELECT 1 FROM app.devices" in query:
            return False
        if "SELECT count(*) FROM (SELECT 1 FROM " in query:
            return 1
        return True

    connection.execute.side_effect = execute
    connection.scalar.side_effect = scalar
    store = DeviceRevocationStore(
        engine, idempotency_secret=b"k" * 32, audit_secret=b"a" * 32
    )
    receipt = await store.delete(
        scope=SCOPE,
        device_id=DEVICE,
        idempotency_key="fixture-key-transaction",
        request_id=uuid4(),
    )
    assert receipt.id == receipt_id
    assert await store.read(scope=SCOPE, receipt_id=receipt_id) == receipt
    queries = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert any("status='revoked'" in query for query in queries)
    assert any("DELETE FROM app.sources" in query for query in queries)
    assert any(
        "app.api_idempotency_keys AS entry" in str(call.args[0])
        for call in connection.scalar.await_args_list
    )

    connection.scalar.side_effect = SQLAlchemyError("database provider detail")
    with pytest.raises(DeviceRevocationUnavailable, match="^$"):
        await store.delete(
            scope=SCOPE,
            device_id=DEVICE,
            idempotency_key="fixture-key-unavailable",
            request_id=uuid4(),
        )
    with pytest.raises(DeviceRevocationUnavailable, match="^$"):
        await store.read(scope=SCOPE, receipt_id=receipt_id)
