from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes import device_queries
from universal_ai_search.devices.query import (
    DeviceQueryDenied,
    DeviceQueryUnavailable,
    DeviceSummary,
    SQLAlchemyDeviceQueryReader,
)
from universal_ai_search.devices.registration import RegistrationScope

NOW = datetime(2026, 11, 24, tzinfo=UTC)
USER, WORKSPACE, SESSION, DEVICE = (uuid4() for _ in range(4))
PATH = "/v1/devices"
HEADERS = {"X-Workspace-ID": str(WORKSPACE), "Cookie": "uas-test=present"}
SCOPE = RegistrationScope(WORKSPACE, USER, SESSION, 2, 3)


def summary() -> DeviceSummary:
    return DeviceSummary(
        DEVICE, "MacBook", "macos", "1.0", "active", NOW, NOW, NOW, None
    )


class Auth:
    mode = AuthenticationMode.BROWSER

    async def authenticate(self, request: Request) -> Principal | None:
        if request.cookies.get("uas-test") != "present":
            return None
        return Principal(
            subject_id=USER,
            session_id=SESSION,
            mode=self.mode,
            authorization_version=2,
        )


class Membership:
    version = 3
    actor = USER

    async def authorize(
        self, *, principal: Principal, workspace_id: object
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=self.actor,
            role="member",
            authorization_version=self.version,
        )


def fixture(*, enabled: bool = True) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    reader = SimpleNamespace(
        get=AsyncMock(return_value=summary()), list_devices=AsyncMock(return_value=[])
    )
    app.state.authentication_backend = Auth()
    app.state.workspace_authorization_backend = Membership()
    if enabled:
        app.state.device_query_reader = reader
    app.state.device_cursor_signer = CursorSigner(b"d" * 32)
    install_problem_handlers(app)
    app.include_router(device_queries.router, prefix="/v1")
    return app, reader


def test_device_detail_requires_live_app_session_and_same_principal() -> None:
    app, reader = fixture()
    client = TestClient(app)
    assert client.get(f"{PATH}/{DEVICE}").status_code == 401
    assert (
        client.get(
            f"{PATH}/{DEVICE}", headers={**HEADERS, "X-Workspace-ID": str(uuid4())}
        ).status_code
        == 404
    )
    app.state.authentication_backend.mode = AuthenticationMode.DEVICE
    assert client.get(f"{PATH}/{DEVICE}", headers=HEADERS).status_code == 401
    app.state.authentication_backend.mode = AuthenticationMode.BROWSER
    app.state.workspace_authorization_backend.actor = uuid4()
    assert client.get(f"{PATH}/{DEVICE}", headers=HEADERS).status_code == 404
    reader.get.assert_not_awaited()


def test_safe_detail_and_collapsed_missing_outcomes() -> None:
    app, reader = fixture()
    client = TestClient(app)
    response = client.get(f"{PATH}/{DEVICE}", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "id": str(DEVICE),
        "display_name": "MacBook",
        "platform": "macos",
        "app_version": "1.0",
        "status": "active",
        "created_at": "2026-11-24T00:00:00Z",
        "updated_at": "2026-11-24T00:00:00Z",
        "last_seen_at": "2026-11-24T00:00:00Z",
        "revoked_at": None,
    }
    reader.get.assert_awaited_once_with(scope=SCOPE, device_id=DEVICE)

    reader.get.return_value = None
    missing = client.get(f"{PATH}/{DEVICE}", headers=HEADERS)
    assert missing.status_code == 404
    reader.get.side_effect = DeviceQueryDenied("do not return this")
    denied = client.get(f"{PATH}/{DEVICE}", headers=HEADERS)
    assert denied.status_code == 404
    assert denied.json()["code"] == missing.json()["code"] == "DEVICE_NOT_FOUND"
    assert "do not return this" not in denied.text


