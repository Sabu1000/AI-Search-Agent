"""Narrow database adapter for proved native heartbeat requests."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .heartbeat import (
    DeviceHeartbeatRateLimited,
    DeviceHeartbeatRejected,
    DeviceHeartbeatUnavailable,
    HeartbeatAuthority,
    HeartbeatClaim,
)


class SQLAlchemyDeviceHeartbeatStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None:
        try:
            async with self._engine.connect() as connection:
                match = (
                    await connection.execute(
                        text(
                            "SELECT public_key, checked_at FROM "
                            "app.lookup_device_heartbeat(:device_id, :credential_hash)"
                        ),
                        {"device_id": device_id, "credential_hash": credential_hash},
                    )
                ).one_or_none()
                if match is None:
                    return None
                return HeartbeatAuthority(
                    bytes(match.public_key), cast(datetime, match.checked_at)
                )
        except SQLAlchemyError:
            raise DeviceHeartbeatUnavailable from None

    async def touch(self, claim: HeartbeatClaim) -> None:
        try:
            async with self._engine.begin() as connection:
                outcome = await connection.scalar(
                    text(
                        "SELECT app.claim_device_heartbeat("
                        ":device_id, :credential_hash, :public_key, "
                        ":signed_at, :nonce_hash)"
                    ),
                    {
                        "device_id": claim.device_id,
                        "credential_hash": claim.credential_hash,
                        "public_key": claim.public_key,
                        "signed_at": claim.signed_at,
                        "nonce_hash": claim.nonce_hash,
                    },
                )
                if outcome == "rejected":
                    raise DeviceHeartbeatRejected
                if outcome == "rate_limited":
                    raise DeviceHeartbeatRateLimited
                if outcome != "accepted":
                    raise DeviceHeartbeatUnavailable
        except SQLAlchemyError:
            raise DeviceHeartbeatUnavailable from None
