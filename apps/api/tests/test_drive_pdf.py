from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from pypdf import PdfWriter

from universal_ai_search.connections.drive import DriveItem
from universal_ai_search.connections.drive_pdf import (
    MAX_DRIVE_PDF_BYTES,
    MAX_DRIVE_PDF_PAGES,
    PdfExtraction,
    extract_pdf,
    normalize_drive_pdf,
)


def searchable_pdf(text: str = "Searchable PDF content") -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
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
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(value)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def test_pdf_text_is_extracted_and_normalized_into_drive_document() -> None:
    extraction = extract_pdf(searchable_pdf())
    item = DriveItem(
        id="file_1",
        name="Brief.pdf",
        mime_type="application/pdf",
        modified_at=datetime(2026, 8, 20, tzinfo=UTC),
        parent_ids=("folder_1",),
        owners=(),
        web_view_link=None,
        size=500,
        drive_id=None,
        shortcut_target_id=None,
        shortcut_target_mime_type=None,
    )
    document = normalize_drive_pdf(item, extraction, logical_path=("Projects",))

    assert extraction.status == "extracted"
    assert extraction.page_count == 1
    assert "Searchable PDF content" in document.content
    assert document.provider_metadata["extraction_status"] == "extracted"
    assert document.provider_metadata["pdf_page_count"] == 1
    assert document.provider_metadata["extracted_character_count"] == 22


def test_pdf_extraction_safely_classifies_empty_encrypted_and_invalid_files() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    empty = BytesIO()
    writer.write(empty)
    assert extract_pdf(empty.getvalue()) == PdfExtraction("empty", "", 1)

    writer.encrypt("secret")
    encrypted = BytesIO()
    writer.write(encrypted)
    assert extract_pdf(encrypted.getvalue()).status == "encrypted"
    assert extract_pdf(b"not a pdf") == PdfExtraction("invalid")
    assert extract_pdf(b"x" * (MAX_DRIVE_PDF_BYTES + 1)).status == "too_large"


def test_pdf_extraction_rejects_excessive_page_counts() -> None:
    writer = PdfWriter()
    for _ in range(MAX_DRIVE_PDF_PAGES + 1):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)

    extraction = extract_pdf(output.getvalue())

    assert extraction.status == "too_many_pages"
    assert extraction.page_count == MAX_DRIVE_PDF_PAGES + 1
