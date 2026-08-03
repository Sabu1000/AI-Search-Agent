from __future__ import annotations

import psycopg
from test_migration_lifecycle import EXPECTED_TABLES

TENANT_TABLES = {
    "api_idempotency_keys",
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
    "job_attempts",
    "jobs",
    "message_claims",
    "messages",
    "oauth_transactions",
    "outbox_events",
    "provider_events",
    "search_requests",
    "source_collection_memberships",
    "source_collections",
    "source_people",
    "sources",
    "workspace_members",
    "workspace_usage",
}

EXPECTED_INDEXES = {
    "ix_api_idempotency_keys_workspace_expiry",
    "ix_chunk_embeddings_hnsw",
    "ix_chunks_search_vector",
    "ix_deletion_requests_incomplete_deadline",
    "ix_document_versions_workspace_source_state",
    "ix_jobs_runnable",
    "ix_outbox_events_unpublished",
    "ix_source_people_workspace_identifier",
    "ix_sources_title_trgm",
    "ix_sources_workspace_filters",
    "ix_sources_workspace_connection_state",
    "uq_connections_active_external_account",
    "uq_embedding_profiles_one_active",
}


def test_extensions_types_constraints_and_workload_indexes_exist(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    extensions = {
        row[0]
        for row in connection.execute(
            "SELECT extname FROM pg_extension "
            "WHERE extname IN ('citext', 'pg_trgm', 'vector')"
        ).fetchall()
    }
    assert extensions == {"citext", "pg_trgm", "vector"}

    column_type_rows = connection.execute(
        """
            SELECT table_name || '.' || column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'app'
              AND (table_name, column_name) IN (
                  ('users', 'email'),
                  ('chunks', 'search_vector'),
                  ('chunk_embeddings', 'embedding')
              )
            """
    ).fetchall()
    column_types: dict[str, str] = {
        str(row[0]): str(row[1]) for row in column_type_rows
    }
    assert column_types == {
        "chunk_embeddings.embedding": "USER-DEFINED",
        "chunks.search_vector": "tsvector",
        "users.email": "USER-DEFINED",
    }

    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'app'"
        ).fetchall()
    }
    assert indexes >= EXPECTED_INDEXES

    unnamed_checks = connection.execute(
        """
        SELECT count(*)
        FROM pg_constraint AS constraint_record
        JOIN pg_namespace AS namespace
          ON namespace.oid = constraint_record.connamespace
        WHERE namespace.nspname = 'app'
          AND constraint_record.contype = 'c'
          AND constraint_record.conname NOT LIKE 'ck_%'
        """
    ).fetchone()
    assert unnamed_checks == (0,)


def test_tables_are_migrator_owned_and_application_roles_cannot_bypass_rls(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    owners = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'app'"
        ).fetchall()
    }
    assert set(owners) == EXPECTED_TABLES
    assert set(owners.values()) == {"app_migrator"}

    role_attributes = {
        row[0]: (row[1], row[2], row[3])
        for row in connection.execute(
            """
            SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
            FROM pg_roles
            WHERE rolname IN ('app_api', 'app_worker', 'app_audit_reader')
            """
        ).fetchall()
    }
    assert role_attributes == {
        "app_api": (False, False, False),
        "app_audit_reader": (False, False, False),
        "app_worker": (False, False, False),
    }
    assert connection.execute(
        "SELECT pg_has_role(current_user, 'app_migrator', 'MEMBER')"
    ).fetchone() == (True,)
    assert connection.execute(
        "SELECT has_table_privilege('app_api', 'public.alembic_version', 'SELECT')"
    ).fetchone() == (True,)


def test_every_tenant_table_has_direct_workspace_and_forced_rls(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace_columns = {
        row[0]
        for row in connection.execute(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = 'app' AND column_name = 'workspace_id'
            """
        ).fetchall()
    }
    assert workspace_columns >= TENANT_TABLES

    protected = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_namespace.nspname = 'app' AND pg_class.relkind = 'r'
            """
        ).fetchall()
    }
    for table in TENANT_TABLES | {
        "users",
        "auth_identities",
        "one_time_tokens",
        "sessions",
        "workspaces",
        "audit_events",
    }:
        assert protected[table] == (True, True)
    assert protected["embedding_profiles"] == (False, False)

    api_policy_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_policies
            WHERE schemaname = 'app' AND 'app_api' = ANY (roles)
            """
        ).fetchall()
    }
    worker_policy_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_policies
            WHERE schemaname = 'app' AND 'app_worker' = ANY (roles)
            """
        ).fetchall()
    }
    application_policy_tables = TENANT_TABLES | {
        "audit_events",
        "auth_identities",
        "one_time_tokens",
        "sessions",
        "users",
        "workspaces",
    }
    assert api_policy_tables >= application_policy_tables
    assert worker_policy_tables >= application_policy_tables

    audit_reader_policy_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_policies
            WHERE schemaname = 'app' AND 'app_audit_reader' = ANY (roles)
            """
        ).fetchall()
    }
    assert audit_reader_policy_tables == {"audit_events"}


def test_source_collection_membership_has_same_connection_foreign_keys(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    columns = [
        row[0]
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'app'
              AND table_name = 'source_collection_memberships'
            ORDER BY ordinal_position
            """
        ).fetchall()
    ]
    assert columns == [
        "workspace_id",
        "connection_id",
        "source_id",
        "collection_id",
        "relationship",
    ]

    foreign_keys = connection.execute(
        """
        SELECT pg_get_constraintdef(pg_constraint.oid)
        FROM pg_constraint
        JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
        JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
        WHERE pg_namespace.nspname = 'app'
          AND pg_class.relname = 'source_collection_memberships'
          AND pg_constraint.contype = 'f'
        ORDER BY pg_constraint.conname
        """
    ).fetchall()
    definitions = "\n".join(str(row[0]) for row in foreign_keys)
    assert "(workspace_id, connection_id, collection_id)" in definitions
    assert "(workspace_id, connection_id, source_id)" in definitions
