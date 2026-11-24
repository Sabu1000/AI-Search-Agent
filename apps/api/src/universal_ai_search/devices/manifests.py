"""Validate an exact signed metadata page; never infer delete or accept files."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, ValidationError

from universal_ai_search.api.models import CanonicalUUID, RequestModel, ResponseModel

from .heartbeat import HeartbeatAuthority
from .registration import DeviceRegistrationRejected, hash_device_credential
from .security import (
    DeviceProofError,
    DeviceProofHeaders,
    hash_device_nonce,
    verify_request,
)

MAX_ENTRIES = 1_000
MAX_FILE_BYTES = 100_000_000
MAX_SEQUENCE = (1 << 53) - 2  # reserve one exact JS integer for next_sequence
_EXTENSIONS = {
    "text/markdown": {".md", ".markdown"},
    "text/plain": {".txt"},
    "text/csv": {".csv"},
}
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(10)}
)
_RESERVED = '\\/:*?"<>|'


class ManifestRejected(Exception):
    """Token, proof, scope, or nonce did not authorize this operation."""


class ManifestInvalid(Exception):
    """Authenticated page is malformed or has an unsafe relative path."""


class ManifestConflict(Exception):
    def __init__(self, next_sequence: int) -> None:
        self.next_sequence = next_sequence
        super().__init__("manifest conflict")


class ManifestRateLimited(Exception):
    """Bound for live per-device nonce receipts."""


class ManifestUnavailable(Exception):
    """A required transaction or dependency is unavailable."""


class ManifestEntry(RequestModel):
    external_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$", repr=False)]
    relative_path: Annotated[str, Field(min_length=1, max_length=1024, repr=False)]
    size_bytes: Annotated[int, Field(strict=True, ge=0, le=(1 << 53) - 1)]
    modified_at: Annotated[
        str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    ]
    content_sha256: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)]
    mime_type: Annotated[
        str, Field(pattern=r"^[A-Za-z0-9.+-]{1,64}/[A-Za-z0-9.+-]{1,64}$")
    ]


class ManifestPage(RequestModel):
    manifest_id: CanonicalUUID
    folder_id: CanonicalUUID
    sequence: Annotated[int, Field(strict=True, ge=1, le=MAX_SEQUENCE)]
    final: Annotated[bool, Field(strict=True)]
    entries: Annotated[list[ManifestEntry], Field(max_length=MAX_ENTRIES, repr=False)]


class UploadRequest(ResponseModel):
    external_id: str
    upload_id: CanonicalUUID


class SkippedEntry(ResponseModel):
    external_id: str
    reason: Literal["FILE_TOO_LARGE", "UNSUPPORTED_FILE_TYPE"]


class ManifestResponse(ResponseModel):
    requested_uploads: list[UploadRequest]
    acknowledged_deletions: list[str]
    skipped_entries: list[SkippedEntry]
    next_sequence: int


@dataclass(frozen=True, slots=True, repr=False)
class ManifestWrite:
    device_id: UUID
    credential_hash: bytes
    public_key: bytes
    signed_at: datetime
    nonce_hash: bytes
    body_hash: bytes
    page: ManifestPage
    accepted: tuple[ManifestEntry, ...]
    skipped: tuple[SkippedEntry, ...]


class ManifestStore(Protocol):
    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None: ...

    async def commit(self, data: ManifestWrite) -> ManifestResponse: ...


class DeviceManifestService:
    def __init__(self, store: ManifestStore, *, hash_key: bytes) -> None:
        if len(hash_key) < 32:
            raise ValueError("device manifest key must be at least 32 bytes")
        self._store = store
        self._key = hash_key

    async def accept(
        self,
        *,
        device_id: UUID,
        credential: str,
        raw_target: bytes,
        headers: DeviceProofHeaders,
        exact_body: bytes,
    ) -> ManifestResponse:
        if (
            device_id.int == 0
            or raw_target != f"/v1/devices/{device_id}/manifests".encode()
        ):
            raise ManifestRejected
        try:
            digest = hash_device_credential(credential, self._key)
        except DeviceRegistrationRejected:
            raise ManifestRejected from None
        authority = await self._store.lookup(device_id, digest)
        if authority is None:
            raise ManifestRejected
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
            raise ManifestRejected from None

        page, accepted, skipped = _validated_page(exact_body)
        return await self._store.commit(
            ManifestWrite(
                device_id,
                digest,
                authority.public_key,
                proof.signed_at,
                hash_device_nonce(proof.nonce),
                hashlib.sha256(exact_body).digest(),
                page,
                accepted,
                skipped,
            )
        )


def _unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name, value in items:
        if name in fields:
            raise ValueError("duplicate field")
        fields[name] = value
    return fields


def _path_key(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("noncanonical relative path")
    for part in value.split("/"):
        normalized = unicodedata.normalize("NFKC", part).casefold()
        if (
            not part
            or normalized in (".", "..")
            or any(unicodedata.category(ch).startswith("C") for ch in part)
            or any(ch in _RESERVED for ch in normalized)
            or normalized.startswith(".")
            or normalized.endswith((" ", "."))
            or normalized.partition(".")[0].rstrip() in _WINDOWS_DEVICES
            or len(part.encode("utf-8")) > 255
        ):
            raise ValueError("unsafe relative path")
    if len(value.encode("utf-8")) > 1024:
        raise ValueError("relative path too long")
    return unicodedata.normalize("NFKC", value).casefold()


def _validated_page(
    body: bytes,
) -> tuple[ManifestPage, tuple[ManifestEntry, ...], tuple[SkippedEntry, ...]]:
    try:
        page = ManifestPage.model_validate(
            json.loads(body.decode("utf-8"), object_pairs_hook=_unique_fields)
        )
        if not page.manifest_id.int or not page.folder_id.int:
            raise ValueError("zero identifier")
        ids: set[str] = set()
        paths: set[str] = set()
        accepted: list[ManifestEntry] = []
        skipped: list[SkippedEntry] = []
        for entry in page.entries:
            path = _path_key(entry.relative_path)
            if path in paths or entry.external_id in ids:
                raise ValueError("duplicate entry")
            paths.add(path)
            ids.add(entry.external_id)
            datetime.fromisoformat(entry.modified_at)
            digest = base64.b64decode(entry.content_sha256 + "=", b"-_", validate=True)
            if (
                base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
                != entry.content_sha256
            ):
                raise ValueError("noncanonical digest")
            extension = "." + path.rsplit("/", 1)[-1].rpartition(".")[2]
            if entry.size_bytes > MAX_FILE_BYTES:
                skipped.append(
                    SkippedEntry(external_id=entry.external_id, reason="FILE_TOO_LARGE")
                )
            elif not path.rsplit("/", 1)[-1].rpartition(".")[
                1
            ] or extension not in _EXTENSIONS.get(entry.mime_type, set()):
                skipped.append(
                    SkippedEntry(
                        external_id=entry.external_id, reason="UNSUPPORTED_FILE_TYPE"
                    )
                )
            else:
                accepted.append(entry)
        return page, tuple(accepted), tuple(skipped)
    except (ValidationError, ValueError, binascii.Error, RecursionError):
        raise ManifestInvalid from None
