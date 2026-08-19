"""Small migration CLI used by deployment and local tooling."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

EXPECTED_SCHEMA_REVISION = "0005_gmail_incremental_sync"


def build_alembic_config() -> Config:
    api_root = Path(os.getenv("UAS_API_ROOT", Path.cwd()))
    config = Config(api_root / "alembic.ini")
    migration_url = os.getenv("UAS_DATABASE_MIGRATION_URL")
    if not migration_url:
        raise RuntimeError("UAS_DATABASE_MIGRATION_URL is required")
    config.set_main_option("sqlalchemy.url", migration_url)
    return config


def run() -> None:
    command.upgrade(build_alembic_config(), "head")
