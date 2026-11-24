"""Authenticate a bounded native heartbeat without granting upload authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .registration import DeviceRegistrationRejected, hash_device_credential
from .security import (
    DeviceProofError,
    DeviceProofHeaders,
    hash_device_nonce,
    verify_request,
)


class DeviceHeartbeatRejected(Exception):
    """Generic denial for credentials, authority, proof, or nonce replay."""


class DeviceHeartbeatRateLimited(Exception):
    """A valid new heartbeat arrived inside the per-device cooldown."""


class DeviceHeartbeatUnavailable(Exception):
    """The database boundary could not complete the heartbeat."""


@dataclass(frozen=True, slots=True, repr=False)
class HeartbeatAuthority:
    public_key: bytes
    now: datetime


@dataclass(frozen=True, slots=True, repr=False)
class HeartbeatClaim:
    device_id: UUID
    credential_hash: bytes
    public_key: bytes
    signed_at: datetime
    nonce_hash: bytes


class DeviceHeartbeatStore(Protocol):
    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None: ...

    async def touch(self, claim: HeartbeatClaim) -> None: ...


class DeviceHeartbeatService:
    def __init__(self, store: DeviceHeartbeatStore, *, hash_key: bytes) -> None:
        if len(hash_key) < 32:
            raise ValueError("device heartbeat key must be at least 32 bytes")
        self._store = store
        self._key = hash_key

    async def accept(
        self,
        *,
        device_id: UUID,
        credential: str,
        raw_target: bytes,
        headers: DeviceProofHeaders,
        exact_body: bytes = b"",
    ) -> None:
        if device_id.int == 0 or raw_target != (
            f"/v1/devices/{device_id}/heartbeat".encode("ascii")
        ):
            raise DeviceHeartbeatRejected
        try:
            credential_hash = hash_device_credential(credential, self._key)
        except DeviceRegistrationRejected:
            raise DeviceHeartbeatRejected from None
        authority = await self._store.lookup(device_id, credential_hash)
        if authority is None:
            raise DeviceHeartbeatRejected
        try:
            proof = verify_request(
                expected_device_id=device_id,
                registered_public_key=authority.public_key,
                method="POST",
                raw_request_target=raw_target,
                headers=headers,
                exact_body=exact_body,
                now=authority.now,
            )
        except DeviceProofError:
            raise DeviceHeartbeatRejected from None
        # Cross-endpoint domain: future device operations must claim the same
        # per-device nonce namespace, independent of request ID or route.
        nonce_hash = hash_device_nonce(proof.nonce)
        await self._store.touch(
            HeartbeatClaim(
                device_id,
                credential_hash,
                authority.public_key,
                proof.signed_at,
                nonce_hash,
            )
        )
