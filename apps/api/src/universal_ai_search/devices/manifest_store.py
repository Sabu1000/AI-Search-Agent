"""Execute the scoped manifest proof result within one durable transaction."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .heartbeat import HeartbeatAuthority
from .manifests import (
    ManifestConflict,
    ManifestRateLimited,
    ManifestRejected,
    ManifestResponse,
    ManifestUnavailable,
    ManifestWrite,
)


class SQLAlchemyDeviceManifestStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None:
        try:
            async with self._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT public_key, checked_at FROM "
                            "app.lookup_device_manifest(:device, :credential)"
                        ),
                        {"device": device_id, "credential": credential_hash},
                    )
                ).one_or_none()
                if row is None:
                    return None
                return HeartbeatAuthority(
                    bytes(row.public_key), cast(datetime, row.checked_at)
                )
        except SQLAlchemyError:
            raise ManifestUnavailable from None

    async def commit(self, data: ManifestWrite) -> ManifestResponse:
        # Do not persist device-local path text in historical receipt rows.
        entries = [
            {
                **entry.model_dump(mode="json", exclude={"relative_path"}),
                "relative_path_sha256": hashlib.sha256(
                    entry.relative_path.encode("utf-8")
                ).hexdigest(),
            }
            for entry in data.accepted
        ]
        try:
            async with self._engine.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT app.commit_device_manifest("
                        ":device, :credential, :public_key, :signed_at, "
                        ":nonce_hash, :body_hash,"
                        ":folder, :manifest, :sequence, :final, "
                        "CAST(:entries AS jsonb), CAST(:skipped AS jsonb))"
                    ),
                    {
                        "device": data.device_id,
                        "credential": data.credential_hash,
                        "public_key": data.public_key,
                        "signed_at": data.signed_at,
                        "nonce_hash": data.nonce_hash,
                        "body_hash": data.body_hash,
                        "folder": data.page.folder_id,
                        "manifest": data.page.manifest_id,
                        "sequence": data.page.sequence,
                        "final": data.page.final,
                        "entries": json.dumps(entries, separators=(",", ":")),
                        "skipped": json.dumps(
                            [item.model_dump(mode="json") for item in data.skipped],
                            separators=(",", ":"),
                        ),
                    },
                )
                if not isinstance(result, dict):
                    raise ManifestUnavailable
                status = result.get("status")
                if status == "accepted":
                    return ManifestResponse.model_validate(result.get("page"))
                if status not in {"conflict", "rejected", "rate_limited"}:
                    raise ManifestUnavailable
                next_sequence = result.get("next_sequence")
        except (SQLAlchemyError, ValidationError):
            raise ManifestUnavailable from None
        # Authenticated conflicts consume their exact nonce, preventing replay
        # when a later request moves the sequence forward.
        if status == "conflict" and type(next_sequence) is int:
            raise ManifestConflict(next_sequence)
        if status == "rejected":
            raise ManifestRejected
        if status == "rate_limited":
            raise ManifestRateLimited
        raise ManifestUnavailable
