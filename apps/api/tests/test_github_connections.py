from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.connections.crypto import LocalEnvelopeEncryption
from universal_ai_search.connections.github import (
    GitHubAuthorizationError,
    HttpGitHubAppGateway,
)
from universal_ai_search.connections.github_service import (
    GitHubAuthorizationComplete,
    GitHubAuthorizationStart,
    GitHubConnectionService,
)
from universal_ai_search.connections.store import OAuthTransaction

USER = uuid4()
SESSION = uuid4()
WORKSPACE = uuid4()
CONNECTION = uuid4()
RETURN_PATH = "/settings/connections"
CALLBACK = "/v1/connections/github/callback"


class MemoryStore:
    def __init__(self) -> None:
        self.pending: dict[tuple[UUID, UUID, bytes], OAuthTransaction] = {}
        self.save_grants = AsyncMock(return_value=CONNECTION)

    async def create(self, **values: Any) -> None:
        self.pending[
            values["workspace_id"], values["user_id"], values["state_hash"]
        ] = OAuthTransaction(
            id=values["transaction_id"],
            workspace_id=values["workspace_id"],
            user_id=values["user_id"],
            encrypted_payload=values["envelope"],
            redirect_path=values["return_path"],
        )

    async def consume(
        self, *, workspace_id: UUID, user_id: UUID, state_hash: bytes
    ) -> OAuthTransaction | None:
        return self.pending.pop((workspace_id, user_id, state_hash), None)


def service_fixture() -> tuple[GitHubConnectionService, MemoryStore, SimpleNamespace]:
    store = MemoryStore()
    gateway = SimpleNamespace(
        authorization_url=HttpGitHubAppGateway.authorization_url,
        exchange_user_code=AsyncMock(return_value="ghu_in_memory"),
        verify_selected_repositories=AsyncMock(),
    )
    return (
        GitHubConnectionService(
            store=store,
            gateway=gateway,  # type: ignore[arg-type]
            encryption=LocalEnvelopeEncryption(b"e" * 32),
            hash_key=b"h" * 32,
            app_slug="read-only-search",
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri=f"https://search.example{CALLBACK}",
        ),
        store,
        gateway,
    )


def state_for(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def start(service: GitHubConnectionService, path: str = RETURN_PATH) -> str:
    result = await service.start(
        workspace_id=WORKSPACE,
        user_id=USER,
        session_id=SESSION,
        repository_ids=frozenset({19, 23}),
        return_path=path,
    )
    assert result.authorization_url.startswith(
        "https://github.com/apps/read-only-search/installations/new?"
    )
    return state_for(result.authorization_url)


async def consent(service: GitHubConnectionService, state: str) -> str:
    return state_for(
        await service.begin_consent(
            state=state, user_id=USER, session_id=SESSION, installation_id=42
        )
    )


async def test_two_single_use_steps_bind_session_and_only_persist_grants() -> None:
    service, store, gateway = service_fixture()
    first = await start(service)
    with pytest.raises(GitHubAuthorizationError):
        await service.begin_consent(
            state=first, user_id=uuid4(), session_id=SESSION, installation_id=42
        )
    second = await consent(service, first)
    assert first != second
    with pytest.raises(GitHubAuthorizationError):
        await consent(service, first)
    result = await service.complete(
        state=second, code="opaque", user_id=USER, session_id=SESSION
    )
    assert result == GitHubAuthorizationComplete(CONNECTION, RETURN_PATH)
    gateway.exchange_user_code.assert_awaited_once()
    gateway.verify_selected_repositories.assert_awaited_once_with(
        installation_id=42,
        repository_ids=frozenset({19, 23}),
        user_access_token="ghu_in_memory",
    )
    store.save_grants.assert_awaited_once_with(
        workspace_id=WORKSPACE,
        user_id=USER,
        session_id=SESSION,
        installation_id=42,
        repository_ids=frozenset({19, 23}),
    )
    with pytest.raises(GitHubAuthorizationError):
        await service.complete(
            state=second, code="opaque", user_id=USER, session_id=SESSION
        )


async def test_other_sessions_and_denials_do_not_grant_access() -> None:
    service, store, gateway = service_fixture()
    first = await start(service)
    with pytest.raises(GitHubAuthorizationError):
        await service.begin_consent(
            state=first, user_id=USER, session_id=uuid4(), installation_id=42
        )
    first = await start(service)
    await service.deny(state=first, user_id=USER, session_id=SESSION)
    with pytest.raises(GitHubAuthorizationError):
        await consent(service, first)
    gateway.verify_selected_repositories.assert_not_awaited()
    store.save_grants.assert_not_awaited()


async def test_provider_failure_never_saves_grants() -> None:
    service, store, gateway = service_fixture()
    second = await consent(service, await start(service))
    gateway.verify_selected_repositories.side_effect = GitHubAuthorizationError
    with pytest.raises(GitHubAuthorizationError):
        await service.complete(
            state=second, code="opaque", user_id=USER, session_id=SESSION
        )
    store.save_grants.assert_not_awaited()


@pytest.mark.parametrize("path", ["//evil.test", "/%2f", "/\\evil.test", "/.."])
async def test_open_redirect_is_rejected(path: str) -> None:
    service, store, _ = service_fixture()
    with pytest.raises(GitHubAuthorizationError):
        await start(service, path)
    assert not store.pending


class Backend:
    def __init__(self, mode: AuthenticationMode = AuthenticationMode.BROWSER) -> None:
        self.mode = mode

    async def authenticate(self, request: object) -> Principal:
        return Principal(
            subject_id=USER, session_id=SESSION, mode=self.mode, authorization_version=1
        )


class Workspace:
    def __init__(self, role: str) -> None:
        self.role = role

    async def authorize(self, **_kwargs: object) -> WorkspaceContext:
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=USER,
            role=self.role,
            authorization_version=1,
        )


