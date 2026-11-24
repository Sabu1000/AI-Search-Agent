"""Verify the versioned desktop Ed25519 wire contract.

These helpers prove possession only. They do not authenticate the opaque
credential, look up any user/workspace or device, or write replay state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_SIGNED_BODY_BYTES = 2_000_000
DEVICE_TIMESTAMP_TOLERANCE = timedelta(minutes=5)
_REGISTRATION_DOMAIN = "uas-device-registration-v1"
_REQUEST_DOMAIN = "uas-device-request-v1"
_DEVICE_TARGET = re.compile(
    rb"/v1/devices/([0-9a-f-]{36})/(?:heartbeat|manifests|uploads:sign|changes)"
)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FIELD_PRIME = 2**255 - 19
_FIELD_Y_BITS = 2**255 - 1
_GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

# Match the desktop verifier's rejection of low-order keys and signature R.
# The eight Ed25519 torsion points have these five canonical Y coordinates
# (the sign of X is masked below). See libsodium ref10's small-order check:
# https://github.com/jedisct1/libsodium/blob/1.0.18/src/libsodium/crypto_core/ed25519/ref10/ed25519_ref10.c
_SMALL_ORDER_Y = frozenset(
    {
        0,
        1,
        _FIELD_PRIME - 1,
        int.from_bytes(
            bytes.fromhex(
                "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"
            ),
            "little",
        ),
        int.from_bytes(
            bytes.fromhex(
                "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"
            ),
            "little",
        ),
    }
)


class DeviceProofFailure(StrEnum):
    INVALID_MESSAGE = "invalid_message"
    INVALID_SIGNATURE = "invalid_signature"
    TIMESTAMP_OUT_OF_WINDOW = "timestamp_out_of_window"


class DeviceProofError(ValueError):
    """Fixed codes only; never carries proofs, raw headers or body material."""

    def __init__(self, failure: DeviceProofFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


@dataclass(frozen=True, slots=True, repr=False)
class DeviceProofHeaders:
    timestamp: str
    nonce: str
    content_digest: str
    signature: str
    request_id: str


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedDeviceRequest:
    device_id: UUID
    request_id: UUID
    nonce: bytes
    signed_at: datetime


def verify_registration(
    *,
    challenge_id: UUID,
    challenge: bytes,
    public_key: str,
    signature: str,
) -> bytes:
    """Return the raw public key if the registration possession proof verifies.

    The caller must use a server-generated challenge bound to the initiating
    session/user and workspace, enforce its TTL and current authorization, and
    consume it exactly once in the same transaction that registers the device.
    The supplied key is not trusted device identity before those steps.
    """
    if not isinstance(challenge_id, UUID) or challenge_id.int == 0:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    if not isinstance(challenge, bytes) or len(challenge) != 32:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    decoded_key = _decode_urlsafe(public_key, low=32, high=32)
    proof = _decode_urlsafe(signature, low=64, high=64)
    transcript = _ascii_lines(
        _REGISTRATION_DOMAIN, str(challenge_id), _encode_urlsafe(challenge), public_key
    )
    _verify_signature(decoded_key, proof, transcript)
    return decoded_key


def verify_request(
    *,
    expected_device_id: UUID,
    registered_public_key: bytes,
    method: str,
    raw_request_target: bytes,
    headers: DeviceProofHeaders,
    exact_body: bytes,
    now: datetime,
) -> VerifiedDeviceRequest:
    """Verify one exact body, target and bounded header set without side effects.

    The caller must enforce TLS, unambiguous original request URI (no proxy or
    percent normalization), single raw headers, allowed media and no content
    decoding. Stream/enforce the size limit before buffering. Resolve an active,
    unexpired opaque device credential to the expected device UUID and its
    stored public key; never take those values from the URL, headers or body
    alone. Pass a trusted, timezone-aware server clock. After verification,
    atomically claim the returned raw nonce for that device, retaining it
    through the signed timestamp's five-minute window plus expiry margin.
    Recheck active device/workspace/folder authority in the atomic
    authorization/action boundary before processing the body or returning data.
    """
    if (
        method != "POST"
        or not isinstance(raw_request_target, bytes)
        or len(raw_request_target) > 80
        or not isinstance(exact_body, bytes)
        or len(exact_body) > MAX_SIGNED_BODY_BYTES
        or not isinstance(registered_public_key, bytes)
        or len(registered_public_key) != 32
        or now.utcoffset() is None
    ):
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    target = _DEVICE_TARGET.fullmatch(raw_request_target)
    if target is None:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    device_id = _canonical_uuid(target[1].decode("ascii"))
    if device_id != expected_device_id:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    request_id = _canonical_uuid(headers.request_id)
    timestamp = _canonical_timestamp(headers.timestamp)
    if abs(timestamp - now) > DEVICE_TIMESTAMP_TOLERANCE:
        raise DeviceProofError(DeviceProofFailure.TIMESTAMP_OUT_OF_WINDOW)
    nonce = _decode_urlsafe(headers.nonce, low=16, high=32)
    signature = _decode_urlsafe(headers.signature, low=64, high=64)
    digest_value = base64.b64encode(hashlib.sha256(exact_body).digest()).decode("ascii")
    content_digest = f"sha-256=:{digest_value}:"
    if (
        not isinstance(headers.content_digest, str)
        or len(headers.content_digest) != len(content_digest)
        or not headers.content_digest.isascii()
        or not hmac.compare_digest(content_digest, headers.content_digest)
    ):
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    transcript = _ascii_lines(
        _REQUEST_DOMAIN,
        method,
        raw_request_target.decode("ascii"),
        headers.timestamp,
        headers.nonce,
        content_digest,
        headers.request_id,
    )
    _verify_signature(registered_public_key, signature, transcript)
    return VerifiedDeviceRequest(device_id, request_id, nonce, timestamp)


def hash_device_nonce(verified_nonce: bytes) -> bytes:
    """Use one per-device replay namespace across every signed operation."""
    return hashlib.sha256(b"uas-device-nonce-v1\0" + verified_nonce).digest()


def _canonical_uuid(value: str) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    try:
        parsed = UUID(value)
    except ValueError:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE) from None
    if parsed.int == 0 or str(parsed) != value:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    return parsed


def _canonical_timestamp(value: str) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) != 20
        or _UTC_TIMESTAMP.fullmatch(value) is None
    ):
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE) from None


def _ascii_lines(*fields: str) -> bytes:
    return "\n".join(fields).encode("ascii")


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_urlsafe(value: str, *, low: int, high: int) -> bytes:
    if (
        not isinstance(value, str)
        or not (low * 8 + 5) // 6 <= len(value) <= (high * 8 + 5) // 6
    ):
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE) from None
    if not low <= len(decoded) <= high or _encode_urlsafe(decoded) != value:
        raise DeviceProofError(DeviceProofFailure.INVALID_MESSAGE)
    return decoded


def _canonical_non_torsion_point(encoded: bytes) -> bool:
    y = int.from_bytes(encoded, "little") & _FIELD_Y_BITS
    return y < _FIELD_PRIME and y not in _SMALL_ORDER_Y


def _verify_signature(public_key: bytes, signature: bytes, transcript: bytes) -> None:
    # These public encoding checks are not ECC verification. The audited
    # cryptography backend verifies the actual Ed25519 equation.
    if (
        not _canonical_non_torsion_point(public_key)
        or not _canonical_non_torsion_point(signature[:32])
        or int.from_bytes(signature[32:], "little") >= _GROUP_ORDER
    ):
        raise DeviceProofError(DeviceProofFailure.INVALID_SIGNATURE)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, transcript)
    except (InvalidSignature, ValueError):
        raise DeviceProofError(DeviceProofFailure.INVALID_SIGNATURE) from None
