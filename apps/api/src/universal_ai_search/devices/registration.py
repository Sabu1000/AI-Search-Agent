"""Registration orchestration without HTTP, filesystem, or network authority."""

from __future__ import annotations

import base64
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from .security import DeviceProofError, verify_registration

DevicePlatform = Literal["macos", "windows"]
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_APP_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,50}")


class DeviceRegistrationRejected(Exception):
    """Generic denial; does not identify a challenge, session, or existing key."""


class DeviceRegistrationRateLimited(Exception):
    """Temporary per-session/workspace challenge cooldown."""


class DeviceRegistrationUnavailable(Exception):
    """Dependency or transaction unavailable; does not include SQL/bind values."""


@dataclass(frozen=True, slots=True)
class RegistrationScope:
    workspace_id: UUID
    user_id: UUID
    session_id: UUID
    user_authorization_version: int
    workspace_authorization_version: int


@dataclass(frozen=True, slots=True, repr=False)
class RegistrationChallenge:
    challenge_id: UUID
    challenge: str
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class RegisteredDevice:
    device_id: UUID
    device_credential: str


class DeviceRegistrationStore(Protocol):
    async def issue(self, scope: RegistrationScope, challenge_id: UUID) -> datetime: ...

    async def is_live(self, scope: RegistrationScope, challenge_id: UUID) -> bool: ...

    async def complete(
        self,
        *,
        scope: RegistrationScope,
        challenge_id: UUID,
        device_id: UUID,
        connection_id: UUID,
        public_key: bytes,
        credential_hash: bytes,
        platform: DevicePlatform,
        app_version: str,
        display_name: str,
    ) -> None: ...


class DeviceRegistrationService:
    def __init__(self, store: DeviceRegistrationStore, *, hash_key: bytes) -> None:
        if len(hash_key) < 32:
            raise ValueError("device registration key must be at least 32 bytes")
        self._store = store
        self._key = hash_key

    async def issue(self, scope: RegistrationScope) -> RegistrationChallenge:
        challenge_id = uuid4()
        expires_at = await self._store.issue(scope, challenge_id)
        challenge = (
            base64.urlsafe_b64encode(self._challenge(scope, challenge_id))
            .rstrip(b"=")
            .decode("ascii")
        )
        return RegistrationChallenge(challenge_id, challenge, expires_at)

    async def complete(
        self,
        *,
        scope: RegistrationScope,
        challenge_id: UUID,
        public_key: str,
        signature: str,
        platform: DevicePlatform,
        app_version: str,
        display_name: str,
    ) -> RegisteredDevice:
        display_name = display_name.strip()
        if (
            platform not in {"macos", "windows"}
            or not _APP_VERSION.fullmatch(app_version)
            or not 1 <= len(display_name) <= 150
            or not display_name.isprintable()
            or challenge_id.int == 0
        ):
            raise DeviceRegistrationRejected
        # Do not verify a challenge another workspace/session owns, and
        # recheck it again under the write boundary after proof verification.
        if not await self._store.is_live(scope, challenge_id):
            raise DeviceRegistrationRejected
        try:
            verified_key = verify_registration(
                challenge_id=challenge_id,
                challenge=self._challenge(scope, challenge_id),
                public_key=public_key,
                signature=signature,
            )
        except DeviceProofError:
            raise DeviceRegistrationRejected from None
        credential = secrets.token_urlsafe(32)
        device_id = uuid4()
        await self._store.complete(
            scope=scope,
            challenge_id=challenge_id,
            device_id=device_id,
            connection_id=uuid4(),
            public_key=verified_key,
            credential_hash=hash_device_credential(credential, self._key),
            platform=platform,
            app_version=app_version,
            display_name=display_name,
        )
        return RegisteredDevice(device_id, credential)

    def _challenge(self, scope: RegistrationScope, challenge_id: UUID) -> bytes:
        # Every ID is fixed-width; challenge derivation avoids storing or
        # logging a reusable message. The DB owns issue/expiry/consumption.
        return hmac.digest(
            self._key,
            b"uas-device-registration-challenge-v1\0"
            + scope.workspace_id.bytes
            + scope.user_id.bytes
            + scope.session_id.bytes
            + challenge_id.bytes,
            "sha256",
        )


def hash_device_credential(credential: str, hash_key: bytes) -> bytes:
    """Stable hash for future credential lookup, distinct from session hashes."""
    if len(hash_key) < 32 or not _TOKEN_SHAPE.fullmatch(credential):
        raise DeviceRegistrationRejected
    return hmac.digest(
        hash_key,
        b"uas-device-opaque-credential-v1\0" + credential.encode("ascii"),
        "sha256",
    )