def client_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str = "owner",
    mode: AuthenticationMode = AuthenticationMode.BROWSER,
    enabled: bool = True,
) -> tuple[SimpleNamespace, TestClient]:
    app = create_app()
    service = SimpleNamespace(
        start=AsyncMock(
            return_value=GitHubAuthorizationStart(
                "https://github.com/apps/read-only/installations/new",
                datetime.now(UTC) + timedelta(minutes=10),
            )
        ),
        begin_consent=AsyncMock(
            return_value="https://github.com/login/oauth/authorize"
        ),
        complete=AsyncMock(
            return_value=GitHubAuthorizationComplete(CONNECTION, RETURN_PATH)
        ),
        deny=AsyncMock(),
    )
    app.state.github_connection_service = service if enabled else None
    app.state.authentication_backend = Backend(mode)
    app.state.workspace_authorization_backend = Workspace(role)
    monkeypatch.setattr(
        "universal_ai_search.api.routes.github_connections._require_csrf", AsyncMock()
    )
    return service, TestClient(app)


@pytest.mark.parametrize(
    ("role", "mode", "enabled", "status"),
    [
        ("owner", AuthenticationMode.BROWSER, True, 200),
        ("member", AuthenticationMode.BROWSER, True, 403),
        ("owner", AuthenticationMode.BEARER, True, 403),
        ("owner", AuthenticationMode.BROWSER, False, 503),
    ],
)
def test_authorize_routes(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mode: AuthenticationMode,
    enabled: bool,
    status: int,
) -> None:
    service, client = client_fixture(monkeypatch, role=role, mode=mode, enabled=enabled)
    with client:
        response = client.post(
            "/v1/connections/github/authorize",
            headers={"X-Workspace-ID": str(WORKSPACE)},
            json={"repository_ids": [23], "return_path": RETURN_PATH},
        )
        service.start.side_effect = GitHubAuthorizationError
        rejected = client.post(
            "/v1/connections/github/authorize",
            headers={"X-Workspace-ID": str(WORKSPACE)},
            json={"repository_ids": [23], "return_path": RETURN_PATH},
        )
    assert response.status_code == status
    if status == 200:
        assert rejected.status_code == 400


def test_callback_never_redirects_provider_secrets_to_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = client_fixture(monkeypatch)
    with client:
        installation = client.get(
            CALLBACK,
            params={"state": "s" * 64, "installation_id": 42},
            follow_redirects=False,
        )
        complete = client.get(
            CALLBACK,
            params={"state": "t" * 64, "code": "secret"},
            follow_redirects=False,
        )
        denied = client.get(
            CALLBACK,
            params={"state": "t" * 64, "error": "sensitive-provider-detail"},
            follow_redirects=False,
        )
    assert installation.status_code == complete.status_code == 303
    assert complete.headers["location"] == (
        f"http://localhost:3000{RETURN_PATH}?connection_id={CONNECTION}&result=pending_sync"
    )
    assert complete.headers["cache-control"] == "no-store"
    assert complete.headers["referrer-policy"] == "no-referrer"
    assert denied.status_code == 400
    assert "sensitive-provider-detail" not in denied.text
    service.deny.assert_awaited_once()
