from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from universal_ai_search.devices.security import (
    DEVICE_TIMESTAMP_TOLERANCE,
    MAX_SIGNED_BODY_BYTES,
    DeviceProofError,
    DeviceProofFailure,
    DeviceProofHeaders,
    verify_registration,
    verify_request,
)

_FIXTURE = Path(__file__).with_name("fixtures") / "device_ed25519_v1.json"
_VECTOR = json.loads(_FIXTURE.read_bytes())
_REQUEST = _VECTOR["request"]
_REGISTRATION = _VECTOR["registration"]
_PUBLIC_KEY = base64.urlsafe_b64decode(_VECTOR["public_key"] + "=")
_CHALLENGE_ID = UUID(_REGISTRATION["challenge_id"])
_CHALLENGE = bytes.fromhex(_REGISTRATION["challenge_hex"])
_DEVICE_ID = UUID(_REQUEST["device_id"])
_REQUEST_ID = UUID(_REQUEST["request_id"])
_TARGET = _REQUEST["path"].encode("ascii")
_BODY = _REQUEST["body"].encode("utf-8")
_SIGNED_AT = datetime.fromisoformat(_REQUEST["timestamp"])
_HEADERS = DeviceProofHeaders(
    timestamp=_REQUEST["timestamp"],
    nonce=_REQUEST["nonce"],
    content_digest=_REQUEST["content_digest"],
    signature=_REQUEST["signature"],
    request_id=_REQUEST["request_id"],
)


def _verify(
    *,
    method: str = "POST",
    target: bytes = _TARGET,
    headers: DeviceProofHeaders = _HEADERS,
    expected_device: UUID = _DEVICE_ID,
    registered_key: bytes = _PUBLIC_KEY,
    body: bytes = _BODY,
    now: datetime = _SIGNED_AT,
) -> None:
    verified = verify_request(
        expected_device_id=expected_device,
        registered_public_key=registered_key,
        method=method,
        raw_request_target=target,
        headers=headers,
        exact_body=body,
        now=now,
    )
    assert verified.device_id == expected_device
    assert verified.request_id == _REQUEST_ID
    assert verified.nonce == bytes.fromhex(_REQUEST["nonce_hex"])
    assert verified.signed_at == _SIGNED_AT


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def test_copied_fixture_remains_byte_identical_to_rust_source() -> None:
    # The backend test container contains only the API fixture, so no
    # cross-tree dependency is required to run it. Detect copying drift when
    # both fixture sources are present in the repository checkout.
    desktop = (
        _FIXTURE.resolve().parents[3]
        / "desktop/src-tauri/scanner/fixtures/ed25519-v1.json"
    )
    if desktop.exists():
        assert _FIXTURE.read_bytes() == desktop.read_bytes()


def test_interoperable_registration_and_exact_body_request() -> None:
    assert (
        verify_registration(
            challenge_id=_CHALLENGE_ID,
            challenge=_CHALLENGE,
            public_key=_VECTOR["public_key"],
            signature=_REGISTRATION["signature"],
        )
        == _PUBLIC_KEY
    )
    _verify()
    # Defense-in-depth: a proof object must not print a signature or nonce.
    assert _HEADERS.signature not in repr(_HEADERS)
    assert _HEADERS.nonce not in repr(_HEADERS)


