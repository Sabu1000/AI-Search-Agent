"""Single-use Google authorization orchestration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag

from .crypto import LocalEnvelopeEncryption, envelope_context
from .google import (
    DRIVE_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GoogleGateway,
    GoogleProviderError,
)
from .store import GoogleConnectionStore

SOURCE_SCOPES = {
    "gmail": GMAIL_READONLY_SCOPE,
    "google_drive": DRIVE_READONLY_SCOPE,
}


class GoogleAuthorizationError(Exception):
    pass


class GoogleAuthorizationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizationComplete:
    connection_id: UUID
    return_path: str


class GoogleConnectionService:
    def __init__(
        self,
        *,
        store: GoogleConnectionStore,
        gateway: GoogleGateway,
        encryption: LocalEnvelopeEncryption,
        hash_key: bytes,
        enabled: bool,
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("OAuth hash key must be at least 32 bytes")
        self._store = store
        self._gateway = gateway
        self._encryption = encryption
        self._hash_key = hash_key
        self._enabled = enabled

    async def start(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        source_families: tuple[str, ...],
        return_path: str,
    ) -> AuthorizationStart:
        if not self._enabled:
            raise GoogleAuthorizationUnavailable
        requested = tuple(sorted(set(source_families)))
        if not requested or any(item not in SOURCE_SCOPES for item in requested):
            raise GoogleAuthorizationError
        transaction_id = uuid4()
        state = self._state(workspace_id)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = self._b64(hashlib.sha256(verifier.encode()).digest())
        expires_at = datetime.now(UTC) + timedelta(minutes=10)
        payload = json.dumps(
            {
                "pkce_verifier": verifier,
                "session_id": str(session_id),
                "source_families": list(requested),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        context = envelope_context(
            provider="google",
            workspace_id=str(workspace_id),
            record_id=str(transaction_id),
            purpose="oauth-transaction",
        )
        await self._store.create_transaction(
            transaction_id=transaction_id,
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=self._hash(state),
            nonce_hash=self._hash(nonce),
            encrypted_payload=self._encryption.encrypt(payload, context=context),
            redirect_path=return_path,
            expires_at=expires_at,
        )
        scopes = frozenset(SOURCE_SCOPES[item] for item in requested)
        return AuthorizationStart(
            authorization_url=self._gateway.authorization_url(
                state=state, challenge=challenge, scopes=scopes
            ),
            expires_at=expires_at,
        )

    async def complete(
        self,
        *,
        state: str,
        code: str,
        user_id: UUID,
        session_id: UUID,
    ) -> AuthorizationComplete:
        if not self._enabled:
            raise GoogleAuthorizationUnavailable
        workspace_id = self.workspace_from_state(state)
        transaction = await self._store.consume_transaction(
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=self._hash(state),
        )
        if transaction is None:
            raise GoogleAuthorizationError
        context = envelope_context(
            provider="google",
            workspace_id=str(workspace_id),
            record_id=str(transaction.id),
            purpose="oauth-transaction",
        )
        try:
            raw_payload = self._encryption.decrypt(
                transaction.encrypted_payload, context=context
            )
            payload = json.loads(raw_payload)
            if payload["session_id"] != str(session_id):
                raise GoogleAuthorizationError
            source_families = tuple(payload["source_families"])
            verifier = payload["pkce_verifier"]
            if not isinstance(verifier, str) or not all(
                isinstance(item, str) for item in source_families
            ):
                raise GoogleAuthorizationError
            expected_scopes = frozenset(SOURCE_SCOPES[item] for item in source_families)
        except (
            InvalidTag,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise GoogleAuthorizationError from error
        try:
            tokens = await self._gateway.exchange_code(code=code, verifier=verifier)
            if tokens.scopes != expected_scopes or not tokens.refresh_token:
                raise GoogleAuthorizationError
            account = await self._gateway.account(
                access_token=tokens.access_token,
                source_families=source_families,
            )
        except GoogleProviderError as error:
            raise GoogleAuthorizationError from error
        account_hash = self._hash(account.external_id)
        connection_id = await self._store.connection_id_for_account(
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=account_hash,
            proposed_id=uuid4(),
        )
        credential_payload = json.dumps(
            {
                "access_token": tokens.access_token,
                "expires_at": tokens.expires_at.isoformat(),
                "refresh_token": tokens.refresh_token,
                "schema_version": 1,
                "scopes": sorted(tokens.scopes),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        credential_context = envelope_context(
            provider="google",
            workspace_id=str(workspace_id),
            record_id=str(connection_id),
            purpose="provider-credential",
        )
        effective_id = await self._store.save_connection(
            connection_id=connection_id,
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=account_hash,
            display_label=account.display_label,
            credentials=self._encryption.encrypt(
                credential_payload, context=credential_context
            ),
            scopes=tokens.scopes,
            source_families=source_families,
        )
        return AuthorizationComplete(effective_id, transaction.redirect_path)

    async def consume_error(
        self, *, state: str, user_id: UUID, session_id: UUID
    ) -> str:
        """Consume a denied/provider-error callback without retaining its details."""

        workspace_id = self.workspace_from_state(state)
        transaction = await self._store.consume_transaction(
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=self._hash(state),
        )
        if transaction is None:
            raise GoogleAuthorizationError
        context = envelope_context(
            provider="google",
            workspace_id=str(workspace_id),
            record_id=str(transaction.id),
            purpose="oauth-transaction",
        )
        try:
            payload = json.loads(
                self._encryption.decrypt(transaction.encrypted_payload, context=context)
            )
            if payload["session_id"] != str(session_id):
                raise GoogleAuthorizationError
        except (
            InvalidTag,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise GoogleAuthorizationError from error
        return transaction.redirect_path

    def workspace_from_state(self, state: str) -> UUID:
        try:
            padded = state + "=" * (-len(state) % 4)
            raw = base64.urlsafe_b64decode(padded.encode())
            if len(raw) != 48:
                raise ValueError
            return UUID(bytes=raw[:16])
        except (ValueError, TypeError) as error:
            raise GoogleAuthorizationError from error

    def _state(self, workspace_id: UUID) -> str:
        return self._b64(workspace_id.bytes + secrets.token_bytes(32))

    def _hash(self, value: str) -> bytes:
        return hmac.digest(self._hash_key, value.encode(), "sha256")

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
