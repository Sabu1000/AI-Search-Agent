from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from universal_ai_search.auth.email import NullVerificationSender
from universal_ai_search.auth.security import (
    AccessTokenCodec,
    PasswordSecurity,
    hash_token,
)
from universal_ai_search.auth.service import (
    AuthenticationFailure,
    AuthenticationService,
    InvalidVerificationToken,
)
from universal_ai_search.auth.store import (
    LoginIdentity,
    RotationResult,
    RotationState,
    SessionIdentity,
    VerifiedIdentity,
)

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")
SIGNING_KEY = b"access-token-signing-key-for-tests-only"
HASH_KEY = b"opaque-token-hashing-key-for-tests-only"


def test_password_security_hashes_verifies_and_enforces_policy() -> None:
    security = PasswordSecurity()
    password_hash = security.hash("a long secure passphrase")

    assert password_hash.startswith("$argon2id$")
    assert security.verify(password_hash, "a long secure passphrase")
    assert not security.verify(password_hash, "the wrong passphrase")
    assert not security.verify(None, "a long secure passphrase")
    assert not security.verify("invalid-hash", "a long secure passphrase")
    assert not security.needs_rehash(password_hash)

    with pytest.raises(ValueError, match="between 12 and 128"):
        security.validate("short", "owner@example.com")
    with pytest.raises(ValueError, match="control"):
        security.validate("long password\nvalue", "owner@example.com")
    with pytest.raises(ValueError, match="email identity"):
        security.validate("owner has a long password", "owner@example.com")
    with pytest.raises(ValueError, match="too common"):
        security.validate("password1234", "owner@example.com")


def test_access_tokens_are_bound_signed_and_expiring() -> None:
    codec = AccessTokenCodec(SIGNING_KEY)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    token, expires_at = codec.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        authorization_version=4,
        now=now,
    )

    assert expires_at == now + timedelta(minutes=15)
    verified = codec.verify(token, now=now)
    assert verified is not None
    assert verified["sub"] == str(USER_ID)
    assert verified["iss"] == "universal-ai-search-api"
    assert UUID(verified["jti"])
    assert len(token.split(".")) == 3
    assert codec.verify(token + "tampered", now=now) is None
    assert codec.verify(token, now=expires_at) is None
    assert codec.verify("not-a-token", now=now) is None
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AccessTokenCodec(b"short")


def _service(store: AsyncMock) -> AuthenticationService:
    return AuthenticationService(
        store=store,
        password_security=PasswordSecurity(),
        token_codec=AccessTokenCodec(SIGNING_KEY),
        token_hash_key=HASH_KEY,
        verification_sender=NullVerificationSender(),
        web_origin="https://app.example.test",
    )


async def test_registration_and_verification_are_generic_and_single_use() -> None:
    store = AsyncMock()
    store.register.return_value = True
    store.verify_email.return_value = VerifiedIdentity(
        USER_ID,
        "owner@example.com",
        "Example Owner",
        uuid4(),
        2,
    )
    service = _service(store)

    await service.register(
        email=" Owner@Example.com ",
        password="a long secure passphrase",
        full_name=" Example Owner ",
        terms_version="2026-08-12",
        locale="en-US",
    )
    await service.verify_email("verification-token-that-is-long-enough")

    assert store.register.await_args.kwargs["email"] == "owner@example.com"
    assert store.register.await_args.kwargs["full_name"] == "Example Owner"
    assert store.register.await_args.kwargs["password_hash"].startswith("$argon2id$")
    store.verify_email.return_value = None
    with pytest.raises(InvalidVerificationToken):
        await service.verify_email("verification-token-that-is-long-enough")


async def test_login_issues_session_and_rejects_ineligible_identity() -> None:
    password_security = PasswordSecurity()
    store = AsyncMock()
    store.login_identity.return_value = LoginIdentity(
        USER_ID,
        password_security.hash("a long secure passphrase"),
        "Example Owner",
        "active",
        True,
        2,
    )
    service = AuthenticationService(
        store=store,
        password_security=password_security,
        token_codec=AccessTokenCodec(SIGNING_KEY),
        token_hash_key=HASH_KEY,
        verification_sender=NullVerificationSender(),
        web_origin="https://app.example.test",
    )

    issued = await service.login(
        email="owner@example.com", password="a long secure passphrase"
    )

    assert issued.identity.user_id == USER_ID
    assert service.token_codec.verify(issued.access_token) is not None
    assert store.create_session.await_count == 1
    assert store.create_session.await_args.kwargs["refresh_hash"] == hash_token(
        issued.refresh_token, HASH_KEY
    )

    store.login_identity.return_value = None
    with pytest.raises(AuthenticationFailure):
        await service.login(email="unknown@example.com", password="wrong password")


async def test_refresh_authentication_logout_and_csrf_delegate_safely() -> None:
    store = AsyncMock()
    identity = SessionIdentity(SESSION_ID, USER_ID, 3)
    store.rotate_session.return_value = RotationResult(RotationState.ROTATED, identity)
    store.validate_session.return_value = identity
    store.validate_csrf.return_value = True
    service = _service(store)

    issued = await service.refresh("refresh-token-that-is-long-enough", "csrf-token")
    authenticated = await service.authenticate_access(issued.access_token)
    assert authenticated is not None
    assert authenticated.identity == identity
    assert await service.validate_csrf(identity, "csrf-token")
    await service.logout(identity)
    store.revoke_session.assert_awaited_once_with(
        user_id=USER_ID, session_id=SESSION_ID
    )

    store.rotate_session.return_value = RotationResult(RotationState.REPLAYED)
    with pytest.raises(AuthenticationFailure):
        await service.refresh("replayed-refresh-token")
    assert await service.authenticate_access("bad-token") is None
