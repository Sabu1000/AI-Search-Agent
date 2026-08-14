from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from universal_ai_search.connections.crypto import (
    EncryptedEnvelope,
    LocalEnvelopeEncryption,
)
from universal_ai_search.connections.google import (
    DRIVE_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GoogleAccount,
    GoogleProviderError,
    GoogleTokens,
    HttpGoogleGateway,
)
from universal_ai_search.connections.service import (
    GoogleAuthorizationError,
    GoogleAuthorizationUnavailable,
    GoogleConnectionService,
)
from universal_ai_search.connections.store import OAuthTransaction

WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")


class MemoryStore:
    def __init__(self) -> None:
        self.transaction: OAuthTransaction | None = None
        self.state_hash = b""
        self.saved: dict[str, object] | None = None

    async def create_transaction(self, **values: object) -> None:
        envelope = values["encrypted_payload"]
        assert isinstance(envelope, EncryptedEnvelope)
        self.state_hash = values["state_hash"]  # type: ignore[assignment]
        self.transaction = OAuthTransaction(
            id=values["transaction_id"],  # type: ignore[arg-type]
            workspace_id=values["workspace_id"],  # type: ignore[arg-type]
            user_id=values["user_id"],  # type: ignore[arg-type]
            encrypted_payload=envelope,
            redirect_path=values["redirect_path"],  # type: ignore[arg-type]
        )

    async def consume_transaction(self, **values: object) -> OAuthTransaction | None:
        if values["state_hash"] != self.state_hash:
            return None
        result, self.transaction = self.transaction, None
        return result

    async def connection_id_for_account(self, **values: object) -> UUID:
        return values["proposed_id"]  # type: ignore[return-value]

    async def save_connection(self, **values: object) -> UUID:
        self.saved = values
        return values["connection_id"]  # type: ignore[return-value]


class FakeGateway:
    def __init__(self) -> None:
        self.scopes = frozenset({GMAIL_READONLY_SCOPE})
        self.refresh_token: str | None = "refresh-secret"
        self.verifier = ""

    def authorization_url(self, **values: object) -> str:
        return f"https://accounts.google.test/auth?state={values['state']}"

    async def exchange_code(self, *, code: str, verifier: str) -> GoogleTokens:
        assert code == "provider-code"
        self.verifier = verifier
        return GoogleTokens(
            access_token="access-secret",
            refresh_token=self.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=self.scopes,
        )

    async def account(self, **values: object) -> GoogleAccount:
        assert values["access_token"] == "access-secret"
        return GoogleAccount("google-account-1", "owner@example.com")


def _service(
    enabled: bool = True,
) -> tuple[GoogleConnectionService, MemoryStore, FakeGateway]:
    store = MemoryStore()
    gateway = FakeGateway()
    return (
        GoogleConnectionService(
            store=store,
            gateway=gateway,
            encryption=LocalEnvelopeEncryption(b"e" * 32),
            hash_key=b"h" * 32,
            enabled=enabled,
        ),
        store,
        gateway,
    )


