from __future__ import annotations

import os

import psycopg
from alembic import command
from conftest import migration_config

EXPECTED_TABLES = {
    "api_idempotency_keys",
    "audit_events",
    "auth_identities",
    "chunk_embeddings",
    "chunks",
    "citations",
    "connection_cursors",
    "connection_scopes",
    "connections",
    "conversations",
    "deletion_requests",
    "device_folders",
    "devices",
    "document_versions",
    "embedding_profiles",
    "job_attempts",
    "jobs",
    "message_claims",
    "messages",
    "oauth_transactions",
    "one_time_tokens",
    "outbox_events",
    "provider_events",
    "search_requests",
    "sessions",
    "source_collection_memberships",
    "source_collections",
    "source_people",
    "sources",
    "users",
    "workspace_members",
    "workspace_usage",
    "workspaces",
}


def test_empty_database_upgrade_creates_exact_catalog(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'app'"
        ).fetchall()
    }
    assert tables == EXPECTED_TABLES
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0007_drive_sync_runtime",
    )


def test_upgrade_downgrade_and_reupgrade_on_isolated_database() -> None:
    lifecycle_database = "uas_migration_lifecycle"
    admin_dsn = os.environ["UAS_TEST_ADMIN_DSN"]
    base_url = os.environ["UAS_DATABASE_MIGRATION_URL"].rsplit("/", 1)[0]

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{lifecycle_database}"')
        admin.execute(f'CREATE DATABASE "{lifecycle_database}"')

    lifecycle_dsn = f"{admin_dsn.rsplit('/', 1)[0]}/{lifecycle_database}"
    lifecycle_url = f"{base_url}/{lifecycle_database}"
    try:
        with psycopg.connect(lifecycle_dsn, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION citext")
            connection.execute("CREATE EXTENSION pg_trgm")
            connection.execute("CREATE EXTENSION vector")

        config = migration_config(lifecycle_url)
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        with psycopg.connect(lifecycle_dsn) as connection:
            assert connection.execute("SELECT to_regnamespace('app')").fetchone() == (
                None,
            )
        command.upgrade(config, "head")
        with psycopg.connect(lifecycle_dsn) as connection:
            count = connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'app'"
            ).fetchone()
            assert count == (33,)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (lifecycle_database,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{lifecycle_database}"')
