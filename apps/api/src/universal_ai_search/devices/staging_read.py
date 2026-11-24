"""Version-pinned SHA-256 HEAD reader; no writes or client-controlled addresses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

import anyio

from .upload_completion import (
    _IMMUTABLE_VERSION,
    _OBJECT_KEY,
    ChangeInvalidObject,
    ImmutableObject,
)
from .uploads import UploadUnavailable


class S3HeadClient(Protocol):
    def head_object(self, **kwargs: str) -> Mapping[str, object]: ...


def _read_metadata(record: Mapping[str, object]) -> ImmutableObject:
    version, length, digest, mime, checksum_type = (
        record.get("VersionId"),
        record.get("ContentLength"),
        record.get("ChecksumSHA256"),
        record.get("ContentType"),
        record.get("ChecksumType"),
    )
    if (
        not isinstance(version, str)
        or not _IMMUTABLE_VERSION.fullmatch(version)
        or version == "null"
        or type(length) is not int
        or length < 0
        or not isinstance(digest, str)
        or len(digest) != 44
        or not digest.isascii()
        or not isinstance(mime, str)
        or mime not in ("text/plain", "text/markdown", "text/csv")
        or (checksum_type is not None and checksum_type != "FULL_OBJECT")
        or (
            record.get("DeleteMarker") is not None
            and record.get("DeleteMarker") is not False
        )
    ):
        raise ChangeInvalidObject
    return ImmutableObject(version, length, digest, mime)


class S3VersionedStageReader:
    """Accept only a separately verified, versioned write-once staging bucket.

    The supplied client is a dedicated, read-only HEAD client with fixed endpoint,
    certificate verification, bounded timeouts/retries and disabled redirects at
    its trusted deployment boundary. Missing full-object SHA-256 never falls
    back to ETag; missing versions never fall back to latest/unversioned objects.
    """

    def __init__(self, client: S3HeadClient, *, bucket: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("verified staging bucket name required")
        self._client = client
        self._bucket = bucket
        self._workers = anyio.CapacityLimiter(4)

    def _head(self, object_key: str) -> ImmutableObject:
        common = {"Bucket": self._bucket, "Key": object_key, "ChecksumMode": "ENABLED"}
        try:
            current = _read_metadata(self._client.head_object(**common))
            pinned = _read_metadata(
                self._client.head_object(**common, VersionId=current.version_id)
            )
        except ChangeInvalidObject:
            raise
        except Exception:
            # Never surface provider URLs, signing material, or response bodies.
            raise UploadUnavailable from None
        if current != pinned:
            raise ChangeInvalidObject
        return pinned

    async def get(self, object_key: str) -> ImmutableObject:
        if not _OBJECT_KEY.fullmatch(object_key):
            raise ChangeInvalidObject
        return await anyio.to_thread.run_sync(
            self._head,
            object_key,
            limiter=self._workers,
            abandon_on_cancel=False,
        )
