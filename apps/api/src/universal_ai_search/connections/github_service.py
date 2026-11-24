"""Two single-use callbacks for installation and GitHub App user consent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag

from .crypto import EncryptedEnvelope, LocalEnvelopeEncryption, envelope_context
from .github import GitHubAuthorizationError, HttpGitHubAppGateway, _valid_id
from .store import OAuthTransaction

Stage = Literal["install", "consent"]
_RETURN_PATHS = frozenset({"/", "/settings/connections"})
_STATE = re.compile(r"[A-Za-z0-9_-]{64}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")


class GitHubConnectionStore(Protocol):
    async def create(
        self,
        *,
        transaction_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        state_hash: bytes,
        nonce_hash: bytes,
        envelope: EncryptedEnvelope,
        return_path: str,
        expires_at: datetime,
    ) -> None: ...

    async def consume(
        self, *, workspace_id: UUID, user_id: UUID, state_hash: bytes
    ) -> OAuthTransaction | None: ...

    async def save_grants(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        installation_id: int,
        repository_ids: frozenset[int],
    ) -> UUID: ...


@dataclass(frozen=True)
class GitHubAuthorizationStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True)
class GitHubAuthorizationComplete:
    connection_id: UUID
    return_path: str


class GitHubConnectionService:
    def __init__(
        self,
        *,
        store: GitHubConnectionStore,
        gateway: HttpGitHubAppGateway,
        encryption: LocalEnvelopeEncryption,
        hash_key: bytes,
        app_slug: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        if not _SLUG.fullmatch(app_slug) or len(hash_key) < 32 or not client_secret:
            raise ValueError("Invalid GitHub App configuration")
        gateway.authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state="test",
            challenge="test",
        )
        self._store = store
        self._gateway = gateway
        self._encryption = encryption
        self._hash_key = hash_key
        self._app_slug = app_slug
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def start(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        repository_ids: frozenset[int],
        return_path: str,
    ) -> GitHubAuthorizationStart:
        if (
            not 1 <= len(repository_ids) <= 500
            or not all(self._provider_id(item) for item in repository_ids)
            or return_path not in _RETURN_PATHS
        ):
            raise GitHubAuthorizationError
        state, expires_at = await self._create(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            stage="install",
            return_path=return_path,
            repository_ids=repository_ids,
        )
        url = f"https://github.com/apps/{self._app_slug}/installations/new"
        return GitHubAuthorizationStart(
            f"{url}?{urlencode({'state': state})}", expires_at
        )

    async def begin_consent(
        self, *, state: str, user_id: UUID, session_id: UUID, installation_id: int
    ) -> str:
        workspace_id, path, repository_ids, _, _ = await self._consume(
            state=state, user_id=user_id, session_id=session_id, stage="install"
        )
        if not self._provider_id(installation_id):
            raise GitHubAuthorizationError
        verifier = secrets.token_urlsafe(64)
        next_state, _ = await self._create(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            stage="consent",
            return_path=path,
            repository_ids=repository_ids,
            installation_id=installation_id,
            verifier=verifier,
        )
        return self._gateway.authorization_url(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            state=next_state,
            challenge=self._b64(hashlib.sha256(verifier.encode()).digest()),
        )

    async def complete(
        self, *, state: str, code: str, user_id: UUID, session_id: UUID
    ) -> GitHubAuthorizationComplete:
        workspace_id, path, repository_ids, installation_id, verifier = (
            await self._consume(
                state=state, user_id=user_id, session_id=session_id, stage="consent"
            )
        )
        if installation_id is None or verifier is None:
            raise GitHubAuthorizationError
        token = await self._gateway.exchange_user_code(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
            code=code,
            verifier=verifier,
        )
        await self._gateway.verify_selected_repositories(
            installation_id=installation_id,
            repository_ids=repository_ids,
            user_access_token=token,
        )
        connection_id = await self._store.save_grants(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            installation_id=installation_id,
            repository_ids=repository_ids,
        )
        return GitHubAuthorizationComplete(connection_id, path)

    async def deny(self, *, state: str, user_id: UUID, session_id: UUID) -> None:
        await self._consume(
            state=state, user_id=user_id, session_id=session_id, stage=None
        )

    async def _create(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        session_id: UUID,
        stage: Stage,
        return_path: str,
        repository_ids: frozenset[int],
        installation_id: int | None = None,
        verifier: str | None = None,
    ) -> tuple[str, datetime]:
        state = self._b64(workspace_id.bytes + secrets.token_bytes(32))
        transaction_id = uuid4()
        expiry = datetime.now(UTC) + timedelta(minutes=10)
        payload = {
            "session_id": str(session_id),
            "stage": stage,
            "repository_ids": sorted(repository_ids),
            "installation_id": installation_id,
            "verifier": verifier,
        }
        await self._store.create(
            transaction_id=transaction_id,
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=self._state_hash(state),
            nonce_hash=secrets.token_bytes(32),
            envelope=self._encryption.encrypt(
                json.dumps(payload).encode(),
                context=self._context(workspace_id, transaction_id),
            ),
            return_path=return_path,
            expires_at=expiry,
        )
        return state, expiry

    async def _consume(
        self, *, state: str, user_id: UUID, session_id: UUID, stage: Stage | None
    ) -> tuple[UUID, str, frozenset[int], int | None, str | None]:
        if not _STATE.fullmatch(state):
            raise GitHubAuthorizationError
        workspace_id = UUID(bytes=base64.urlsafe_b64decode(state)[:16])
        record = await self._store.consume(
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=self._state_hash(state),
        )
        if (
            record is None
            or record.workspace_id != workspace_id
            or record.user_id != user_id
        ):
            raise GitHubAuthorizationError
        try:
            payload = json.loads(
                self._encryption.decrypt(
                    record.encrypted_payload,
                    context=self._context(workspace_id, record.id),
                )
            )
            if (
                payload["session_id"] != str(session_id)
                or (stage is not None and payload["stage"] != stage)
                or record.redirect_path not in _RETURN_PATHS
            ):
                raise ValueError
            repository_ids = frozenset(payload["repository_ids"])
            installation_id = payload["installation_id"]
            verifier = payload["verifier"]
            if (
                not 1 <= len(repository_ids) <= 500
                or not all(self._provider_id(item) for item in repository_ids)
                or (
                    installation_id is not None
                    and not self._provider_id(installation_id)
                )
                or (verifier is not None and not isinstance(verifier, str))
            ):
                raise ValueError
        except (InvalidTag, KeyError, TypeError, ValueError):
            raise GitHubAuthorizationError from None
        return (
            workspace_id,
            record.redirect_path,
            repository_ids,
            installation_id,
            verifier,
        )

    def _state_hash(self, state: str) -> bytes:
        return hmac.digest(self._hash_key, f"github-app:{state}".encode(), "sha256")

    @staticmethod
    def _context(workspace_id: UUID, transaction_id: UUID) -> bytes:
        return envelope_context(
            provider="github_app",
            workspace_id=str(workspace_id),
            record_id=str(transaction_id),
            purpose="oauth-transaction",
        )

    @staticmethod
    def _provider_id(value: object) -> bool:
        return _valid_id(value) and value <= 2**53 - 1

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
