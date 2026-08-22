from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from universal_ai_search.connections import drive_docx
from universal_ai_search.connections.drive import DriveItem
from universal_ai_search.connections.drive_docx import (
    DRIVE_DOCX_MIME_TYPE,
    DRIVE_GOOGLE_DOC_MIME_TYPE,
    MAX_DRIVE_DOCX_BYTES,
    MAX_DRIVE_DOCX_XML_BYTES,
    DocxExtraction,
    extract_docx,
    normalize_drive_word_document,
)


def docx_bytes(document_xml: str | None = None) -> bytes:
    xml = (
        document_xml
        or """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Launch Plan</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Ship safely</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Owner</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Status</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>Alex</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Ready</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


def drive_item(mime_type: str = DRIVE_DOCX_MIME_TYPE) -> DriveItem:
    return DriveItem(
        id="doc_1",
        name=(
            "Launch plan" if mime_type == DRIVE_GOOGLE_DOC_MIME_TYPE else "Launch.docx"
        ),
        mime_type=mime_type,
        modified_at=datetime(2026, 8, 21, tzinfo=UTC),
        parent_ids=("folder_1",),
        owners=(),
        web_view_link=None,
        size=500,
        drive_id=None,
        shortcut_target_id=None,
        shortcut_target_mime_type=None,
    )


def test_docx_preserves_headings_lists_paragraphs_and_tables() -> None:
    extraction = extract_docx(docx_bytes())
    document = normalize_drive_word_document(
        drive_item(), extraction, logical_path=("Projects",), source_kind="docx"
    )

    assert extraction.status == "extracted"
    assert extraction.paragraph_count == 7
    assert extraction.table_count == 1
    assert extraction.text == (
        "# Launch Plan\n\nFirst paragraph\n\n- Ship safely\n\n"
        "[Table]\n\nOwner | Status\n\nAlex | Ready\n\n[/Table]"
    )
    assert document.provider_metadata["document_source_kind"] == "docx"
    assert document.provider_metadata["extraction_status"] == "extracted"


def test_google_doc_export_keeps_native_identity_and_export_metadata() -> None:
    extraction = extract_docx(docx_bytes())
    document = normalize_drive_word_document(
        drive_item(DRIVE_GOOGLE_DOC_MIME_TYPE),
        extraction,
        logical_path=("My Drive",),
        source_kind="google_docs_export",
    )

    assert document.mime_type == DRIVE_GOOGLE_DOC_MIME_TYPE
    assert document.external_id == "doc_1"
    assert "Google Docs export extraction: extracted" in document.content
    assert document.provider_metadata["document_source_kind"] == "google_docs_export"


def test_docx_safely_classifies_invalid_oversized_and_expanded_archives() -> None:
    assert extract_docx(b"not a docx") == DocxExtraction("invalid")
    assert extract_docx(b"x" * (MAX_DRIVE_DOCX_BYTES + 1)).status == "too_large"

    empty_xml = """<w:document
      xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p/></w:body>
    </w:document>"""
    assert extract_docx(docx_bytes(empty_xml)).status == "empty"
    entity_xml = """<!DOCTYPE x [<!ENTITY secret "blocked">]>
    <w:document
      xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>&secret;</w:t></w:r></w:p></w:body>
    </w:document>"""
    assert extract_docx(docx_bytes(entity_xml)).status == "invalid"

    oversized_xml = "x" * (MAX_DRIVE_DOCX_XML_BYTES + 1)
    assert extract_docx(docx_bytes(oversized_xml)).status == "archive_too_large"


def test_docx_text_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_docx, "MAX_DRIVE_DOCX_TEXT_CHARACTERS", 10)

    extraction = extract_docx(docx_bytes())

    assert extraction.status == "text_truncated"
    assert len(extraction.text) == 10
