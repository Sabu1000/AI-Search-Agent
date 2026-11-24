"""Validate and reserve native upload authorization without minting a URL."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, ValidationError

from universal_ai_search.api.models import CanonicalUUID, RequestModel

from .heartbeat import HeartbeatAuthority
from .manifests import MAX_FILE_BYTES
from .registration import DeviceRegistrationRejected, hash_device_credential
from .security import (
    DeviceProofError,
    DeviceProofHeaders,
    hash_device_nonce,
    verify_request,
)

MAX_UPLOAD_SIGN_BODY_BYTES = 2048
UPLOAD_GRANT_TTL = timedelta(minutes=10)
UploadMimeType = Literal["text/markdown", "text/plain", "text/csv"]


class UploadRejected(Exception):
    """Generic rejection for proof, authority, intent, or metadata mismatch."""


class UploadRequestInvalid(Exception):
    """Signed request is invalid."""


class UploadQuotaExceeded(Exception):
    """Existing usage plus unsettled reservations exhaust the workspace quota."""


class UploadReservationExpired(Exception):
    """Expired authorization cannot be extended or used to mint another URL."""


class UploadRateLimited(Exception):
    """Durable per-device nonce or workspace upload reserve limit reached."""


class UploadUnavailable(Exception):
    """Persistence or verified provider support is unavailable."""


class UploadSignRequest(RequestModel):
    upload_id: CanonicalUUID
    manifest_id: CanonicalUUID
    folder_id: CanonicalUUID
    external_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$", repr=False)]
    size_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_FILE_BYTES)]
    mime_type: UploadMimeType
    content_sha256: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)]


@dataclass(frozen=True, slots=True, repr=False)
class UploadReserve:
    device_id: UUID
    credential_hash: bytes
    public_key: bytes
    signed_at: datetime
    nonce_hash: bytes
    request: UploadSignRequest


@dataclass(frozen=True, slots=True, repr=False)
class UploadGrant:
    """Internal, durable authorization; never a capability or a public response."""

    upload_id: UUID
    workspace_id: UUID
    object_key: str
    issued_at: datetime
    expires_at: datetime
    size_bytes: int
    mime_type: UploadMimeType
    content_sha256: str

    def required_headers(self) -> dict[str, str]:
        """Provider must enforce all four, not simply return them as hints."""
        digest = base64.urlsafe_b64decode(self.content_sha256 + "=")
        return {
            "Content-Type": self.mime_type,
            "Content-Length": str(self.size_bytes),
            "x-amz-checksum-sha256": base64.b64encode(digest).decode("ascii"),
            "If-None-Match": "*",
        }


class UploadStore(Protocol):
    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None: ...

    async def reserve(self, data: UploadReserve) -> UploadGrant: ...


class DeviceUploadAuthorization:
    """Future transport must fail closed unless a safe signer is configured.

    Do not call this service as a fallback merely to return success/placeholder.
    The real provider must bind method PUT, the exact opaque key, all required
    headers, and the existing expiry; enforce write-once even on retry, and
    prevent deletes and delete markers while a grant can still be used.
    """

    def __init__(self, store: UploadStore, *, hash_key: bytes) -> None:
        if len(hash_key) < 32:
            raise ValueError("device upload key must be at least 32 bytes")
        self._store = store
        self._key = hash_key

    async def reserve(
        self,
        *,
        device_id: UUID,
        credential: str,
        raw_target: bytes,
        headers: DeviceProofHeaders,
        exact_body: bytes,
    ) -> UploadGrant:
        if (
            device_id.int == 0
            or raw_target != f"/v1/devices/{device_id}/uploads:sign".encode()
        ):
            raise UploadRejected
        if not 1 <= len(exact_body) <= MAX_UPLOAD_SIGN_BODY_BYTES:
            raise UploadRequestInvalid
        try:
            digest = hash_device_credential(credential, self._key)
        except DeviceRegistrationRejected:
            raise UploadRejected from None
        authority = await self._store.lookup(device_id, digest)
        if authority is None:
            raise UploadRejected
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
            raise UploadRejected from None
        request = parse_upload_request(exact_body)
        return await self._store.reserve(
            UploadReserve(
                device_id,
                digest,
                authority.public_key,
                proof.signed_at,
                hash_device_nonce(proof.nonce),
                request,
            )
        )


def _unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, value in items:
        if name in values:
            raise ValueError("duplicate field")
        values[name] = value
    return values


def parse_upload_request(exact_body: bytes) -> UploadSignRequest:
    if not 1 <= len(exact_body) <= MAX_UPLOAD_SIGN_BODY_BYTES:
        raise UploadRequestInvalid
    try:
        raw = json.loads(exact_body.decode("utf-8"), object_pairs_hook=_unique_fields)
        parsed = UploadSignRequest.model_validate(raw)
        for field in ("upload_id", "manifest_id", "folder_id"):
            value = getattr(parsed, field)
            if value.int == 0 or raw[field] != str(value):
                raise ValueError("noncanonical identifier")
        content_digest = base64.b64decode(
            parsed.content_sha256 + "=", b"-_", validate=True
        )
        if (
            len(content_digest) != 32
            or base64.urlsafe_b64encode(content_digest).decode()[:-1]
            != parsed.content_sha256
        ):
            raise ValueError("noncanonical digest")
        return parsed
    except (ValueError, ValidationError, binascii.Error, RecursionError):
        raise UploadRequestInvalid from None
