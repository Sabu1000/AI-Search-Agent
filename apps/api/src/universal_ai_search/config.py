from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_CURSOR_SIGNING_KEY = "local-cursor-signing-key-change-before-production"
_LOCAL_IDEMPOTENCY_HASH_KEY = "local-idempotency-hash-key-change-before-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="UAS_",
        extra="ignore",
    )

    environment: Literal["local", "test", "development", "staging", "production"] = (
        "local"
    )
    database_url: str = "postgresql+asyncpg://uas:uas@localhost:5432/uas"
    redis_url: str = "redis://localhost:6379/0"
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "universal-ai-search-local"
    object_storage_access_key: str = "minio"
    object_storage_secret_key: SecretStr = SecretStr("minio-local-only")
    cursor_signing_key: SecretStr = SecretStr(_LOCAL_CURSOR_SIGNING_KEY)
    idempotency_hash_key: SecretStr = SecretStr(_LOCAL_IDEMPOTENCY_HASH_KEY)

    @model_validator(mode="after")
    def validate_api_platform_secrets(self) -> Self:
        cursor_key = self.cursor_signing_key.get_secret_value()
        idempotency_key = self.idempotency_hash_key.get_secret_value()
        if len(cursor_key.encode()) < 32 or len(idempotency_key.encode()) < 32:
            raise ValueError("API platform secrets must be at least 32 bytes")
        if cursor_key == idempotency_key:
            raise ValueError("API platform secrets must be distinct")
        if self.environment in {"staging", "production"} and (
            cursor_key == _LOCAL_CURSOR_SIGNING_KEY
            or idempotency_key == _LOCAL_IDEMPOTENCY_HASH_KEY
        ):
            raise ValueError(
                "local-only API platform secrets cannot be used outside development"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
