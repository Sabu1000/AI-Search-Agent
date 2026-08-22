from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from conftest import database_dsn

from universal_ai_search.connections.crypto import (
    LocalEnvelopeEncryption,
    envelope_context,
)
from universal_ai_search.connections.drive import (
    DRIVE_FOLDER_MIME_TYPE,
    DriveItem,
    DrivePage,
)
from universal_ai_search.connections.google import DRIVE_READONLY_SCOPE
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.sync.drive_repository import DriveSyncRepository
from universal_ai_search.sync.drive_runtime import DriveSyncRuntime

WORKSPACE_ID = UUID("91000000-0000-4000-8000-000000000001")
USER_ID = UUID("92000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("93000000-0000-4000-8000-000000000001")
JOB_ID = UUID("94000000-0000-4000-8000-000000000001")


def searchable_pdf() -> bytes:
    stream = b"BT /F1 12 Tf 72 720 Td (Quarterly launch plan) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(value)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def drive_item(
    item_id: str,
    name: str,
    mime_type: str,
    parent_id: str,
) -> DriveItem:
    return DriveItem(
        id=item_id,
        name=name,
        mime_type=mime_type,
        modified_at=datetime(2026, 8, 20, tzinfo=UTC),
        parent_ids=(parent_id,),
        owners=(),
        web_view_link=None,
        size=42,
        drive_id=None,
        shortcut_target_id=None,
        shortcut_target_mime_type=None,
    )


class FakeDriveClient:
    async def ensure_fresh(self, credentials: object) -> object:
        return credentials

    async def children_page(self, **values: object) -> DrivePage:
        assert values["access_token"] == "synthetic-access"
        folder_id = values["folder_id"]
        if folder_id == "root":
            return DrivePage(
                (
                    drive_item("folder_1", "Projects", DRIVE_FOLDER_MIME_TYPE, "root"),
                    drive_item("file_1", "Overview.pdf", "application/pdf", "root"),
                ),
                None,
            )
        assert folder_id == "folder_1"
        return DrivePage(
            (drive_item("file_2", "Plan.txt", "text/plain", "folder_1"),),
            None,
        )

    async def download_file(self, **values: object) -> bytes:
        assert values == {"access_token": "synthetic-access", "file_id": "file_1"}
        return searchable_pdf()


def test_drive_folder_tree_queues_and_indexes_durably(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    encryption = LocalEnvelopeEncryption(b"e" * 32)
    credential_context = envelope_context(
        provider="google",
        workspace_id=str(WORKSPACE_ID),
        record_id=str(CONNECTION_ID),
        purpose="provider-credential",
    )
    envelope = encryption.encrypt(
        json.dumps(
            {
                "access_token": "synthetic-access",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "refresh_token": "synthetic-refresh",
                "schema_version": 1,
                "scopes": [DRIVE_READONLY_SCOPE],
            }
        ).encode(),
        context=credential_context,
    )
    connection.execute("DELETE FROM app.workspaces")
    connection.execute("DELETE FROM app.users")
    connection.execute(
        "INSERT INTO app.users (id, email, full_name, status) "
        "VALUES (%s, 'drive-sync@example.test', 'Drive Sync', 'active')",
        (USER_ID,),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, 'Drive Sync', 'free', 'active')",
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
        ) VALUES (%s, %s, %s, 'google', 'drive-sync@example.test', 'active',
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
        (WORKSPACE_ID, CONNECTION_ID, DRIVE_READONLY_SCOPE),
    )
    connection.execute(
        "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)",
        (WORKSPACE_ID,),
    )
    connection.execute(
        """INSERT INTO app.jobs (
            id, workspace_id, connection_id, job_type, queue,
            idempotency_key, status, payload
        ) VALUES (%s, %s, %s, 'sync', 'sync', 'drive-e2e', 'pending',
            '{"mode":"full","source_families":["google_drive"]}'::JSONB)""",
        (JOB_ID, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.commit()

    index_repository = IndexRepository(database_dsn())
    runtime = DriveSyncRuntime(
        repository=DriveSyncRepository(database_dsn()),
        index_repository=index_repository,
        client=FakeDriveClient(),  # type: ignore[arg-type]
        encryption=encryption,
    )
    assert runtime.run_once("drive-database-test")
    assert connection.execute(
        "SELECT status FROM app.jobs WHERE id = %s", (JOB_ID,)
    ).fetchone() == ("completed",)
    pending = connection.execute(
        "SELECT payload::TEXT FROM app.jobs WHERE connection_id = %s "
        "AND job_type = 'sync' AND status = 'pending'",
        (CONNECTION_ID,),
    ).fetchone()
    assert pending is not None
    payload_text = pending[0]
    assert isinstance(payload_text, str)
    assert "drive_progress" in payload_text
    assert "folder_1" not in payload_text
    assert connection.execute(
        "SELECT last_successful_sync_at IS NULL FROM app.connections WHERE id = %s",
        (CONNECTION_ID,),
    ).fetchone() == (True,)

    assert runtime.run_once("drive-database-test")
    assert connection.execute(
        "SELECT last_successful_sync_at IS NOT NULL FROM app.connections WHERE id = %s",
        (CONNECTION_ID,),
    ).fetchone() == (True,)
    assert connection.execute(
        "SELECT count(*) FROM app.outbox_events WHERE aggregate_id = %s "
        "AND event_type = 'connection.drive.full_sync_completed'",
        (CONNECTION_ID,),
    ).fetchone() == (1,)

    index_runtime = IndexingRuntime(index_repository, IndexingPipeline())
    assert index_runtime.run_once("drive-index-test")
    assert index_runtime.run_once("drive-index-test")
    assert (
        connection.execute(
            """SELECT source.external_id, source.provider,
            source.metadata ->> 'logical_path', version.state
        FROM app.sources AS source
        JOIN app.document_versions AS version
          ON version.id = source.current_document_version_id
        WHERE source.connection_id = %s ORDER BY source.external_id""",
            (CONNECTION_ID,),
        ).fetchall()
        == [
            ("file_1", "google_drive", "My Drive/Overview.pdf", "ready"),
            ("file_2", "google_drive", "My Drive/Projects/Plan.txt", "ready"),
        ]
    )
    pdf = connection.execute(
        """SELECT source.metadata ->> 'extraction_status', version.normalized_text
        FROM app.sources AS source
        JOIN app.document_versions AS version
          ON version.id = source.current_document_version_id
        WHERE source.connection_id = %s AND source.external_id = 'file_1'""",
        (CONNECTION_ID,),
    ).fetchone()
    assert pdf is not None
    assert pdf[0] == "extracted"
    assert "Quarterly launch plan" in str(pdf[1])
