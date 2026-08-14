"""Small envelope-encryption boundary for provider credential records."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


@dataclass(frozen=True)
class EncryptedEnvelope:
    ciphertext: bytes
    encrypted_data_key: bytes
    key_version: int = 1


def envelope_context(
    *, provider: str, workspace_id: str, record_id: str, purpose: str
) -> bytes:
    return json.dumps(
        {
            "provider": provider,
            "purpose": purpose,
            "record_id": record_id,
            "schema_version": 1,
            "workspace_id": workspace_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class LocalEnvelopeEncryption:
    """Development KMS adapter using AES-GCM for both data and key wrapping."""

    _VERSION = b"\x01"
    _NONCE_BYTES = 12

    def __init__(self, master_secret: bytes) -> None:
        if len(master_secret) < 32:
            raise ValueError("provider encryption secret must be at least 32 bytes")
        self._wrapping_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"universal-ai-search/provider-envelope/v1",
        ).derive(master_secret)

    def encrypt(self, plaintext: bytes, *, context: bytes) -> EncryptedEnvelope:
        data_key = AESGCM.generate_key(bit_length=256)
        data_nonce = secrets.token_bytes(self._NONCE_BYTES)
        wrap_nonce = secrets.token_bytes(self._NONCE_BYTES)
        ciphertext = AESGCM(data_key).encrypt(data_nonce, plaintext, context)
        wrapped_key = AESGCM(self._wrapping_key).encrypt(wrap_nonce, data_key, context)
        return EncryptedEnvelope(
            ciphertext=self._VERSION + data_nonce + ciphertext,
            encrypted_data_key=self._VERSION + wrap_nonce + wrapped_key,
        )

    def decrypt(self, envelope: EncryptedEnvelope, *, context: bytes) -> bytes:
        if envelope.key_version != 1:
            raise ValueError("unsupported credential key version")
        ciphertext = self._payload(envelope.ciphertext)
        wrapped_key = self._payload(envelope.encrypted_data_key)
        data_key = AESGCM(self._wrapping_key).decrypt(
            wrapped_key[: self._NONCE_BYTES],
            wrapped_key[self._NONCE_BYTES :],
            context,
        )
        return AESGCM(data_key).decrypt(
            ciphertext[: self._NONCE_BYTES],
            ciphertext[self._NONCE_BYTES :],
            context,
        )

    @classmethod
    def _payload(cls, value: bytes) -> bytes:
        if not value.startswith(cls._VERSION) or len(value) <= cls._NONCE_BYTES + 1:
            raise ValueError("invalid encrypted envelope")
        return value[1:]
