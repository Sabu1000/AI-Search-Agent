from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_CURSOR_SIGNING_KEY = "local-cursor-signing-key-change-before-production"
_LOCAL_IDEMPOTENCY_HASH_KEY = "local-idempotency-hash-key-change-before-production"
_LOCAL_AUTH_SIGNING_KEY = "local-auth-signing-key-change-before-production"
_LOCAL_AUTH_HASH_KEY = "local-auth-hash-key-change-before-production"
_LOCAL_OAUTH_HASH_KEY = "local-oauth-hash-key-change-before-production"
_LOCAL_PROVIDER_ENCRYPTION_KEY = "local-provider-key-change-before-production"


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
    database_role: Literal["app_api"] = "app_api"
    redis_url: str = "redis://localhost:6379/0"
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "universal-ai-search-local"
    object_storage_access_key: str = "minio"
    object_storage_secret_key: SecretStr = SecretStr("minio-local-only")
    cursor_signing_key: SecretStr = SecretStr(_LOCAL_CURSOR_SIGNING_KEY)
    idempotency_hash_key: SecretStr = SecretStr(_LOCAL_IDEMPOTENCY_HASH_KEY)
    auth_signing_key: SecretStr = SecretStr(_LOCAL_AUTH_SIGNING_KEY)
    auth_hash_key: SecretStr = SecretStr(_LOCAL_AUTH_HASH_KEY)
    oauth_hash_key: SecretStr = SecretStr(_LOCAL_OAUTH_HASH_KEY)
    provider_encryption_key: SecretStr = SecretStr(_LOCAL_PROVIDER_ENCRYPTION_KEY)
    google_oauth_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = "http://localhost:8000/v1/connections/google/callback"
    web_origin: str = "http://localhost:3000"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "Universal AI Search <no-reply@localhost>"

    @model_validator(mode="after")
    def validate_api_platform_secrets(self) -> Self:
        cursor_key = self.cursor_signing_key.get_secret_value()
        idempotency_key = self.idempotency_hash_key.get_secret_value()
        auth_signing_key = self.auth_signing_key.get_secret_value()
        auth_hash_key = self.auth_hash_key.get_secret_value()
        oauth_hash_key = self.oauth_hash_key.get_secret_value()
        provider_key = self.provider_encryption_key.get_secret_value()
        secret_values = (
            cursor_key,
            idempotency_key,
            auth_signing_key,
            auth_hash_key,
            oauth_hash_key,
            provider_key,
        )
        if any(len(value.encode()) < 32 for value in secret_values):
            raise ValueError("API platform secrets must be at least 32 bytes")
        if len(set(secret_values)) != len(secret_values):
            raise ValueError("API platform secrets must be distinct")
        if self.environment in {"staging", "production"} and (
            cursor_key == _LOCAL_CURSOR_SIGNING_KEY
            or idempotency_key == _LOCAL_IDEMPOTENCY_HASH_KEY
            or auth_signing_key == _LOCAL_AUTH_SIGNING_KEY
            or auth_hash_key == _LOCAL_AUTH_HASH_KEY
            or oauth_hash_key == _LOCAL_OAUTH_HASH_KEY
            or provider_key == _LOCAL_PROVIDER_ENCRYPTION_KEY
        ):
            raise ValueError(
                "local-only API platform secrets cannot be used outside development"
            )
        if self.google_oauth_enabled and (
            not self.google_client_id.strip()
            or not self.google_client_secret.get_secret_value()
        ):
            raise ValueError("Google OAuth credentials are required when enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
