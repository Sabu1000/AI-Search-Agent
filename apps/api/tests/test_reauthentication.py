from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncConnection

from test_conversation_routes import HEADERS, SESSION, USER, WORKSPACE, Auth, Workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode
from universal_ai_search.api.routes.reauthentication import router
from universal_ai_search.auth.reauth import (
    IssuedReauthentication,
    ReauthenticationFailed,
    ReauthenticationLimited,
    ReauthenticationService,
    ReauthenticationUnavailable,
    RedisReauthLimiter,
)
from universal_ai_search.auth.reauth_store import ReauthenticationStore, ReauthScope
from universal_ai_search.auth.recovery_limiter import RedisScriptClient
from universal_ai_search.auth.security import PasswordSecurity

PATH = "/v1/auth/reauthenticate"
NOW = datetime(2026, 11, 24, tzinfo=UTC)
SCOPE = ReauthScope(USER, SESSION, WORKSPACE, 2, 7)
PASSWORD = "synthetic-only-P@ssphrase-123"
PAYLOAD = {"password": PASSWORD, "purpose": "export"}


def service_fixture(
    *, limited: bool = False, valid: bool = True
) -> tuple[ReauthenticationService, SimpleNamespace, SimpleNamespace]:
    store = SimpleNamespace(
        password_hash=AsyncMock(return_value="dummy-hash"),
        issue=AsyncMock(return_value=NOW),
        consume=AsyncMock(return_value=True),
    )
    limiter = SimpleNamespace(allow=AsyncMock(return_value=not limited))

    def verify(password_hash: str, password: str) -> bool:
        return valid and password_hash == "dummy-hash" and password == PASSWORD

    service = ReauthenticationService(
        store=cast(ReauthenticationStore, store),
        security=cast(PasswordSecurity, SimpleNamespace(verify=verify)),
        limiter=limiter,
        hash_key=b"s" * 32,
    )
    return service, store, limiter


async def test_service_only_transfers_keyed_values_to_the_token_store() -> None:
    service, store, limiter = service_fixture()
    request_id = uuid4()
    proof = await service.obtain(
        scope=SCOPE, purpose="export", password=PASSWORD, request_id=request_id
    )
    assert len(proof.token) == 43 and proof.token not in repr(proof)
    assert proof.expires_at == NOW
    kwargs = store.issue.await_args.kwargs
    assert kwargs["request_id"] == request_id and kwargs["scope"] == SCOPE
    assert len(kwargs["token_hash"]) == 32
    assert kwargs["token_hash"] not in (
        proof.token.encode(),
        kwargs["audit_subject"],
        kwargs["audit_workspace"],
        limiter.allow.await_args.kwargs["user_key"],
    )

    await service.consume(
        cast(AsyncConnection, None), scope=SCOPE, purpose="export", token=proof.token
    )
    assert store.consume.await_args.kwargs["token_hash"] == kwargs["token_hash"]
    store.consume.return_value = False
    with pytest.raises(ReauthenticationFailed):
        await service.consume(
            cast(AsyncConnection, None),
            scope=SCOPE,
            purpose="export",
            token=proof.token,
        )
    with pytest.raises(ReauthenticationFailed):
        await service.consume(
            cast(AsyncConnection, None),
            scope=SCOPE,
            purpose="export",
            token="not-a-proof",
        )


async def test_bad_password_throttle_and_stale_session_fail_closed() -> None:
    limited, limited_store, _ = service_fixture(limited=True)
    with pytest.raises(ReauthenticationLimited):
        await limited.obtain(
            scope=SCOPE, purpose="export", password=PASSWORD, request_id=uuid4()
        )
    limited_store.password_hash.assert_not_awaited()

    service, store, _ = service_fixture(valid=False)
    with pytest.raises(ReauthenticationFailed):
        await service.obtain(
            scope=SCOPE, purpose="export", password=PASSWORD, request_id=uuid4()
        )
    store.issue.assert_not_awaited()
    with pytest.raises(ReauthenticationFailed):
        await service.obtain(
            scope=SCOPE, purpose="export", password="\ud800", request_id=uuid4()
        )

    service, store, _ = service_fixture()
    store.issue.return_value = None
    with pytest.raises(ReauthenticationFailed):
        await service.obtain(
            scope=SCOPE, purpose="export", password=PASSWORD, request_id=uuid4()
        )


async def test_redis_limiter_is_atomic_and_fails_closed_on_dependency_errors() -> None:
    evaluate = AsyncMock(side_effect=[1, 0, "bad", RedisError()])
    limiter = RedisReauthLimiter(
        cast(RedisScriptClient, SimpleNamespace(eval=evaluate))
    )
    assert await limiter.allow(user_key=b"d" * 32)
    assert not await limiter.allow(user_key=b"d" * 32)
    args = evaluate.await_args.args
    assert args[1:] == (1, f"uas:reauth:actor:{(b'd' * 32).hex()}", "5", "300")
    with pytest.raises(ReauthenticationUnavailable):
        await limiter.allow(user_key=b"d" * 32)
    with pytest.raises(ReauthenticationUnavailable):
        await limiter.allow(user_key=b"d" * 32)


def client_factory() -> tuple[TestClient, Auth, AsyncMock]:
    app = create_app()
    auth = Auth()
    obtain = AsyncMock(return_value=IssuedReauthentication("p" * 43, NOW))
    app.state.authentication_backend = auth
    app.state.workspace_authorization_backend = Workspace()
    app.state.reauth_service = SimpleNamespace(obtain=obtain)
    app.include_router(router, prefix="/v1")
    return TestClient(app), auth, obtain


def test_route_uses_verified_context_and_no_store_on_response() -> None:
    client, auth, obtain = client_factory()
    request_id = uuid4()
    with client:
        result = client.post(
            PATH, json=PAYLOAD, headers={**HEADERS, "X-Request-ID": str(request_id)}
        )
        bad_purpose = client.post(
            PATH, json={**PAYLOAD, "purpose": "not-supported"}, headers=HEADERS
        )
        auth.mode = AuthenticationMode.BROWSER
        no_csrf = client.post(PATH, json=PAYLOAD, headers=HEADERS)
        auth.mode = AuthenticationMode.DEVICE
        no_session = client.post(PATH, json=PAYLOAD, headers=HEADERS)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert result.json()["reauth_token"] == "p" * 43
    assert bad_purpose.status_code == 422
    assert PASSWORD not in bad_purpose.text
    assert no_csrf.json()["code"] == "CSRF_FAILED"
    assert no_session.json()["code"] == "SESSION_INVALID"
    obtain.assert_awaited_once_with(
        scope=SCOPE, purpose="export", password=PASSWORD, request_id=request_id
    )


def test_route_uses_generic_failures_and_rate_limit_headers() -> None:
    client, _, obtain = client_factory()
    obtain.side_effect = [
        ReauthenticationFailed("sensitive detail"),
        ReauthenticationLimited(),
        ReauthenticationUnavailable(),
    ]
    with client:
        failed = client.post(PATH, json=PAYLOAD, headers=HEADERS)
        limited = client.post(PATH, json=PAYLOAD, headers=HEADERS)
        unavailable = client.post(PATH, json=PAYLOAD, headers=HEADERS)
    assert failed.status_code == 401
    assert "sensitive detail" not in failed.text
    assert limited.status_code == 429 and limited.headers["retry-after"] == "300"
    assert unavailable.status_code == 503
