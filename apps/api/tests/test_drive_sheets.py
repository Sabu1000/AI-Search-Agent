from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from universal_ai_search.connections import drive_sheets
from universal_ai_search.connections.drive import DriveItem
from universal_ai_search.connections.drive_sheets import (
    DRIVE_GOOGLE_SHEET_MIME_TYPE,
    MAX_DRIVE_XLSX_BYTES,
    MAX_DRIVE_XLSX_XML_BYTES,
    SheetExtraction,
    extract_xlsx,
    normalize_drive_sheet,
)


def xlsx_bytes(sheet_xml: str | None = None) -> bytes:
    sheet = (
        sheet_xml
        or """<worksheet
      xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
      <row><c t="s"><v>0</v></c><c t="inlineStr"><is><t>Status</t></is></c></row>
      <row><c t="str"><v>Launch</v></c><c t="b"><v>1</v></c>
        <c><f>1+1</f><v>2</v></c></row>
    </sheetData></worksheet>"""
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<workbook
              xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Roadmap" sheetId="1" r:id="rId1"/></sheets>
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
            "xl/sharedStrings.xml",
            """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Project</t></si></sst>""",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def item() -> DriveItem:
    return DriveItem(
        id="sheet_1",
        name="Launch tracker",
        mime_type=DRIVE_GOOGLE_SHEET_MIME_TYPE,
        modified_at=datetime(2026, 8, 21, tzinfo=UTC),
        parent_ids=("root",),
        owners=(),
        web_view_link=None,
        size=None,
        drive_id=None,
        shortcut_target_id=None,
        shortcut_target_mime_type=None,
    )


def test_xlsx_extracts_named_sheets_rows_cells_formulas_and_types() -> None:
    extraction = extract_xlsx(xlsx_bytes())
    document = normalize_drive_sheet(item(), extraction, logical_path=("My Drive",))

    assert extraction == SheetExtraction(
        "extracted",
        "# Sheet: Roadmap\n\nProject | Status\nLaunch | TRUE | =1+1 (2)",
        1,
        2,
        5,
    )
    assert document.mime_type == DRIVE_GOOGLE_SHEET_MIME_TYPE
    assert document.provider_metadata["document_source_kind"] == (
        "google_sheets_export"
    )


def test_xlsx_safely_classifies_empty_invalid_and_oversized_content() -> None:
    assert extract_xlsx(b"invalid") == SheetExtraction("invalid")
    assert extract_xlsx(b"x" * (MAX_DRIVE_XLSX_BYTES + 1)).status == "too_large"
    assert (
        extract_xlsx(
            xlsx_bytes(
                """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>"""
            )
        ).status
        == "empty"
    )
    assert extract_xlsx(xlsx_bytes("x" * (MAX_DRIVE_XLSX_XML_BYTES + 1))).status == (
        "archive_too_large"
    )


def test_xlsx_rejects_entities_and_invalid_shared_string_references() -> None:
    entity = """<!DOCTYPE x [<!ENTITY value "blocked">]>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row><c t="inlineStr"><is><t>&value;</t></is></c></row></sheetData>
    </worksheet>"""
    bad_shared = """<worksheet
      xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row><c t="s"><v>999</v></c></row></sheetData>
    </worksheet>"""
    assert extract_xlsx(xlsx_bytes(entity)).status == "invalid"
    assert extract_xlsx(xlsx_bytes(bad_shared)).status == "invalid"


def test_xlsx_text_and_cell_counts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_sheets, "MAX_DRIVE_XLSX_TEXT_CHARACTERS", 10)
    assert extract_xlsx(xlsx_bytes()).status == "text_truncated"
    monkeypatch.setattr(drive_sheets, "MAX_DRIVE_XLSX_CELLS", 1)
    assert extract_xlsx(xlsx_bytes()).status == "content_limit_exceeded"