@pytest.mark.parametrize(
    ("challenge_id", "challenge", "public_key", "signature", "expected"),
    [
        (
            _CHALLENGE_ID,
            bytes(32),
            _VECTOR["public_key"],
            _REGISTRATION["signature"],
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            UUID(int=0),
            _CHALLENGE,
            _VECTOR["public_key"],
            _REGISTRATION["signature"],
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            _CHALLENGE_ID,
            b"too-short",
            _VECTOR["public_key"],
            _REGISTRATION["signature"],
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            _CHALLENGE_ID,
            _CHALLENGE,
            _VECTOR["public_key"] + "=",
            _REGISTRATION["signature"],
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            _CHALLENGE_ID,
            _CHALLENGE,
            _VECTOR["public_key"],
            _REQUEST["signature"],
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            _CHALLENGE_ID,
            _CHALLENGE,
            _b64url(bytes(32)),
            _REGISTRATION["signature"],
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
    ],
)
def test_registration_rejects_mismatched_or_invalid_fields(
    challenge_id: UUID,
    challenge: bytes,
    public_key: str,
    signature: str,
    expected: DeviceProofFailure,
) -> None:
    with pytest.raises(DeviceProofError) as error:
        verify_registration(
            challenge_id=challenge_id,
            challenge=challenge,
            public_key=public_key,
            signature=signature,
        )
    assert error.value.failure == expected
    assert str(error.value) == expected.value


@pytest.mark.parametrize(
    "target",
    [
        _TARGET + b"?",
        _TARGET + b"?ignored=1",
        _TARGET + b"/",
        _TARGET.replace(b"/manifests", b"/../manifests"),
        _TARGET.replace(b"/manifests", b"/%6danifests"),
        _TARGET.replace(b"/manifests", b"/manifest\xff"),
        _TARGET.replace(b"/manifests", b"/foo"),
        b"/v1/devices/" + str(_DEVICE_ID).upper().encode("ascii") + b"/manifests",
        b"/v1/devices/00000000-0000-0000-0000-000000000000/manifests",
        b"/v1/devices/------------------------------------/manifests",
        b"/" * 81,
    ],
)
def test_requires_original_canonical_request_target(target: bytes) -> None:
    with pytest.raises(DeviceProofError) as error:
        _verify(target=target)
    assert error.value.failure == DeviceProofFailure.INVALID_MESSAGE


@pytest.mark.parametrize(
    ("headers", "failure"),
    [
        (
            replace(_HEADERS, request_id="018f4fa3-3d41-7486-a5dc-f49b976c2410"),
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            replace(_HEADERS, request_id="short"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, request_id="A" * 36),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, timestamp="2026-02-29T00:00:00Z"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, timestamp="2026-11-24T17:45:00+00:00"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, timestamp="2026-11-24T17:45:00z"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, timestamp="2026-11-24T17:45:01Z"),
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            replace(_HEADERS, nonce="?" * len(_HEADERS.nonce)),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, nonce=_HEADERS.nonce[:-1] + "x"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, nonce=_HEADERS.nonce + "="),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, signature=_REGISTRATION["signature"]),
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            replace(_HEADERS, signature=_b64url(bytes(64))),
            DeviceProofFailure.INVALID_SIGNATURE,
        ),
        (
            replace(_HEADERS, content_digest="sha256 invalid"),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
        (
            replace(_HEADERS, content_digest="é" + _HEADERS.content_digest[1:]),
            DeviceProofFailure.INVALID_MESSAGE,
        ),
    ],
)
def test_bounds_and_binds_raw_proof_headers(
    headers: DeviceProofHeaders, failure: DeviceProofFailure
) -> None:
    with pytest.raises(DeviceProofError) as error:
        _verify(headers=headers)
    assert error.value.failure == failure


def test_rejects_mismatched_device_key_method_body_and_route() -> None:
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(expected_device=UUID(int=12))
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(method="GET")
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(body=_BODY + b" ")
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(body=bytes(MAX_SIGNED_BODY_BYTES + 1))
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(registered_key=b"short")
    with pytest.raises(DeviceProofError, match=r"^invalid_signature$"):
        _verify(registered_key=bytes(32))
    with pytest.raises(DeviceProofError, match=r"^invalid_signature$"):
        _verify(target=_TARGET.replace(b"/manifests", b"/changes"))


def test_freshness_uses_trusted_aware_clock_with_exact_boundary() -> None:
    gap = DEVICE_TIMESTAMP_TOLERANCE
    _verify(now=_SIGNED_AT + gap)
    _verify(now=_SIGNED_AT - gap)
    with pytest.raises(DeviceProofError, match=r"^timestamp_out_of_window$"):
        _verify(now=_SIGNED_AT + gap + timedelta(microseconds=1))
    with pytest.raises(DeviceProofError, match=r"^timestamp_out_of_window$"):
        _verify(now=_SIGNED_AT - gap - timedelta(microseconds=1))
    with pytest.raises(DeviceProofError, match=r"^invalid_message$"):
        _verify(now=_SIGNED_AT.replace(tzinfo=None))
