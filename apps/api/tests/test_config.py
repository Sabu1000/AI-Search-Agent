import pytest
from pydantic import ValidationError

from universal_ai_search.config import Settings


def test_api_platform_secrets_must_be_long_and_distinct() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(
            _env_file=None,
            cursor_signing_key="short",
            idempotency_hash_key="another-short-key",
        )

    shared_key = "a-secure-but-shared-platform-secret"
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            _env_file=None,
            cursor_signing_key=shared_key,
            idempotency_hash_key=shared_key,
        )


def test_local_api_platform_secrets_are_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="local-only"):
        Settings(_env_file=None, environment="production")


def test_production_accepts_independent_api_platform_secrets() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        cursor_signing_key="production-cursor-signing-key-one",
        idempotency_hash_key="production-idempotency-hash-key-two",
    )

    assert settings.environment == "production"
