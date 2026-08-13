"""Authentication application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid4

from universal_ai_search.auth.email import VerificationSender
from universal_ai_search.auth.security import (
    AccessTokenCodec,
    PasswordSecurity,
    hash_token,
    random_token,
)
from universal_ai_search.auth.store import (
    AuthStore,
    Membership,
    RotationState,
    SessionIdentity,
    UserAccount,
)


class AuthenticationFailure(Exception):
    pass


class InvalidVerificationToken(Exception):
    pass


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    csrf_token: str
    identity: SessionIdentity


@dataclass(frozen=True)
class CurrentAccount:
    account: UserAccount
    memberships: list[Membership]


@dataclass(frozen=True)
class AuthenticatedSession:
    identity: SessionIdentity
    access_expires_at: datetime


class AuthenticationService:
    def __init__(
        self,
        *,
        store: AuthStore,
        password_security: PasswordSecurity,
        token_codec: AccessTokenCodec,
        token_hash_key: bytes,
        verification_sender: VerificationSender,
        web_origin: str,
    ) -> None:
        self.store = store
        self.password_security = password_security
        self.token_codec = token_codec
        self.token_hash_key = token_hash_key
        self.verification_sender = verification_sender
        self.web_origin = web_origin.rstrip("/")

    async def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str,
        terms_version: str,
        locale: str,
    ) -> None:
        normalized_email = email.strip().casefold()
        normalized_name = full_name.strip()
        self.password_security.validate(password, normalized_email)
        raw_token = random_token()
        created = await self.store.register(
            user_id=uuid4(),
            email=normalized_email,
            password_hash=self.password_security.hash(password),
            full_name=normalized_name,
            terms_version=terms_version,
            locale=locale,
            token_id=uuid4(),
            token_hash=hash_token(raw_token, self.token_hash_key),
            token_expiry=datetime.now(UTC) + timedelta(hours=24),
        )
        if created:
            await self.verification_sender.send_verification(
                recipient=normalized_email,
                url=f"{self.web_origin}/?verify_token={quote(raw_token)}",
            )

    async def verify_email(self, raw_token: str) -> None:
        verified = await self.store.verify_email(
            token_hash=hash_token(raw_token, self.token_hash_key),
            workspace_id=uuid4(),
        )
        if verified is None:
            raise InvalidVerificationToken

    async def login(
        self,
        *,
        email: str,
        password: str,
        device_metadata: dict[str, str] | None = None,
    ) -> IssuedSession:
        identity = await self.store.login_identity(email.strip().casefold())
        password_hash = identity.password_hash if identity else None
        password_matches = self.password_security.verify(password_hash, password)
        if (
            identity is None
            or not password_matches
            or identity.status != "active"
            or not identity.email_verified
        ):
            raise AuthenticationFailure
        if self.password_security.needs_rehash(identity.password_hash or ""):
            await self.store.update_password_hash(
                identity.user_id, self.password_security.hash(password)
            )
        return await self._new_session(
            user_id=identity.user_id,
            authorization_version=identity.authorization_version,
            device_metadata=device_metadata or {},
        )

    async def _new_session(
        self,
        *,
        user_id: UUID,
        authorization_version: int,
        device_metadata: dict[str, str],
    ) -> IssuedSession:
        session_id = uuid4()
        family_id = uuid4()
        refresh_token = random_token()
        csrf_token = random_token()
        expires_at = datetime.now(UTC) + timedelta(days=30)
        await self.store.create_session(
            session_id=session_id,
            user_id=user_id,
            family_id=family_id,
            refresh_hash=hash_token(refresh_token, self.token_hash_key),
            csrf_hash=hash_token(csrf_token, self.token_hash_key),
            authorization_version=authorization_version,
            expires_at=expires_at,
            device_metadata=device_metadata,
        )
        access_token, access_expiry = self.token_codec.issue(
            user_id=user_id,
            session_id=session_id,
            authorization_version=authorization_version,
        )
        return IssuedSession(
            access_token,
            access_expiry,
            refresh_token,
            csrf_token,
            SessionIdentity(session_id, user_id, authorization_version),
        )

    async def refresh(
        self, refresh_token: str, csrf_token: str | None = None
    ) -> IssuedSession:
        successor_id = uuid4()
        successor_refresh = random_token()
        successor_csrf = random_token()
        result = await self.store.rotate_session(
            refresh_hash=hash_token(refresh_token, self.token_hash_key),
            presented_csrf_hash=(
                hash_token(csrf_token, self.token_hash_key) if csrf_token else None
            ),
            successor_id=successor_id,
            successor_refresh_hash=hash_token(successor_refresh, self.token_hash_key),
            successor_csrf_hash=hash_token(successor_csrf, self.token_hash_key),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        if result.state is not RotationState.ROTATED or result.identity is None:
            raise AuthenticationFailure
        access_token, access_expiry = self.token_codec.issue(
            user_id=result.identity.user_id,
            session_id=result.identity.session_id,
            authorization_version=result.identity.authorization_version,
        )
        return IssuedSession(
            access_token,
            access_expiry,
            successor_refresh,
            successor_csrf,
            result.identity,
        )

    async def authenticate_access(
        self, access_token: str
    ) -> AuthenticatedSession | None:
        payload = self.token_codec.verify(access_token)
        if payload is None:
            return None
        identity = await self.store.validate_session(
            user_id=UUID(payload["sub"]),
            session_id=UUID(payload["session_id"]),
            authorization_version=payload["auth_version"],
        )
        if identity is None:
            return None
        return AuthenticatedSession(
            identity=identity,
            access_expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    async def logout(self, identity: SessionIdentity) -> None:
        await self.store.revoke_session(
            user_id=identity.user_id, session_id=identity.session_id
        )

    async def validate_csrf(self, identity: SessionIdentity, csrf_token: str) -> bool:
        return await self.store.validate_csrf(
            user_id=identity.user_id,
            session_id=identity.session_id,
            csrf_hash=hash_token(csrf_token, self.token_hash_key),
        )

    async def current_account(self, user_id: UUID) -> CurrentAccount | None:
        account = await self.store.account(user_id)
        if account is None:
            return None
        return CurrentAccount(account, await self.store.memberships(user_id))
