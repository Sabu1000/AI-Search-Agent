from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal
from universal_ai_search.auth.backends import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE
from universal_ai_search.auth.service import (
    AuthenticationFailure,
    CurrentAccount,
    InvalidVerificationToken,
    IssuedSession,
)
from universal_ai_search.auth.store import (
    Membership,
    SessionIdentity,
    UserAccount,
)

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
EXPIRY = datetime.now(UTC) + timedelta(minutes=15)


def _issued() -> IssuedSession:
    return IssuedSession(
        "access-token",
        EXPIRY,
        "refresh-token-that-is-long-enough-for-validation",
        "csrf-token-that-is-long-enough-for-validation",
        SessionIdentity(SESSION_ID, USER_ID, 2),
    )


def _current() -> CurrentAccount:
    return CurrentAccount(
        UserAccount(USER_ID, "owner@example.com", "Example Owner", True, 2),
        [Membership(WORKSPACE_ID, "Example Owner", "owner", "active", 1)],
    )


def _app() -> tuple[object, TestClient]:
    app = create_app()
    service = SimpleNamespace(
        web_origin="http://localhost:3000",
        register=AsyncMock(),
        verify_email=AsyncMock(),
        login=AsyncMock(return_value=_issued()),
        refresh=AsyncMock(return_value=_issued()),
        current_account=AsyncMock(return_value=_current()),
        validate_csrf=AsyncMock(return_value=True),
        logout=AsyncMock(),
    )
    app.state.authentication_service = service
    return service, TestClient(app, base_url="https://testserver")


def test_registration_and_verification_routes_are_safe() -> None:
    service, client = _app()
    with client:
        registered = client.post(
            "/v1/auth/register",
            json={
                "email": "owner@example.com",
                "password": "a long secure passphrase",
                "full_name": "Example Owner",
                "terms_version": "2026-08-12",
                "locale": "en-US",
            },
        )
        verified = client.post(
            "/v1/auth/email/verify",
            json={"token": "verification-token-that-is-long-enough"},
        )

    assert registered.status_code == 202
    assert registered.json()["status"] == "verification_if_eligible"
    assert verified.status_code == 204
    service.register.assert_awaited_once()


def test_registration_policy_and_invalid_verification_use_problems() -> None:
    service, client = _app()
    service.register.side_effect = ValueError("password cannot contain identity")
    service.verify_email.side_effect = InvalidVerificationToken
    with client:
        registration = client.post(
            "/v1/auth/register",
            json={
                "email": "owner@example.com",
                "password": "owner password is unsafe",
                "full_name": "Example Owner",
                "terms_version": "1",
            },
        )
        verification = client.post(
            "/v1/auth/email/verify",
            json={"token": "verification-token-that-is-long-enough"},
        )

    assert registration.status_code == 422
    assert registration.json()["code"] == "PASSWORD_POLICY_FAILED"
    assert verification.status_code == 400
    assert verification.json()["code"] == "VERIFICATION_TOKEN_INVALID"


def test_browser_login_sets_host_only_session_cookies() -> None:
    service, client = _app()
    with client:
        response = client.post(
            "/v1/auth/login",
            json={
                "email": "owner@example.com",
                "password": "a long secure passphrase",
                "client_type": "browser",
            },
        )

    assert response.status_code == 200
    assert response.json()["access_token"] is None
    assert response.json()["csrf_token"].startswith("csrf-token")
    cookies = response.headers.get_list("set-cookie")
    assert any(ACCESS_COOKIE in value and "HttpOnly" in value for value in cookies)
    assert any(
        REFRESH_COOKIE in value and "SameSite=strict" in value for value in cookies
    )
    assert any(CSRF_COOKIE in value and "HttpOnly" not in value for value in cookies)
    service.login.assert_awaited_once()


def test_native_login_returns_tokens_and_invalid_login_is_generic() -> None:
    service, client = _app()
    with client:
        native = client.post(
            "/v1/auth/login",
            json={
                "email": "owner@example.com",
                "password": "a long secure passphrase",
                "client_type": "native",
            },
        )
        service.login.side_effect = AuthenticationFailure
        rejected = client.post(
            "/v1/auth/login",
            json={"email": "unknown@example.com", "password": "wrong"},
        )

    assert native.json()["access_token"] == "access-token"
    assert native.json()["refresh_token"].startswith("refresh-token")
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "INVALID_CREDENTIALS"


def test_browser_refresh_requires_matching_csrf_and_rotates_cookies() -> None:
    service, client = _app()
    client.cookies.set(
        REFRESH_COOKIE, "refresh-token-that-is-long-enough-for-validation"
    )
    client.cookies.set(CSRF_COOKIE, "csrf-token-that-is-long-enough-for-validation")
    with client:
        rejected = client.post("/v1/auth/refresh", json={"client_type": "browser"})
        accepted = client.post(
            "/v1/auth/refresh",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": "csrf-token-that-is-long-enough-for-validation",
            },
            json={"client_type": "browser"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    service.refresh.assert_awaited_once_with(
        "refresh-token-that-is-long-enough-for-validation",
        "csrf-token-that-is-long-enough-for-validation",
    )


class BrowserBackend:
    async def authenticate(self, request: object) -> Principal:
        request.state.session_identity = SessionIdentity(SESSION_ID, USER_ID, 2)  # type: ignore[attr-defined]
        return Principal(
            subject_id=USER_ID,
            session_id=SESSION_ID,
            mode=AuthenticationMode.BROWSER,
            authorization_version=2,
            access_expires_at=EXPIRY,
        )


def test_me_and_logout_return_current_account_and_revoke_session() -> None:
    service, client = _app()
    client.app.state.authentication_backend = BrowserBackend()
    client.cookies.set(CSRF_COOKIE, "csrf-token-that-is-long-enough-for-validation")
    with client:
        current = client.get("/v1/auth/me")
        logout = client.post(
            "/v1/auth/logout",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": "csrf-token-that-is-long-enough-for-validation",
            },
        )

    assert current.status_code == 200
    assert current.headers["Cache-Control"] == "private, no-store"
    assert current.json()["user"]["email"] == "owner@example.com"
    assert current.json()["preferred_workspace_id"] == str(WORKSPACE_ID)
    assert logout.status_code == 204
    service.logout.assert_awaited_once()
