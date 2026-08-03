from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config

from universal_ai_search.database.schema import run as run_schema_migration


def database_dsn() -> str:
    return os.environ["UAS_TEST_DATABASE_DSN"]


def migration_config(url: str | None = None) -> Config:
    api_root = Path(__file__).resolve().parents[1]
    config = Config(api_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        url or os.environ["UAS_DATABASE_MIGRATION_URL"],
    )
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    run_schema_migration()
    yield


@pytest.fixture()
def connection() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(database_dsn()) as database_connection:
        yield database_connection
