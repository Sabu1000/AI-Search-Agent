"""Single-use password recovery service; HTTP routing is a separate boundary."""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .recovery_email import RecoveryMail, RecoveryMailQueue
from .recovery_store import RecoveryStore
from .security import PasswordSecurity, hash_token, random_token

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")


class InvalidRecoveryToken(Exception):
    """A public reset token is invalid, expired, superseded, or used."""


class RecoveryPasswordRejected(Exception):
    """The new password did not meet the existing account password policy."""


class RecoveryRequestLimiter(Protocol):
    async def allow(self, *, ip_key: bytes, email_key: bytes) -> bool: ...


class PasswordRecoveryService:
    def __init__(
        self,
        *,
        store: RecoveryStore,
        security: PasswordSecurity,
        limiter: RecoveryRequestLimiter,
        mail: RecoveryMailQueue,
        token_hash_key: bytes,
        web_origin: str,
    ) -> None:
        if len(token_hash_key) < 32:
            raise ValueError("Password recovery hash key must be at least 32 bytes")
        self._origin = _origin(web_origin)
        self._store = store
        self._security = security
        self._limiter = limiter
        self._mail = mail
        self._token_hash_key = token_hash_key

    async def request_reset(self, *, email: str, client_ip: str) -> None:
        """Return no identity information and never await account-specific SMTP.

        Callers must supply a peer IP from a trusted transport/proxy boundary,
        and return the same generic 202 body on every ordinary request outcome.
        """
        loop = asyncio.get_running_loop()
        # Pad ordinary DB-backed issuance and unknown-account responses to the
        # same local floor. This does not promise constant network timing.
        floor = loop.time() + 0.100 + secrets.randbelow(26) / 1000
        try:
            await self._request_token(email=email, client_ip=client_ip)
        finally:
            remaining = floor - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _request_token(self, *, email: str, client_ip: str) -> None:
        if len(email) > 320 or len(client_ip) > 45 or "%" in client_ip:
            return
        normalized = email.strip().casefold()
        if (
            len(normalized) > 320
            or not _EMAIL.fullmatch(normalized)
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        ):
            return
        try:
            address = ip_address(client_ip)
        except ValueError:
            return
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not await self._limiter.allow(
            ip_key=self._digest("request-ip", address.compressed),
            email_key=self._digest("request-email", normalized),
        ):
            return

        token = random_token()
        recipient = await self._store.issue(
            email=normalized,
            token_id=uuid4(),
            token_hash=self._digest("reset-token", token),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        if recipient is not None:
            # The fragment is not sent in HTTP requests/referrers. The web
            # client must remove it from browser history before posting JSON.
            self._mail.enqueue(
                RecoveryMail(recipient, f"{self._origin}/#reset_token={token}")
            )

    async def reset_password(
        self, *, token: str, password: str, request_id: UUID
    ) -> None:
        if not _TOKEN.fullmatch(token):
            raise InvalidRecoveryToken()
        token_hash = self._digest("reset-token", token)
        target = await self._store.lookup(token_hash=token_hash)
        if target is None:
            raise InvalidRecoveryToken()
        try:
            self._security.validate(password, target.email)
            password_hash = self._security.hash(password)
        except ValueError:
            raise RecoveryPasswordRejected() from None
        recipient = await self._store.complete(
            token_hash=token_hash,
            user_id=target.user_id,
            expected_authorization_version=target.authorization_version,
            expected_email=target.email,
            password_hash=password_hash,
            audit_id=uuid4(),
            audit_subject_hash=self._digest("audit-user", str(target.user_id)),
            request_id=request_id,
        )
        if recipient is None:
            raise InvalidRecoveryToken()
        self._mail.enqueue(RecoveryMail(recipient))

    def _digest(self, purpose: str, value: str) -> bytes:
        return hash_token(f"password-recovery:{purpose}:{value}", self._token_hash_key)


def _origin(value: str) -> str:
    if (
        any(not 33 <= ord(char) <= 126 for char in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        raise ValueError("Invalid password recovery web origin")
    uri = urlsplit(value)
    if (
        uri.hostname is None
        or uri.username is not None
        or uri.password is not None
        or uri.path not in {"", "/"}
        or uri.query
        or uri.fragment
        or (
            uri.scheme != "https"
            and not (
                uri.scheme == "http"
                and uri.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("Invalid password recovery web origin")
    # Validate a configured explicit port as well as the host/path.
    _ = uri.port
    return f"{uri.scheme}://{uri.netloc}"
