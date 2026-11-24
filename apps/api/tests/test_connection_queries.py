from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes import connection_queries
from universal_ai_search.connections.query import ConnectionResponse, ConnectionSnapshot

NOW = datetime(2026, 11, 24, tzinfo=UTC)
USER = uuid4()
WORKSPACE = uuid4()
CONNECTION = uuid4()
PATH = "/v1/connections"
HEADERS = {"X-Workspace-ID": str(WORKSPACE)}
COOKIE = {"Cookie": "__Host-uas_access=synthetic"}


def snapshot(connection_id: UUID, version: int = 1) -> ConnectionSnapshot:
    return ConnectionSnapshot(
        body=ConnectionResponse(
            id=connection_id,
            provider="github",
            display_label="GitHub",
            status="pending",
            scopes=["metadata:read"],
            selected_collections=[],
            selected_collections_truncated=False,
            last_successful_sync_at=None,
            current_operation=None,
            error_code=None,
            recovery_action=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        lock_version=version,
    )


class BrowserAuth:
    mode = AuthenticationMode.BROWSER

    async def authenticate(self, request: object) -> Principal | None:
        if not request.cookies.get("__Host-uas_access"):  # type: ignore[attr-defined]
            return None
        return Principal(
            subject_id=USER,
            session_id=uuid4() if self.mode is AuthenticationMode.BROWSER else None,
            mode=self.mode,
            authorization_version=1,
        )


class Membership:
    authorization_version = 1

    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE or principal.subject_id != USER:
            return None
        return WorkspaceContext(
            workspace_id=workspace_id,
            user_id=USER,
            role="member",
            authorization_version=self.authorization_version,
        )


def app_fixture() -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    reader = SimpleNamespace(
        list_connections=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=snapshot(CONNECTION, 2)),
    )
    app.state.authentication_backend = BrowserAuth()
    app.state.workspace_authorization_backend = Membership()
    app.state.connection_query_reader = reader
    app.state.connection_cursor_signer = CursorSigner(b"s" * 32)
    install_problem_handlers(app)
    app.include_router(connection_queries.router, prefix="/v1")
    return app, reader


def test_connection_reads_require_signed_in_member_and_workspace() -> None:
    app, reader = app_fixture()
    client = TestClient(app)
    assert client.get(PATH, headers=HEADERS).status_code == 401
    assert client.get(PATH, headers=COOKIE).status_code == 400
    assert (
        client.get(PATH, headers={**COOKIE, "X-Workspace-ID": str(uuid4())}).status_code
        == 404
    )
    app.state.authentication_backend.mode = AuthenticationMode.DEVICE
    assert client.get(PATH, headers={**HEADERS, **COOKIE}).status_code == 401
    reader.list_connections.assert_not_awaited()


