"""Verify the public Rust test vector with the Python API's crypto library."""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def verify() -> None:
    vector = json.loads(Path(__file__).with_name("ed25519-v1.json").read_text())
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(vector["seed_hex"]))
    public_key = b64url(key.public_key().public_bytes_raw())
    assert public_key == vector["public_key"]
    registration = vector["registration"]
    request = vector["request"]
    registered_message = "\n".join(
        (
            "uas-device-registration-v1",
            registration["challenge_id"],
            b64url(bytes.fromhex(registration["challenge_hex"])),
            public_key,
        )
    )
    assert registered_message == registration["message"]
    assert b64url(key.sign(registered_message.encode("ascii"))) == registration["signature"]

    body = request["body"].encode("utf-8")
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    nonce = b64url(bytes.fromhex(request["nonce_hex"]))
    assert nonce == request["nonce"]
    assert request["content_digest"] == f"sha-256=:{digest}:"
    assert request["path"] == f'/v1/devices/{request["device_id"]}/manifests'
    signed_message = "\n".join(
        (
            "uas-device-request-v1",
            "POST",
            request["path"],
            request["timestamp"],
            nonce,
            request["content_digest"],
            request["request_id"],
        )
    )
    assert signed_message == request["message"]
    assert b64url(key.sign(signed_message.encode("ascii"))) == request["signature"]


if __name__ == "__main__":
    verify()
    print("Synthetic Ed25519 registration and exact-body request vectors verified.")
