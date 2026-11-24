from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.local_account_export import export_operation, router
from universal_ai_search.auth.local_account_export import (
    ExportBusy,
    ExportFile,
    ExportMissing,
    ExportOperation,
    ExportUnsupported,
)
from universal_ai_search.auth.reauth import ReauthenticationFailed
from universal_ai_search.auth.reauth_store import ReauthScope

ACTOR, WORKSPACE, SESSION, EXPORT = (uuid4() for _ in range(4))
SCOPE = ReauthScope(ACTOR, SESSION, WORKSPACE, 5, 8)
NOW = datetime(2026, 11, 24, tzinfo=UTC)
OPERATION = ExportOperation(EXPORT, NOW, NOW, NOW + timedelta(minutes=5))
HEADERS = {
    "Authorization": "Bearer synthetic-session",
    "X-Workspace-ID": str(WORKSPACE),
    "X-Reauth-Token": "z" * 43,
    "Idempotency-Key": "synthetic-export-key",
}
START = "/v1/account/export"
DOWNLOAD = f"/v1/account/exports/{EXPORT}:download"


class Auth:
    mode = AuthenticationMode.BEARER

    async def authenticate(self, request: Request) -> Principal | None:
        if not request.headers.get("authorization"):
            return None
        return Principal(
            subject_id=ACTOR,
            session_id=SESSION,
            mode=self.mode,
            authorization_version=5,
        )


class Workspaces:
    role = "owner"

    async def authorize(
        self,
        *,
        principal: Principal,
        workspace_id: object,
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=ACTOR,
            role=self.role,
            authorization_version=8,
        )


def _fixture(
    *,
    enabled: bool = True,
) -> tuple[TestClient, FastAPI, SimpleNamespace]:
    app = FastAPI()
    app.state.authentication_backend = Auth()
    app.state.workspace_authorization_backend = Workspaces()
    app.state.authentication_service = SimpleNamespace(
        web_origin="https://test.example"
    )
    store = SimpleNamespace(
        start=AsyncMock(return_value=OPERATION),
        download=AsyncMock(
            return_value=ExportFile(b'{"profile": {"name": "fixture"}}')
        ),
    )
    if enabled:
        app.state.local_account_export_store = store
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    return TestClient(app), app, store


def test_start_safe_operation_with_no_proof_or_capability_in_replay() -> None:
    client, _, store = _fixture()
    response = client.post(START, headers=HEADERS)
    assert response.status_code == 202
    assert response.headers["location"] == f"/v1/operations/{EXPORT}"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["operation"]["links"] == {
        "self": f"/v1/operations/{EXPORT}",
        "download": None,
    }
    assert HEADERS["X-Reauth-Token"] not in response.text
    assert HEADERS["Idempotency-Key"] not in response.text
    kwargs = store.start.await_args.kwargs
    assert kwargs["scope"] == SCOPE
    assert kwargs["proof"] == HEADERS["X-Reauth-Token"]
    assert kwargs["idempotency_key"] == HEADERS["Idempotency-Key"]
    detail = export_operation(replace(OPERATION, downloadable=True), Response())
    assert detail.operation.links.download == DOWNLOAD
    assert "?" not in DOWNLOAD


def test_proof_key_and_transport_are_strict_before_any_export() -> None:
    client, app, store = _fixture()
    assert client.post(START).status_code == 401
    headers = {**HEADERS, "X-Reauth-Token": "short"}
    assert client.post(START, headers=headers).status_code == 401
    duplicate_proof = [*HEADERS.items(), ("X-Reauth-Token", "z" * 43)]
    assert client.post(START, headers=duplicate_proof).status_code == 401
    duplicate_key = [*HEADERS.items(), ("Idempotency-Key", "second-key")]
    assert client.post(START, headers=duplicate_key).status_code == 400
    assert (
        client.post(START, headers=HEADERS, json={"export": "all"}).status_code == 400
    )
    assert (
        client.post(START, headers={**HEADERS, "Cookie": "ambient=1"}).status_code
        == 404
    )
    assert (
        client.post(
            DOWNLOAD, headers={**HEADERS, "Origin": "https://another.example"}
        ).status_code
        == 404
    )
    app.state.workspace_authorization_backend.role = "member"
    assert client.post(START, headers=HEADERS).status_code == 404
    app.state.workspace_authorization_backend.role = "owner"
    app.state.authentication_backend.mode = AuthenticationMode.DEVICE
    assert client.post(DOWNLOAD, headers=HEADERS).status_code == 404
    app.state.authentication_backend.mode = AuthenticationMode.BROWSER
    assert client.post(DOWNLOAD, headers=HEADERS).status_code == 403
    store.start.assert_not_awaited()
    store.download.assert_not_awaited()


def test_download_must_post_and_sends_only_private_attachment() -> None:
    client, _, store = _fixture()
    assert client.get(DOWNLOAD, headers=HEADERS).status_code == 405
    response = client.post(DOWNLOAD, headers=HEADERS)
    assert response.status_code == 200
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="account-export.json"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {"profile": {"name": "fixture"}}
    assert store.download.await_args.kwargs["operation_id"] == EXPORT
    assert store.download.await_args.kwargs["scope"] == SCOPE
    store.download.side_effect = ExportMissing("must not expose this fixture")
    response = client.post(DOWNLOAD, headers=HEADERS)
    assert response.status_code == 404
    assert "fixture" not in response.text


def test_safe_error_vocabulary_and_disabled_backend() -> None:
    client, _, store = _fixture()
    for error, status, code in (
        (ExportUnsupported, 409, "ACCOUNT_EXPORT_SCOPE_UNSUPPORTED"),
        (ExportBusy, 409, "ACCOUNT_EXPORT_BUSY"),
        (ReauthenticationFailed, 401, "REAUTHENTICATION_FAILED"),
        (ExportMissing, 404, "ACCOUNT_EXPORT_NOT_FOUND"),
        (SQLAlchemyError, 503, "ACCOUNT_EXPORT_UNAVAILABLE"),
    ):
        store.start.side_effect = error("postgres detail never leaves the backend")
        response = client.post(START, headers=HEADERS)
        assert response.status_code == status
        assert response.json()["code"] == code
        assert "postgres detail" not in response.text
    client, _, _ = _fixture(enabled=False)
    assert client.post(START, headers=HEADERS).status_code == 503
    assert client.post(DOWNLOAD, headers=HEADERS).status_code == 503
