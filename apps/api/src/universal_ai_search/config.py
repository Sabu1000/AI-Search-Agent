from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