def test_pagination_is_bounded_and_authorization_version_bound() -> None:
    app, reader = fixture()
    reader.list_devices.side_effect = [[summary(), summary()], []]
    client = TestClient(app)
    response = client.get(PATH, headers=HEADERS, params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["page"]["has_more"]
    cursor = response.json()["page"]["next_cursor"]
    response = client.get(PATH, headers=HEADERS, params={"cursor": cursor})
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": {"has_more": False, "next_cursor": None},
    }
    assert reader.list_devices.await_args_list[0].kwargs == {
        "scope": SCOPE,
        "limit": 2,
        "before": None,
    }
    assert reader.list_devices.await_args.kwargs["before"] == (NOW, DEVICE)

    app.state.workspace_authorization_backend.version = 4
    assert (
        client.get(PATH, headers=HEADERS, params={"cursor": cursor}).status_code == 400
    )
    assert client.get(PATH, headers=HEADERS, params={"limit": 101}).status_code == 422
    assert reader.list_devices.await_count == 2


def test_invalid_cursor_sort_and_unavailable_reads_are_generic() -> None:
    app, reader = fixture()
    client = TestClient(app)
    signer = app.state.device_cursor_signer
    cursor = signer.issue(context=device_queries._context(SCOPE), sort_key=["wrong", 1])
    error = client.get(PATH, headers=HEADERS, params={"cursor": cursor})
    assert error.status_code == 400
    reader.list_devices.assert_not_awaited()

    reader.list_devices.side_effect = DeviceQueryDenied("session no longer active")
    assert client.get(PATH, headers=HEADERS).status_code == 404
    reader.list_devices.side_effect = DeviceQueryUnavailable("secret DB metadata")
    error = client.get(PATH, headers=HEADERS)
    assert error.status_code == 503
    assert "secret DB metadata" not in error.text

    reader.get.side_effect = DeviceQueryUnavailable("secret DB metadata")
    assert client.get(f"{PATH}/{DEVICE}", headers=HEADERS).status_code == 503
    app, _ = fixture(enabled=False)
    assert TestClient(app).get(PATH, headers=HEADERS).status_code == 503


def row(*, name: str, version: str = "1.0") -> dict[str, object]:
    return {
        "id": DEVICE,
        "name": name,
        "platform": "macos",
        "app_version": version,
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "last_seen_at": NOW,
        "revoked_at": None,
    }


@pytest.mark.parametrize("label", ["/Users/alex/app", r"C:\Users\alex", "A" * 43])
async def test_store_sanitizes_labels_and_bounds_sql(label: str) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = True
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [row(name=label, version="/a/path")]
    rows.mappings.return_value.one_or_none.side_effect = [
        row(name="Alice’s MacBook"),
        None,
    ]
    connection.execute.return_value = rows
    engine = MagicMock(spec=AsyncEngine)
    engine.begin.return_value.__aenter__.return_value = connection
    reader = SQLAlchemyDeviceQueryReader(engine)

    (device,) = await reader.list_devices(scope=SCOPE, before=(NOW, DEVICE), limit=1)
    assert device.name == "Mac device"
    assert device.app_version is None
    query = str(connection.execute.await_args.args[0])
    assert "d.workspace_id = :workspace AND d.user_id = :actor" in query
    assert "credential_hash" not in query
    assert connection.execute.await_args.args[1]["before_id"] == DEVICE

    device = await reader.get(scope=SCOPE, device_id=DEVICE)
    assert device is not None and device.name == "Alice’s MacBook"
    assert await reader.get(scope=SCOPE, device_id=DEVICE) is None
    with pytest.raises(ValueError):
        await reader.list_devices(scope=SCOPE, before=None, limit=102)

    connection.scalar.return_value = False
    with pytest.raises(DeviceQueryDenied):
        await reader.get(scope=SCOPE, device_id=DEVICE)
    connection.scalar.side_effect = SQLAlchemyError("do not expose")
    with pytest.raises(DeviceQueryUnavailable):
        await reader.get(scope=SCOPE, device_id=DEVICE)
    with pytest.raises(DeviceQueryUnavailable):
        await reader.list_devices(scope=SCOPE, before=None, limit=1)
