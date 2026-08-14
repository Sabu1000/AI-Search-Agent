from __future__ import annotations

from uuid import UUID

import psycopg
import pytest
from conftest import database_dsn
from uas_connector_sdk import RawItem
from uas_connector_sdk.testing import FakeConnector

from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import EnqueueStatus, IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime

WORKSPACE_ID = UUID("71000000-0000-4000-8000-000000000001")
USER_ID = UUID("72000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("73000000-0000-4000-8000-000000000001")


def seed_workspace(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    connection.execute("DELETE FROM app.workspaces")
    connection.execute("DELETE FROM app.users")
    connection.execute(
        "INSERT INTO app.users (id, email, full_name, status) "
        "VALUES (%s, 'indexer@example.test', 'Indexer', 'active')",
        (USER_ID,),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, 'Index Test', 'free', 'active')",
        (WORKSPACE_ID,),
    )
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id, user_id, role, status) "
        "VALUES (%s, %s, 'owner', 'active')",
        (WORKSPACE_ID, USER_ID),
    )
    connection.execute(
        """INSERT INTO app.connections (
            id, workspace_id, owner_user_id, provider, display_label, status
        ) VALUES (%s, %s, %s, 'github', 'Fake GitHub', 'active')""",
        (CONNECTION_ID, WORKSPACE_ID, USER_ID),
    )
    connection.execute(
        "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)",
        (WORKSPACE_ID,),
    )
    connection.commit()


@pytest.mark.asyncio
async def test_fake_connector_document_is_atomically_indexed(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    document = await FakeConnector().normalize(
        RawItem(
            external_id="repo:1:README.md",
            payload={
                "title": "README",
                "content": "# Search\n\nIndex this connector document.",
            },
        )
    )
    repository = IndexRepository(database_dsn())

    enqueued = repository.enqueue(WORKSPACE_ID, CONNECTION_ID, document)
    assert enqueued.status is EnqueueStatus.QUEUED
    assert IndexingRuntime(repository, IndexingPipeline()).run_once("test-worker")

    version = connection.execute(
        "SELECT state, language, token_count, normalized_text "
        "FROM app.document_versions WHERE id = %s",
        (enqueued.document_version_id,),
    ).fetchone()
    assert version is not None
    assert version[0:3] == ("ready", "en", 7)
    assert version[3] == "# Search\n\nIndex this connector document."
    assert connection.execute(
        "SELECT count(*) FROM app.chunks WHERE document_version_id = %s",
        (enqueued.document_version_id,),
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT count(*) FROM app.chunk_embeddings AS embedding "
        "JOIN app.chunks AS chunk ON chunk.id = embedding.chunk_id "
        "WHERE chunk.document_version_id = %s",
        (enqueued.document_version_id,),
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT status, attempt_count FROM app.jobs WHERE id = %s",
        (enqueued.job_id,),
    ).fetchone() == ("completed", 1)
    assert connection.execute(
        "SELECT indexed_source_count, extracted_bytes FROM app.workspace_usage "
        "WHERE workspace_id = %s",
        (WORKSPACE_ID,),
    ).fetchone() == (1, len(version[3].encode()))

    repeated = repository.enqueue(WORKSPACE_ID, CONNECTION_ID, document)
    assert repeated.status is EnqueueStatus.UNCHANGED
    assert repeated.document_version_id == enqueued.document_version_id
    assert repeated.job_id is None

    changed = document.model_copy(
        update={"content": "# Search\n\nThis content was re-indexed safely."}
    )
    reindex = repository.enqueue(WORKSPACE_ID, CONNECTION_ID, changed)
    assert reindex.status is EnqueueStatus.QUEUED
    assert IndexingRuntime(repository, IndexingPipeline()).run_once("test-worker")
    assert connection.execute(
        "SELECT state FROM app.document_versions WHERE id = %s",
        (enqueued.document_version_id,),
    ).fetchone() == ("superseded",)
    assert connection.execute(
        "SELECT current_document_version_id FROM app.sources WHERE id = %s",
        (enqueued.source_id,),
    ).fetchone() == (reindex.document_version_id,)
    assert connection.execute(
        "SELECT indexed_source_count FROM app.workspace_usage WHERE workspace_id = %s",
        (WORKSPACE_ID,),
    ).fetchone() == (1,)


def test_claim_function_chooses_authoritative_workspace(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    source_id = UUID("74000000-0000-4000-8000-000000000001")
    version_id = UUID("75000000-0000-4000-8000-000000000001")
    connection.execute(
        """INSERT INTO app.sources (
            id, workspace_id, connection_id, provider, external_id, source_type,
            title, content_hash, permissions_hash, state
        ) VALUES (%s, %s, %s, 'github', 'one', 'file', 'One', %s, %s, 'active')""",
        (source_id, WORKSPACE_ID, CONNECTION_ID, b"content", b"permissions"),
    )
    connection.execute(
        """INSERT INTO app.document_versions (
            id, workspace_id, source_id, version_key, state, normalized_text,
            language, parser_version, chunker_version, content_hash,
            permissions_hash, token_count, extracted_bytes
        ) VALUES (%s, %s, %s, %s, 'pending', 'one', 'und', 'p', 'c', %s, %s, 0, 0)""",
        (
            version_id,
            WORKSPACE_ID,
            source_id,
            b"version",
            b"content",
            b"permissions",
        ),
    )
    connection.execute(
        """INSERT INTO app.jobs (
            id, workspace_id, source_id, job_type, queue, idempotency_key,
            status, payload
        ) VALUES (%s, %s, %s, 'index', 'index', 'authority', 'pending', %s::JSONB)""",
        (
            UUID("76000000-0000-4000-8000-000000000001"),
            WORKSPACE_ID,
            source_id,
            '{"document_version_id": "75000000-0000-4000-8000-000000000001"}',
        ),
    )
    connection.commit()
    connection.execute("SET ROLE app_worker")
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true)",
        ("99999999-0000-4000-8000-000000000999",),
    )

    row = connection.execute(
        "SELECT workspace_id FROM app.claim_index_job('authority-worker', 120)"
    ).fetchone()

    assert row == (WORKSPACE_ID,)
