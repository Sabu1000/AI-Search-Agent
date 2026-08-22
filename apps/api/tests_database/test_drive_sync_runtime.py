from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

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
from universal_ai_search.connections.drive_docx import (
    DRIVE_DOCX_MIME_TYPE,
    DRIVE_GOOGLE_DOC_MIME_TYPE,
)
from universal_ai_search.connections.drive_sheets import (
    DRIVE_GOOGLE_SHEET_MIME_TYPE,
    DRIVE_XLSX_MIME_TYPE,
)
from universal_ai_search.connections.drive_slides import (
    DRIVE_GOOGLE_SLIDES_MIME_TYPE,
    DRIVE_PPTX_MIME_TYPE,
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
SECOND_JOB_ID = UUID("94000000-0000-4000-8000-000000000002")


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


def searchable_docx(text: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>{text}</w:t></w:r>
    </w:p>
    <w:tbl><w:tr>
      <w:tc><w:p><w:r><w:t>Owner</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Ready</w:t></w:r></w:p></w:tc>
    </w:tr></w:tbl>
  </w:body>
</w:document>"""
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


def searchable_xlsx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<workbook
              xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Launch" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships
              xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet
              xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row><c t="inlineStr"><is><t>Project</t></is></c>
              <c t="inlineStr"><is><t>Orion rollout</t></is></c></row></sheetData>
            </worksheet>""",
        )
    return output.getvalue()


def searchable_pptx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld
              xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p><a:r><a:t>Constellation launch review</a:t></a:r></a:p>
            </p:sld>""",
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            """<p:notes
              xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p><a:r><a:t>Confirm launch owner</a:t></a:r></a:p>
            </p:notes>""",
        )
    return output.getvalue()


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
    def __init__(self) -> None:
        self.removed_ids: set[str] = set()

    async def ensure_fresh(self, credentials: object) -> object:
        return credentials

    async def children_page(self, **values: object) -> DrivePage:
        assert values["access_token"] == "synthetic-access"
        folder_id = values["folder_id"]
        if folder_id == "root":
            root_items = (
                drive_item("folder_1", "Projects", DRIVE_FOLDER_MIME_TYPE, "root"),
                drive_item("file_1", "Overview.pdf", "application/pdf", "root"),
                drive_item(
                    "gdoc_1",
                    "Native roadmap",
                    DRIVE_GOOGLE_DOC_MIME_TYPE,
                    "root",
                ),
                drive_item(
                    "sheet_1",
                    "Launch tracker",
                    DRIVE_GOOGLE_SHEET_MIME_TYPE,
                    "root",
                ),
                drive_item(
                    "slides_1",
                    "Launch review",
                    DRIVE_GOOGLE_SLIDES_MIME_TYPE,
                    "root",
                ),
            )
            return DrivePage(
                tuple(item for item in root_items if item.id not in self.removed_ids),
                None,
            )
        assert folder_id == "folder_1"
        folder_items = (
            drive_item("file_2", "Plan.docx", DRIVE_DOCX_MIME_TYPE, "folder_1"),
        )
        return DrivePage(
            tuple(item for item in folder_items if item.id not in self.removed_ids),
            None,
        )

    async def download_file(self, **values: object) -> bytes:
        assert values["access_token"] == "synthetic-access"
        if values["file_id"] == "file_1":
            return searchable_pdf()
        assert values["file_id"] == "file_2"
        return searchable_docx("Uploaded Word launch plan")

    async def export_file(self, **values: object) -> bytes:
        assert values["access_token"] == "synthetic-access"
        exports = {
            ("gdoc_1", DRIVE_DOCX_MIME_TYPE): searchable_docx("Native Google roadmap"),
            ("sheet_1", DRIVE_XLSX_MIME_TYPE): searchable_xlsx(),
            ("slides_1", DRIVE_PPTX_MIME_TYPE): searchable_pptx(),
        }
        return exports[(str(values["file_id"]), str(values["mime_type"]))]


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
    client = FakeDriveClient()
    runtime = DriveSyncRuntime(
        repository=DriveSyncRepository(database_dsn()),
        index_repository=index_repository,
        client=client,  # type: ignore[arg-type]
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
    assert index_runtime.run_once("drive-index-test")
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
            ("file_2", "google_drive", "My Drive/Projects/Plan.docx", "ready"),
            ("gdoc_1", "google_drive", "My Drive/Native roadmap", "ready"),
            ("sheet_1", "google_drive", "My Drive/Launch tracker", "ready"),
            ("slides_1", "google_drive", "My Drive/Launch review", "ready"),
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
    word_documents = connection.execute(
        """SELECT source.external_id,
            source.metadata ->> 'document_source_kind', version.normalized_text
        FROM app.sources AS source
        JOIN app.document_versions AS version
          ON version.id = source.current_document_version_id
        WHERE source.connection_id = %s AND source.external_id IN ('file_2', 'gdoc_1')
        ORDER BY source.external_id""",
        (CONNECTION_ID,),
    ).fetchall()
    assert word_documents[0][0:2] == ("file_2", "docx")
    assert "Uploaded Word launch plan" in str(word_documents[0][2])
    assert word_documents[1][0:2] == ("gdoc_1", "google_docs_export")
    assert "Native Google roadmap" in str(word_documents[1][2])
    native_exports = connection.execute(
        """SELECT source.external_id,
            source.metadata ->> 'document_source_kind', version.normalized_text
        FROM app.sources AS source
        JOIN app.document_versions AS version
          ON version.id = source.current_document_version_id
        WHERE source.connection_id = %s
          AND source.external_id IN ('sheet_1', 'slides_1')
        ORDER BY source.external_id""",
        (CONNECTION_ID,),
    ).fetchall()
    assert native_exports[0][0:2] == ("sheet_1", "google_sheets_export")
    assert "Orion rollout" in str(native_exports[0][2])
    assert native_exports[1][0:2] == ("slides_1", "google_slides_export")
    assert "Constellation launch review" in str(native_exports[1][2])
    assert "Confirm launch owner" in str(native_exports[1][2])

    client.removed_ids.add("gdoc_1")
    connection.execute(
        """INSERT INTO app.jobs (
            id, workspace_id, connection_id, job_type, queue,
            idempotency_key, status, payload
        ) VALUES (%s, %s, %s, 'sync', 'sync', 'drive-delete-e2e', 'pending',
            '{"mode":"full","source_families":["google_drive"]}'::JSONB)""",
        (SECOND_JOB_ID, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.commit()
    assert runtime.run_once("drive-database-test")
    assert runtime.run_once("drive-database-test")
    assert runtime.run_once("drive-database-test")
    assert (
        connection.execute(
            """SELECT state, title, current_document_version_id, metadata
        FROM app.sources WHERE connection_id = %s AND external_id = 'gdoc_1'""",
            (CONNECTION_ID,),
        ).fetchone()
        == ("deleted", "Deleted source", None, {})
    )
    assert (
        connection.execute(
            """SELECT count(*) FROM app.document_versions AS version
        JOIN app.sources AS source ON source.id = version.source_id
        WHERE source.connection_id = %s AND source.external_id = 'gdoc_1'""",
            (CONNECTION_ID,),
        ).fetchone()
        == (0,)
    )
