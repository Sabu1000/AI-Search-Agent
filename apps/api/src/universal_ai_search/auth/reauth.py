"""Obtain and validate recent-password proof without performing an action."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

import anyio
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncConnection

from .reauth_store import ReauthenticationStore, ReauthPurpose, ReauthScope
from .recovery_limiter import RedisScriptClient
from .security import PasswordSecurity, random_token

_PROOF = re.compile(r"[A-Za-z0-9_-]{43}")
_ALLOWED = frozenset({"disconnect", "export", "delete_account"})
_CONSUME_ATTEMPT = """
local attempts = tonumber(redis.call('GET', KEYS[1]) or '0')
if attempts >= tonumber(ARGV[1]) then return 0 end
if redis.call('INCR', KEYS[1]) == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 1
"""


class ReauthenticationFailed(Exception):
    """Generic bad password, stale authority or invalid proof."""


class ReauthenticationLimited(Exception):
    """This actor's attempt budget is exhausted."""


class ReauthenticationUnavailable(Exception):
    """Security dependencies are unavailable; no proof was issued."""


class ReauthLimiter(Protocol):
    async def allow(self, *, user_key: bytes) -> bool: ...


class RedisReauthLimiter:
    def __init__(self, redis: RedisScriptClient) -> None:
        self._redis = redis

    async def allow(self, *, user_key: bytes) -> bool:
        try:
            outcome = await self._redis.eval(
                _CONSUME_ATTEMPT, 1, f"uas:reauth:actor:{user_key.hex()}", "5", "300"
            )
        except RedisError:
            raise ReauthenticationUnavailable from None
        if type(outcome) is not int or outcome not in {0, 1}:
            raise ReauthenticationUnavailable
        return outcome == 1


@dataclass(frozen=True)
class IssuedReauthentication:
    token: str = field(repr=False)
    expires_at: datetime


class ReauthenticationService:
    def __init__(
        self,
        *,
        store: ReauthenticationStore,
        security: PasswordSecurity,
        limiter: ReauthLimiter,
        hash_key: bytes,
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("reauthentication hash key must be at least 32 bytes")
        self._store = store
        self._security = security
        self._limiter = limiter
        self._hash_key = hash_key
        self._password_workers = anyio.CapacityLimiter(4)

    async def obtain(
        self,
        *,
        scope: ReauthScope,
        purpose: ReauthPurpose,
        password: str,
        request_id: UUID,
    ) -> IssuedReauthentication:
        if (
            purpose not in _ALLOWED
            or not isinstance(password, str)
            or not 1 <= len(password) <= 128
            or any(
                ord(character) < 32 or ord(character) == 127 for character in password
            )
        ):
            raise ReauthenticationFailed
        try:
            password.encode("utf-8")
        except UnicodeEncodeError:
            raise ReauthenticationFailed from None
        if not await self._limiter.allow(
            user_key=self._digest(b"rate", scope.user_id.bytes)
        ):
            raise ReauthenticationLimited
        password_hash = await self._store.password_hash(scope=scope, purpose=purpose)
        valid = await anyio.to_thread.run_sync(
            self._security.verify,
            password_hash,
            password,
            limiter=self._password_workers,
        )
        if not valid or password_hash is None:
            raise ReauthenticationFailed

        proof = random_token()
        expiry = await self._store.issue(
            scope=scope,
            purpose=purpose,
            expected_hash=password_hash,
            token_hash=self._digest(b"proof", proof.encode()),
            request_id=request_id,
            audit_subject=self._digest(b"audit-user", scope.user_id.bytes),
            audit_workspace=self._digest(b"audit-workspace", scope.workspace_id.bytes),
        )
        if expiry is None:
            raise ReauthenticationFailed
        return IssuedReauthentication(proof, expiry)

    async def consume(
        self,
        connection: AsyncConnection,
        *,
        scope: ReauthScope,
        purpose: ReauthPurpose,
        token: str,
    ) -> None:
        if purpose not in _ALLOWED:
            raise ReauthenticationFailed
        valid = await self._store.consume(
            connection,
            scope=scope,
            purpose=purpose,
            token_hash=self.proof_hash(token),
        )
        if not valid:
            raise ReauthenticationFailed

    def proof_hash(self, token: str) -> bytes:
        """Validate syntax; protected SQL must still verify and consume the proof."""
        if not _PROOF.fullmatch(token):
            raise ReauthenticationFailed
        return self._digest(b"proof", token.encode())

    def _digest(self, domain: bytes, value: bytes) -> bytes:
        return hmac.digest(
            self._hash_key, b"reauthentication:" + domain + b":" + value, "sha256"
        )
