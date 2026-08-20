from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from conftest import database_dsn
from uas_connector_sdk import DocumentPerson, NormalizedDocument, Provider

from universal_ai_search.connections.crypto import (
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.gmail import GmailHistoryPage, GmailPage
from universal_ai_search.connections.google import GMAIL_READONLY_SCOPE
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import EnqueueStatus, IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.sync.repository import GoogleSyncRepository
from universal_ai_search.sync.runtime import GmailSyncRuntime

WORKSPACE_ID = UUID("81000000-0000-4000-8000-000000000001")
USER_ID = UUID("82000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("83000000-0000-4000-8000-000000000001")
JOB_ID = UUID("84000000-0000-4000-8000-000000000001")


class FakeGmailClient:
    async def ensure_fresh(self, credentials: object) -> object:
        return credentials

    async def history_id(self, access_token: str) -> str:
        assert access_token == "synthetic-access"
        return "history-100"

    async def page(self, **values: object) -> GmailPage:
        assert values["access_token"] == "synthetic-access"
        page_token = values["page_token"]
        assert page_token in {None, "page-2"}
        suffix = "1" if page_token is None else "2"
        return GmailPage(
            (
                NormalizedDocument(
                    external_id=f"gmail-message-{suffix}",
                    provider=Provider.GMAIL,
                    source_type="email",
                    title=f"Gmail integration test {suffix}",
                    content=(
                        f"Subject: Gmail integration test {suffix}\n\n"
                        f"Searchable mailbox content {suffix}."
                    ),
                    canonical_url=(
                        "https://mail.google.com/mail/u/0/#all/"
                        f"gmail-message-{suffix}"
                    ),
                    mime_type="text/plain",
                    authors=("sender@example.test",),
                    created_at=datetime(2026, 8, 15, tzinfo=UTC),
                    people=(
                        DocumentPerson(
                            relationship="sender",
                            identity_kind="email",
                            normalized_identifier="sender@example.test",
                        ),
                        DocumentPerson(
                            relationship="recipient",
                            identity_kind="email",
                            normalized_identifier="recipient@example.test",
                        ),
                    ),
                    provider_metadata={"thread_id": "thread-1"},
                ),
            ),
            "page-2" if page_token is None else None,
        )

    async def history_page(self, **values: object) -> GmailHistoryPage:
        assert values["access_token"] == "synthetic-access"
        assert values["start_history_id"] == "history-100"
        assert values["page_token"] is None
        return GmailHistoryPage(
            (
                NormalizedDocument(
                    external_id="gmail-message-1",
                    provider=Provider.GMAIL,
                    source_type="email",
                    title="Gmail integration test updated",
                    content="Subject: Gmail integration test updated\n\nNew content.",
                    canonical_url=(
                        "https://mail.google.com/mail/u/0/#all/gmail-message-1"
                    ),
                    mime_type="text/plain",
                    authors=("sender@example.test",),
                    created_at=datetime(2026, 8, 15, tzinfo=UTC),
                    people=(
                        DocumentPerson(
                            relationship="sender",
                            identity_kind="email",
                            normalized_identifier="sender@example.test",
                        ),
                    ),
                    provider_metadata={"thread_id": "thread-1"},
                ),
            ),
            ("gmail-message-2",),
            "history-200",
            None,
        )


def test_gmail_full_sync_queues_indexes_and_commits_cursor(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    credential_context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(CONNECTION_ID),
        purpose="provider-credential",
    )
    credential_payload = json.dumps(
        {
            "access_token": "synthetic-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "refresh_token": "synthetic-refresh",
            "schema_version": 1,
            "scopes": [GMAIL_READONLY_SCOPE],
        }
    ).encode()
    envelope = encryption.encrypt(credential_payload, context=credential_context)

    connection.execute("DELETE FROM app.workspaces")
    connection.execute("DELETE FROM app.users")
    connection.execute(
        "INSERT INTO app.users (id, email, full_name, status) "
        "VALUES (%s, 'gmail-sync@example.test', 'Gmail Sync', 'active')",
        (USER_ID,),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, 'Gmail Sync', 'free', 'active')",
        (WORKSPACE_ID,),
    )
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id, user_id, role, status) "
        "VALUES (%s, %s, 'owner', 'active')",
        (WORKSPACE_ID, USER_ID),
    )
    connection.execute(
        """INSERT INTO app.connections (
            id, workspace_id, owner_user_id, provider, display_label, status,
            credential_ciphertext, encrypted_data_key, key_version
        ) VALUES (%s, %s, %s, 'google', 'gmail-sync@example.test', 'active',
            %s, %s, %s)""",
        (
            CONNECTION_ID,
            WORKSPACE_ID,
            USER_ID,
            envelope.ciphertext,
            envelope.encrypted_data_key,
            envelope.key_version,
        ),
    )
    connection.execute(
        "INSERT INTO app.connection_scopes (workspace_id, connection_id, scope) "
        "VALUES (%s, %s, %s)",
        (WORKSPACE_ID, CONNECTION_ID, GMAIL_READONLY_SCOPE),
    )
    connection.execute(
        "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)",
        (WORKSPACE_ID,),
    )
    connection.execute(
        """INSERT INTO app.jobs (
            id, workspace_id, connection_id, job_type, queue,
            idempotency_key, status, payload
        ) VALUES (%s, %s, %s, 'sync', 'sync', 'gmail-e2e', 'pending',
            '{"mode":"full","source_families":["gmail"]}'::JSONB)""",
        (JOB_ID, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.commit()

    index_repository = IndexRepository(database_dsn())
    sync_runtime = GmailSyncRuntime(
        repository=GoogleSyncRepository(database_dsn()),
        index_repository=index_repository,
        client=FakeGmailClient(),  # type: ignore[arg-type]
        encryption=encryption,
    )
    assert sync_runtime.run_once("gmail-database-test")

    assert connection.execute(
        "SELECT status, attempt_count FROM app.jobs WHERE id = %s", (JOB_ID,)
    ).fetchone() == ("completed", 1)
    assert connection.execute(
        "SELECT count(*) FROM app.connection_cursors WHERE connection_id = %s",
        (CONNECTION_ID,),
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM app.jobs WHERE connection_id = %s "
        "AND job_type = 'sync' AND status = 'pending'",
        (CONNECTION_ID,),
    ).fetchone() == (1,)
    continuation_payload = connection.execute(
        "SELECT payload::TEXT FROM app.jobs WHERE connection_id = %s "
        "AND job_type = 'sync' AND status = 'pending'",
        (CONNECTION_ID,),
    ).fetchone()
    assert continuation_payload is not None
    payload_text = continuation_payload[0]
    assert isinstance(payload_text, str)
    assert "gmail_progress" in payload_text
    assert "page-2" not in payload_text
    assert "history-100" not in payload_text

    assert sync_runtime.run_once("gmail-database-test")
    assert connection.execute(
        "SELECT cursor ->> 'history_id' FROM app.connection_cursors "
        "WHERE connection_id = %s AND stream = 'gmail'",
        (CONNECTION_ID,),
    ).fetchone() == ("history-100",)
    index_job_ids = connection.execute(
        "SELECT id FROM app.jobs WHERE connection_id = %s AND job_type = 'index'",
        (CONNECTION_ID,),
    ).fetchall()
    assert len(index_job_ids) == 2

    assert IndexingRuntime(index_repository, IndexingPipeline()).run_once(
        "gmail-index-test"
    )
    assert IndexingRuntime(index_repository, IndexingPipeline()).run_once(
        "gmail-index-test"
    )
    assert (
        connection.execute(
            """SELECT source.provider, source.source_type, version.state,
            version.normalized_text
        FROM app.sources AS source
        JOIN app.document_versions AS version
          ON version.id = source.current_document_version_id
        WHERE source.connection_id = %s ORDER BY source.external_id""",
            (CONNECTION_ID,),
        ).fetchall()
        == [
            (
                "gmail",
                "email",
                "ready",
                "Subject: Gmail integration test 1\n\nSearchable mailbox content 1.",
            ),
            (
                "gmail",
                "email",
                "ready",
                "Subject: Gmail integration test 2\n\nSearchable mailbox content 2.",
            ),
        ]
    )
    assert connection.execute(
        "SELECT relationship, normalized_identifier::TEXT "
        "FROM app.source_people WHERE workspace_id = %s "
        "ORDER BY relationship, normalized_identifier",
        (WORKSPACE_ID,),
    ).fetchall() == [
        ("recipient", "recipient@example.test"),
        ("recipient", "recipient@example.test"),
        ("sender", "sender@example.test"),
        ("sender", "sender@example.test"),
    ]

    metadata_only_update = NormalizedDocument(
        external_id="gmail-message-1",
        provider=Provider.GMAIL,
        source_type="email",
        title="Gmail integration test 1 metadata refreshed",
        content=("Subject: Gmail integration test 1\n\nSearchable mailbox content 1."),
        canonical_url=("https://mail.google.com/mail/u/0/#all/gmail-message-1"),
        mime_type="text/plain",
        authors=("sender@example.test",),
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
        people=(
            DocumentPerson(
                relationship="sender",
                identity_kind="email",
                normalized_identifier="sender@example.test",
            ),
            DocumentPerson(
                relationship="recipient",
                identity_kind="email",
                normalized_identifier="new-recipient@example.test",
            ),
        ),
        provider_metadata={"label_ids": ["STARRED"], "thread_id": "thread-1"},
    )
    refresh = index_repository.enqueue(
        WORKSPACE_ID, CONNECTION_ID, metadata_only_update
    )
    assert refresh.status is EnqueueStatus.UNCHANGED
    assert connection.execute(
        "SELECT title, metadata -> 'label_ids' FROM app.sources "
        "WHERE connection_id = %s AND external_id = 'gmail-message-1'",
        (CONNECTION_ID,),
    ).fetchone() == (
        "Gmail integration test 1 metadata refreshed",
        ["STARRED"],
    )
    assert connection.execute(
        "SELECT normalized_identifier::TEXT FROM app.source_people "
        "WHERE source_id = %s ORDER BY normalized_identifier",
        (refresh.source_id,),
    ).fetchall() == [
        ("new-recipient@example.test",),
        ("sender@example.test",),
    ]

    connection.execute(
        "UPDATE app.jobs SET available_at = clock_timestamp() "
        "WHERE connection_id = %s AND job_type = 'sync' AND status = 'pending' "
        "AND payload ->> 'mode' = 'incremental'",
        (CONNECTION_ID,),
    )
    connection.commit()
    assert sync_runtime.run_once("gmail-database-test")
    assert IndexingRuntime(index_repository, IndexingPipeline()).run_once(
        "gmail-index-test"
    )
    assert connection.execute(
        "SELECT cursor ->> 'history_id' FROM app.connection_cursors "
        "WHERE connection_id = %s AND stream = 'gmail'",
        (CONNECTION_ID,),
    ).fetchone() == ("history-200",)
    assert connection.execute(
        "SELECT external_id, state FROM app.sources WHERE connection_id = %s "
        "ORDER BY external_id",
        (CONNECTION_ID,),
    ).fetchall() == [
        ("gmail-message-1", "active"),
        ("gmail-message-2", "deleted"),
    ]
    assert connection.execute(
        "SELECT version.normalized_text FROM app.sources AS source "
        "JOIN app.document_versions AS version "
        "ON version.id = source.current_document_version_id "
        "WHERE source.connection_id = %s AND source.external_id = 'gmail-message-1'",
        (CONNECTION_ID,),
    ).fetchone() == ("Subject: Gmail integration test updated\n\nNew content.",)
