"""Signed, bounded assertion that exactly one staged upload was verified."""

from __future__ import annotations

import base64
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from universal_ai_search.api.models import (
    CanonicalUUID,
    RequestModel,
    ResponseModel,
    UtcDateTime,
)

from .heartbeat import HeartbeatAuthority
from .registration import DeviceRegistrationRejected, hash_device_credential
from .security import (
    DeviceProofError,
    DeviceProofHeaders,
    hash_device_nonce,
    verify_request,
)
from .uploads import (
    UploadMimeType,
    UploadRejected,
    UploadRequestInvalid,
    UploadUnavailable,
    _unique_fields,
)

MAX_CHANGES_BODY_BYTES = 512
_OBJECT_KEY = re.compile(r"device-inbox/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_IMMUTABLE_VERSION = re.compile(r"[A-Za-z0-9+=/_~-]{1,256}")


class ChangeConflict(Exception):
    """Change batch already bound or being verified."""


class ChangeExpired(Exception):
    """The bounded verification window has expired."""


class ChangeRateLimited(Exception):
    """The signed device replay budget is exhausted."""


class ChangeInvalidObject(Exception):
    """Required immutable size, digest and type could not be matched."""


class ChangeRequest(RequestModel):
    change_batch_id: CanonicalUUID
    upload_ids: tuple[CanonicalUUID]


@dataclass(frozen=True, slots=True, repr=False)
class ChangeClaim:
    device_id: UUID
    credential_hash: bytes
    public_key: bytes
    signed_at: datetime
    nonce_hash: bytes
    batch_id: UUID
    upload_id: UUID


@dataclass(frozen=True, slots=True, repr=False)
class ChangeCandidate:
    upload_id: UUID
    object_key: str
    size_bytes: int
    content_sha256: str
    mime_type: UploadMimeType


class VerifiedChange(ResponseModel):
    status: Literal["verified"]
    id: CanonicalUUID
    created_at: UtcDateTime
    completed_at: UtcDateTime


@dataclass(frozen=True, slots=True, repr=False)
class ImmutableObject:
    version_id: str
    size_bytes: int
    checksum_sha256: str
    mime_type: str


class ImmutableStageReader(Protocol):
    """Trusted reader: only opaque staging keys in a verified versioned bucket."""

    async def get(self, object_key: str) -> ImmutableObject: ...


class ChangeStore(Protocol):
    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None: ...

    async def prepare(self, claim: ChangeClaim) -> ChangeCandidate | VerifiedChange: ...

    async def complete(
        self, claim: ChangeClaim, candidate: ChangeCandidate, version: str
    ) -> VerifiedChange: ...


def parse_change(body: bytes) -> ChangeRequest:
    if not 1 <= len(body) <= MAX_CHANGES_BODY_BYTES:
        raise UploadRequestInvalid
    try:
        raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_fields)
        parsed = ChangeRequest.model_validate(raw)
        if (
            parsed.change_batch_id.int == 0
            or parsed.upload_ids[0].int == 0
            or raw["change_batch_id"] != str(parsed.change_batch_id)
            or raw["upload_ids"] != [str(parsed.upload_ids[0])]
        ):
            raise ValueError("noncanonical IDs")
        return parsed
    except (ValueError, ValidationError, RecursionError):
        raise UploadRequestInvalid from None


class DeviceUploadCompletion:
    def __init__(
        self, store: ChangeStore, *, reader: ImmutableStageReader, hash_key: bytes
    ) -> None:
        if len(hash_key) < 32:
            raise ValueError("device upload key must be at least 32 bytes")
        self._store = store
        self._reader = reader
        self._key = hash_key

    async def accept(
        self,
        *,
        device_id: UUID,
        credential: str,
        raw_target: bytes,
        headers: DeviceProofHeaders,
        exact_body: bytes,
    ) -> VerifiedChange:
        if (
            device_id.int == 0
            or raw_target != f"/v1/devices/{device_id}/changes".encode()
        ):
            raise UploadRejected
        if not 1 <= len(exact_body) <= MAX_CHANGES_BODY_BYTES:
            raise UploadRequestInvalid
        try:
            digest = hash_device_credential(credential, self._key)
        except DeviceRegistrationRejected:
            raise UploadRejected from None
        authority = await self._store.lookup(device_id, digest)
        if authority is None:
            raise UploadRejected
        try:
            signed = verify_request(
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
        parsed = parse_change(exact_body)
        claim = ChangeClaim(
            device_id,
            digest,
            authority.public_key,
            signed.signed_at,
            hash_device_nonce(signed.nonce),
            parsed.change_batch_id,
            parsed.upload_ids[0],
        )
        candidate = await self._store.prepare(claim)
        if isinstance(candidate, VerifiedChange):
            return candidate
        if candidate.upload_id != claim.upload_id or not _OBJECT_KEY.fullmatch(
            candidate.object_key
        ):
            raise UploadUnavailable
        obj = await self._reader.get(candidate.object_key)
        digest_bytes = base64.urlsafe_b64decode(candidate.content_sha256 + "=")
        checksum = base64.b64encode(digest_bytes).decode("ascii")
        if (
            not _IMMUTABLE_VERSION.fullmatch(obj.version_id)
            or obj.version_id == "null"
            or obj.size_bytes != candidate.size_bytes
            or obj.mime_type != candidate.mime_type
            or not obj.checksum_sha256.isascii()
            or not hmac.compare_digest(obj.checksum_sha256, checksum)
        ):
            raise ChangeInvalidObject
        return await self._store.complete(claim, candidate, obj.version_id)
