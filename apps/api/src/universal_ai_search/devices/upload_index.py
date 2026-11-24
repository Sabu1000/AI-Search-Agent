"""Read only one immutable staged native object and verify actual bytes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from uas_connector_sdk import AccessMetadata, NormalizedDocument, Provider

from .upload_completion import _IMMUTABLE_VERSION, _OBJECT_KEY

MAX_NATIVE_TEXT_BYTES = 512 * 1024
_READ_CHUNK = 64 * 1024
_SUPPORTED = {
    "text/plain": "Local file.txt",
    "text/markdown": "Local file.md",
    "text/csv": "Local file.csv",
}
_EXTERNAL = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NON_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class UploadIndexError(Exception):
    """Stable failure only; deliberately excludes file or provider detail."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True, repr=False)
class UploadIndexTarget:
    upload_id: UUID
    device_id: UUID
    folder_id: UUID
    connection_id: UUID
    owner_id: UUID
    external_id: str
    object_key: str
    immutable_version: str
    size_bytes: int
    content_sha256: str
    mime_type: str
    manifest_sequence: int

    @property
    def source_external_id(self) -> str:
        return f"desktop:{self.folder_id}:{self.external_id}"


class S3PinnedGetClient(Protocol):
    def get_object(self, **kwargs: str) -> Mapping[str, object]: ...


@runtime_checkable
class _StreamingBody(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class S3PinnedNativeTextReader:
    """Requires a dedicated trusted config with fixed endpoint/credentials."""

    def __init__(self, client: S3PinnedGetClient, *, bucket: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("valid fixed native staging bucket required")
        self._client = client
        self._bucket = bucket

    def read(self, target: UploadIndexTarget) -> NormalizedDocument:
        if (
            target.mime_type not in _SUPPORTED
            or type(target.size_bytes) is not int
            or not 1 <= target.size_bytes <= MAX_NATIVE_TEXT_BYTES
        ):
            raise UploadIndexError("UPLOAD_INDEX_UNSUPPORTED")
        if (
            not _OBJECT_KEY.fullmatch(target.object_key)
            or not _IMMUTABLE_VERSION.fullmatch(target.immutable_version)
            or target.immutable_version == "null"
            or not _EXTERNAL.fullmatch(target.external_id)
            or not 1 <= target.manifest_sequence <= 9_007_199_254_740_990
        ):
            raise UploadIndexError("UPLOAD_INDEX_INVALID")
        try:
            expected = base64.b64decode(
                target.content_sha256 + "=", altchars=b"-_", validate=True
            )
            if (
                len(expected) != 32
                or base64.urlsafe_b64encode(expected).decode()[:-1]
                != target.content_sha256
            ):
                raise UploadIndexError("UPLOAD_INDEX_INVALID")
            entry = self._client.get_object(
                Bucket=self._bucket,
                Key=target.object_key,
                VersionId=target.immutable_version,
                ChecksumMode="ENABLED",
            )
            raw = self._read_body(entry, target, expected)
        except UploadIndexError:
            raise
        except (ValueError, RecursionError):
            raise UploadIndexError("UPLOAD_INDEX_INVALID") from None
        except Exception:
            raise UploadIndexError("UPLOAD_INDEX_UNAVAILABLE", retryable=True) from None
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            raise UploadIndexError("UPLOAD_INDEX_UNSUPPORTED") from None
        if not text.strip() or _NON_TEXT.search(text):
            raise UploadIndexError("UPLOAD_INDEX_UNSUPPORTED")
        return NormalizedDocument(
            external_id=target.source_external_id,
            provider=Provider.LOCAL_FILES,
            source_type="file",
            title=_SUPPORTED[target.mime_type],
            content=text,
            mime_type=target.mime_type,
            access_metadata=AccessMetadata(user_ids=(str(target.owner_id),)),
            provider_metadata={
                "local_upload_id": str(target.upload_id),
                "local_device_id": str(target.device_id),
                "local_folder_id": str(target.folder_id),
                "local_manifest_sequence": target.manifest_sequence,
            },
        )

    @staticmethod
    def _read_body(
        response: Mapping[str, object],
        target: UploadIndexTarget,
        digest: bytes,
    ) -> bytes:
        body = response.get("Body")
        if not isinstance(body, _StreamingBody):
            raise UploadIndexError("UPLOAD_INDEX_INVALID")
        try:
            length = response.get("ContentLength")
            if (
                type(length) is not int
                or length != target.size_bytes
                or response.get("VersionId") != target.immutable_version
                or response.get("ContentType") != target.mime_type
                or response.get("ChecksumSHA256") != base64.b64encode(digest).decode()
                or response.get("ChecksumType") not in {None, "FULL_OBJECT"}
                or response.get("DeleteMarker") not in {None, False}
                or response.get("ContentEncoding") not in {None, "identity"}
                or response.get("ContentRange") is not None
            ):
                raise UploadIndexError("UPLOAD_INDEX_INVALID")
            chunks: list[bytes] = []
            accounted = 0
            computed = hashlib.sha256()
            while accounted <= length:
                part = body.read(min(_READ_CHUNK, length - accounted + 1))
                if not isinstance(part, bytes):
                    raise UploadIndexError("UPLOAD_INDEX_INVALID")
                if not part:
                    break
                accounted += len(part)
                if accounted > length or len(part) > _READ_CHUNK:
                    raise UploadIndexError("UPLOAD_INDEX_INVALID")
                computed.update(part)
                chunks.append(part)
            if accounted != length or not hmac.compare_digest(
                computed.digest(), digest
            ):
                raise UploadIndexError("UPLOAD_INDEX_INVALID")
            return b"".join(chunks)
        finally:
            body.close()
