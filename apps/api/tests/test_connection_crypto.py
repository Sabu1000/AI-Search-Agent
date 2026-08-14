from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag

from universal_ai_search.connections.crypto import (
    LocalEnvelopeEncryption,
    envelope_context,
)


def test_envelope_round_trip_is_randomized_and_context_bound() -> None:
    encryption = LocalEnvelopeEncryption(b"a" * 32)
    context = envelope_context(
        provider="google", workspace_id="workspace", record_id="record", purpose="test"
    )
    first = encryption.encrypt(b"refresh-token", context=context)
    second = encryption.encrypt(b"refresh-token", context=context)

    assert first != second
    assert b"refresh-token" not in first.ciphertext
    assert encryption.decrypt(first, context=context) == b"refresh-token"
    with pytest.raises(InvalidTag):
        encryption.decrypt(first, context=context + b"wrong")


def test_envelope_rejects_bad_keys_versions_and_payloads() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        LocalEnvelopeEncryption(b"short")

    encryption = LocalEnvelopeEncryption(b"a" * 32)
    envelope = encryption.encrypt(b"secret", context=b"context")
    with pytest.raises(ValueError, match="key version"):
        encryption.decrypt(replace(envelope, key_version=2), context=b"context")
    with pytest.raises(ValueError, match="invalid encrypted"):
        encryption.decrypt(replace(envelope, ciphertext=b"bad"), context=b"context")
