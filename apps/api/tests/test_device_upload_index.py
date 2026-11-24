from __future__ import annotations

import base64
import hashlib
import io
from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

import pytest
from uas_connector_sdk import Provider

from universal_ai_search.devices.upload_index import (
    MAX_NATIVE_TEXT_BYTES,
    S3PinnedNativeTextReader,
    UploadIndexError,
    UploadIndexTarget,
)

BYTES = b"# Fixture\n\nSearch approved text."
VERSION = "only-verified-version"
BUCKET = "stage-fixture"


def _target(content: bytes = BYTES) -> UploadIndexTarget:
    return UploadIndexTarget(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "external-file",
        f"device-inbox/{uuid4()}",
        VERSION,
        len(content),
        base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode()[:-1],
        "text/markdown",
        1,
    )


class Client:
    def __init__(
        self,
        target: UploadIndexTarget,
        content: bytes = BYTES,
        **updates: object,
    ) -> None:
        self.body = io.BytesIO(content)
        self.response = {
            "ContentType": target.mime_type,
            "VersionId": target.immutable_version,
            "ContentLength": target.size_bytes,
            "ChecksumSHA256": base64.b64encode(
                base64.urlsafe_b64decode(target.content_sha256 + "=")
            ).decode(),
            "ChecksumType": "FULL_OBJECT",
            "Body": self.body,
        } | updates
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs: str) -> Mapping[str, object]:
        self.calls.append(kwargs)
        return self.response


def test_reader_uses_only_pinned_version_and_owner_private_projection() -> None:
    target = _target()
    client = Client(target)
    item = S3PinnedNativeTextReader(client, bucket=BUCKET).read(target)
    assert client.calls == [
        {
            "Bucket": BUCKET,
            "Key": target.object_key,
            "VersionId": VERSION,
            "ChecksumMode": "ENABLED",
        }
    ]
    assert client.body.closed
    assert item.provider is Provider.LOCAL_FILES
    assert item.content == BYTES.decode()
    assert item.title == "Local file.md"
    assert item.canonical_url is None and not item.access_metadata.public
    assert item.access_metadata.user_ids == (str(target.owner_id),)
    assert item.provider_metadata == {
        "local_upload_id": str(target.upload_id),
        "local_device_id": str(target.device_id),
        "local_folder_id": str(target.folder_id),
        "local_manifest_sequence": 1,
    }
    assert target.object_key not in str(item.provider_metadata)
    assert VERSION not in str(item.provider_metadata)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"size_bytes": MAX_NATIVE_TEXT_BYTES + 1}, "UPLOAD_INDEX_UNSUPPORTED"),
        ({"size_bytes": 0}, "UPLOAD_INDEX_UNSUPPORTED"),
        ({"mime_type": "application/octet-stream"}, "UPLOAD_INDEX_UNSUPPORTED"),
        ({"object_key": "http://private.test"}, "UPLOAD_INDEX_INVALID"),
        ({"immutable_version": "null"}, "UPLOAD_INDEX_INVALID"),
        ({"content_sha256": "invalid"}, "UPLOAD_INDEX_INVALID"),
        ({"manifest_sequence": 0}, "UPLOAD_INDEX_INVALID"),
    ],
)
def test_untrusted_or_unbounded_targets_never_contact_storage(
    fields: dict[str, object], code: str
) -> None:
    target = _target()
    client = Client(target)
    with pytest.raises(UploadIndexError, match=code):
        S3PinnedNativeTextReader(client, bucket=BUCKET).read(replace(target, **fields))
    assert not client.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"VersionId": "alternate"},
        {"ChecksumSHA256": None},
        {"ChecksumType": "COMPOSITE"},
        {"ContentType": "text/plain"},
        {"ContentLength": 1},
        {"DeleteMarker": True},
        {"ContentEncoding": "gzip"},
        {"ContentRange": "bytes 0-1/10"},
    ],
)
def test_conflicting_pinned_response_refused_and_closed(
    changes: dict[str, object],
) -> None:
    target = _target()
    client = Client(target, **changes)
    with pytest.raises(UploadIndexError, match="UPLOAD_INDEX_INVALID"):
        S3PinnedNativeTextReader(client, bucket=BUCKET).read(target)
    assert client.body.closed


@pytest.mark.parametrize("content", [BYTES[:-1], BYTES + b"x", b"x" * len(BYTES)])
def test_actual_length_and_hash_are_recomputed(content: bytes) -> None:
    target = _target()
    client = Client(target, content)
    with pytest.raises(UploadIndexError, match="UPLOAD_INDEX_INVALID"):
        S3PinnedNativeTextReader(client, bucket=BUCKET).read(target)
    assert client.body.closed


@pytest.mark.parametrize("content", [b" \n\t", b"binary\x00sample", b"\xff"])
def test_nontext_does_not_enter_normalized_index(content: bytes) -> None:
    target = _target(content)
    with pytest.raises(UploadIndexError, match="UPLOAD_INDEX_UNSUPPORTED"):
        S3PinnedNativeTextReader(Client(target, content), bucket=BUCKET).read(target)


def test_provider_detail_is_sanitized() -> None:
    class LeakingProvider:
        def get_object(self, **kwargs: str) -> Mapping[str, object]:
            raise RuntimeError("synthetic signed URL and private file contents")

    reader = S3PinnedNativeTextReader(LeakingProvider(), bucket=BUCKET)
    with pytest.raises(UploadIndexError, match="^UPLOAD_INDEX_UNAVAILABLE$") as error:
        reader.read(_target())
    assert error.value.retryable
    with pytest.raises(ValueError, match="valid fixed"):
        S3PinnedNativeTextReader(LeakingProvider(), bucket="http://bad.invalid")