@pytest.mark.asyncio
async def test_google_authorization_is_single_use_and_saves_encrypted_credentials() -> (
    None
):
    service, store, gateway = _service()
    started = await service.start(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        source_families=("gmail",),
        return_path="/settings/connections",
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    completed = await service.complete(
        state=state, code="provider-code", user_id=USER_ID, session_id=SESSION_ID
    )

    assert completed.return_path == "/settings/connections"
    assert gateway.verifier
    assert store.saved is not None
    assert store.saved["scopes"] == frozenset({GMAIL_READONLY_SCOPE})
    credentials = store.saved["credentials"]
    assert isinstance(credentials, EncryptedEnvelope)
    assert b"access-secret" not in credentials.ciphertext
    with pytest.raises(GoogleAuthorizationError):
        await service.complete(
            state=state, code="provider-code", user_id=USER_ID, session_id=SESSION_ID
        )


@pytest.mark.asyncio
async def test_google_authorization_rejects_session_scope_and_refresh_failures() -> (
    None
):
    service, _, gateway = _service()
    started = await service.start(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        source_families=("gmail",),
        return_path="/connections",
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    with pytest.raises(GoogleAuthorizationError):
        await service.complete(
            state=state, code="provider-code", user_id=USER_ID, session_id=UUID(int=9)
        )

    service, _, gateway = _service()
    started = await service.start(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        source_families=("gmail",),
        return_path="/connections",
    )
    gateway.scopes = frozenset({GMAIL_READONLY_SCOPE, DRIVE_READONLY_SCOPE})
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    with pytest.raises(GoogleAuthorizationError):
        await service.complete(
            state=state, code="provider-code", user_id=USER_ID, session_id=SESSION_ID
        )


@pytest.mark.asyncio
async def test_denial_is_consumed_and_disabled_provider_fails_closed() -> None:
    service, _, _ = _service()
    started = await service.start(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        source_families=("gmail",),
        return_path="/connections",
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    assert (
        await service.consume_error(state=state, user_id=USER_ID, session_id=SESSION_ID)
        == "/connections"
    )
    with pytest.raises(GoogleAuthorizationError):
        await service.consume_error(state=state, user_id=USER_ID, session_id=SESSION_ID)

    disabled, _, _ = _service(enabled=False)
    with pytest.raises(GoogleAuthorizationUnavailable):
        await disabled.start(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            session_id=SESSION_ID,
            source_families=("gmail",),
            return_path="/connections",
        )


def test_http_gateway_builds_exact_pkce_read_only_authorization_url() -> None:
    gateway = HttpGoogleGateway(
        client_id="client-id",
        client_secret="secret",
        redirect_uri="https://api.test/callback",
    )
    url = gateway.authorization_url(
        state="opaque-state",
        challenge="pkce-challenge",
        scopes=frozenset({GMAIL_READONLY_SCOPE}),
    )
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == [GMAIL_READONLY_SCOPE]
    assert "gmail.modify" not in url


@pytest.mark.asyncio
async def test_http_gateway_exchanges_code_and_reads_gmail_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://oauth2.googleapis.com/token"):
            assert b"code_verifier=verifier" in request.content
            assert b"client_secret=secret" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
        assert request.url == httpx.URL(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        )
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"emailAddress": "owner@example.test"})

    gateway = HttpGoogleGateway(
        client_id="client-id",
        client_secret="secret",
        redirect_uri="https://api.test/callback",
        transport=httpx.MockTransport(handler),
    )
    tokens = await gateway.exchange_code(code="code", verifier="verifier")
    account = await gateway.account(
        access_token=tokens.access_token, source_families=("gmail",)
    )

    assert tokens.refresh_token == "refresh-token"
    assert tokens.scopes == frozenset({GMAIL_READONLY_SCOPE})
    assert account == GoogleAccount("owner@example.test", "owner@example.test")


@pytest.mark.asyncio
async def test_http_gateway_reads_drive_account_and_sanitizes_failures() -> None:
    def drive_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"].startswith("user(")
        return httpx.Response(
            200,
            json={
                "user": {
                    "permissionId": "permission-1",
                    "displayName": "Drive Owner",
                }
            },
        )

    gateway = HttpGoogleGateway(
        client_id="client-id",
        client_secret="secret",
        redirect_uri="https://api.test/callback",
        transport=httpx.MockTransport(drive_handler),
    )
    account = await gateway.account(
        access_token="access-token", source_families=("google_drive",)
    )
    assert account == GoogleAccount("permission-1", "Drive Owner")

    failed = HttpGoogleGateway(
        client_id="client-id",
        client_secret="secret",
        redirect_uri="https://api.test/callback",
        transport=httpx.MockTransport(lambda request: httpx.Response(400)),
    )
    with pytest.raises(GoogleProviderError) as raised:
        await failed.exchange_code(code="private-code", verifier="private-verifier")
    assert "private-code" not in str(raised.value)