def test_list_paginates_and_binds_cursor_to_current_workspace_authority() -> None:
    app, reader = app_fixture()
    reader.list_connections.side_effect = [
        [snapshot(CONNECTION, 2), snapshot(uuid4())],
        [snapshot(uuid4())],
    ]
    client = TestClient(app)
    headers = {**HEADERS, **COOKIE}
    response = client.get(
        PATH, params={"limit": 1, "provider": "github"}, headers=headers
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    params = {"cursor": response.json()["page"]["next_cursor"], "provider": "github"}
    assert response.json()["page"]["has_more"]
    assert response.json()["items"][0]["id"] == str(CONNECTION)
    response = client.get(PATH, params=params, headers=headers)
    assert response.status_code == 200
    assert response.json()["page"] == {"has_more": False, "next_cursor": None}
    assert reader.list_connections.await_args.kwargs["before"] == (NOW, CONNECTION)
    assert reader.list_connections.await_args.kwargs["workspace_id"] == WORKSPACE
    assert reader.list_connections.await_args.kwargs["user_id"] == USER

    assert (
        client.get(
            PATH, params={**params, "provider": "google"}, headers=headers
        ).status_code
        == 400
    )
    app.state.workspace_authorization_backend.authorization_version = 2
    assert client.get(PATH, params=params, headers=headers).status_code == 400
    assert (
        client.get(PATH, params={"cursor": "unusable"}, headers=headers).status_code
        == 400
    )
    assert reader.list_connections.await_count == 2


def test_rejects_malformed_sort_key_even_when_signed() -> None:
    app, reader = app_fixture()
    context = connection_queries._context(
        WorkspaceContext(
            workspace_id=WORKSPACE, user_id=USER, role="member", authorization_version=1
        ),
        None,
    )
    signer = app.state.connection_cursor_signer
    cursor = signer.issue(context=context, sort_key=["not-a-date", str(CONNECTION)])
    response = TestClient(app).get(
        PATH, params={"cursor": cursor}, headers={**HEADERS, **COOKIE}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "CURSOR_INVALID"
    reader.list_connections.assert_not_awaited()


def test_detail_etag_and_status_projection() -> None:
    app, reader = app_fixture()
    client = TestClient(app)
    headers = {**HEADERS, **COOKIE}
    response = client.get(f"{PATH}/{CONNECTION}", headers=headers)
    assert response.status_code == 200
    assert response.headers["etag"] == connection_queries.connection_etag(CONNECTION, 2)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["scopes"] == ["metadata:read"]
    assert "lock_version" not in response.json()

    status = client.get(f"{PATH}/{CONNECTION}/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "pending"
    assert "scopes" not in status.json()
    assert "selected_collections" not in status.json()
    assert status.headers["cache-control"] == "private, no-store"
    reader.get.assert_awaited_with(
        workspace_id=WORKSPACE, user_id=USER, connection_id=CONNECTION
    )

    reader.get.return_value = None
    response = client.get(f"{PATH}/{uuid4()}", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "CONNECTION_NOT_FOUND"
    assert response.headers["cache-control"] == "private, no-store"


def database_row(
    *,
    provider: str = "github",
    status: str = "reauthorization_required",
    error: str | None = None,
    selections: list[dict[str, str]] | None = None,
    delete: bool = False,
) -> dict[str, object]:
    operation = {
        "id": str(uuid4()),
        "type": "connection_delete" if delete else "sync",
        "status": "pending",
        "created_at": NOW.isoformat(),
    }
    return {
        "id": CONNECTION,
        "provider": provider,
        "display_label": (
            "/never/expose/this/path" if provider == "local_files" else "GitHub"
        ),
        "status": status,
        "scopes": ["metadata:read"],
        "selected_collections": selections or [],
        "sync": None if delete else operation,
        "deletion": operation if delete else None,
        "last_successful_sync_at": NOW,
        "last_error_code": error,
        "created_at": NOW,
        "updated_at": NOW,
        "lock_version": 3,
    }


async def test_query_adapter_applies_scope_bounds_and_projects_only_safe_values() -> (
    None
):
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import AsyncEngine

    from universal_ai_search.connections.query import SQLAlchemyConnectionQueryReader

    root = {"id": str(uuid4()), "name": "/home/private", "kind": "folder"}
    fake_result = MagicMock()
    fake_result.mappings.return_value.all.return_value = [
        database_row(
            provider="local_files",
            status="active",
            error="raw private SQL error text",
            selections=[root] * 21,
        )
    ]
    fake_result.mappings.return_value.one_or_none.side_effect = [
        database_row(status="deleting", delete=True),
        None,
    ]
    engine = MagicMock(spec=AsyncEngine)
    connection = AsyncMock()
    engine.begin.return_value.__aenter__.return_value = connection
    connection.execute.return_value = fake_result
    reader = SQLAlchemyConnectionQueryReader(engine)
    (with_error,) = await reader.list_connections(
        workspace_id=WORKSPACE,
        user_id=USER,
        provider="local_files",
        before=(NOW, CONNECTION),
        limit=2,
    )
    assert with_error.body.display_label == "Local files"
    assert {item.name for item in with_error.body.selected_collections} == {
        "Local folder"
    }
    assert len(with_error.body.selected_collections) == 20
    assert with_error.body.selected_collections_truncated
    assert with_error.body.error_code == "CONNECTION_NEEDS_ATTENTION"
    assert with_error.body.recovery_action == "check_status"
    assert with_error.body.current_operation is not None
    assert with_error.body.current_operation.type == "sync"
    execution = connection.execute.await_args.args
    assert "c.workspace_id = :workspace_id" in str(execution[0])
    assert "c.provider = :provider" in str(execution[0])
    assert "c.id) < (:before_created_at, :before_id)" in str(execution[0])
    params = execution[1]
    assert params["workspace_id"] == WORKSPACE
    assert params["row_limit"] == 2
    assert all("write" not in scope for scope in params["allowed_scopes"])

    deleting = await reader.get(
        workspace_id=WORKSPACE, user_id=USER, connection_id=CONNECTION
    )
    assert deleting is not None
    assert deleting.body.status == "deleting"
    assert not deleting.body.selected_collections
    assert not deleting.body.scopes
    assert deleting.body.current_operation is not None
    assert deleting.body.current_operation.type == "connection_delete"
    assert (
        await reader.get(workspace_id=WORKSPACE, user_id=USER, connection_id=CONNECTION)
        is None
    )


def test_safe_scopes_and_recovery_are_narrow() -> None:
    from typing import cast

    from sqlalchemy import RowMapping

    from universal_ai_search.connections.query import _snapshot

    item = database_row(
        error="RATE_LIMIT",
        selections=[
            {"id": str(uuid4()), "name": "GitHub repository 123", "kind": "repository"}
        ],
    )
    result = _snapshot(cast(RowMapping, item), 1).body
    assert result.selected_collections[0].name == "GitHub repository"
    assert result.recovery_action == "reauthorize"
    assert result.error_code == "RATE_LIMIT"
