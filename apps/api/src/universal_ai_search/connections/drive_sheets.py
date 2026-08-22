"""Bounded XLSX extraction for native Google Sheets exports."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from xml.etree.ElementTree import Element
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from uas_connector_sdk import NormalizedDocument

from universal_ai_search.connections.drive import DriveItem, normalize_drive_item

DRIVE_GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
DRIVE_XLSX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
MAX_DRIVE_XLSX_BYTES = 20 * 1024 * 1024
MAX_DRIVE_XLSX_MEMBERS = 5_000
MAX_DRIVE_XLSX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_DRIVE_XLSX_XML_BYTES = 10 * 1024 * 1024
MAX_DRIVE_XLSX_TEXT_CHARACTERS = 4_900_000
MAX_DRIVE_XLSX_SHEETS = 200
MAX_DRIVE_XLSX_CELLS = 250_000
_SHEET_FILE = re.compile(r"xl/worksheets/sheet(\d+)\.xml\Z")
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_S = f"{{{_MAIN}}}"

type SheetExtractionStatus = Literal[
    "extracted",
    "empty",
    "invalid",
    "too_large",
    "archive_too_large",
    "content_limit_exceeded",
    "text_truncated",
]


@dataclass(frozen=True)
class SheetExtraction:
    status: SheetExtractionStatus
    text: str = ""
    sheet_count: int = 0
    row_count: int = 0
    cell_count: int = 0


class _ArchiveTooLargeError(Exception):
    pass


class _ContentLimitError(Exception):
    pass


def _text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    visible = "".join(
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    return " ".join(visible.split())


def _xml(archive: ZipFile, name: str) -> Element:
    member = archive.getinfo(name)
    if member.file_size > MAX_DRIVE_XLSX_XML_BYTES:
        raise _ArchiveTooLargeError
    return ElementTree.fromstring(archive.read(member))


def _shared_strings(archive: ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = _xml(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall(f"./{_S}si"):
        values.append(_text("".join(node.text or "" for node in item.iter(f"{_S}t"))))
        if len(values) > MAX_DRIVE_XLSX_CELLS:
            raise _ContentLimitError
    return tuple(values)


def _sheet_names(archive: ZipFile) -> dict[str, str]:
    if (
        "xl/workbook.xml" not in archive.namelist()
        or "xl/_rels/workbook.xml.rels" not in archive.namelist()
    ):
        return {}
    workbook = _xml(archive, "xl/workbook.xml")
    relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {
        relation.get("Id", ""): relation.get("Target", "")
        for relation in relationships.findall(f"./{{{_PACKAGE_REL}}}Relationship")
    }
    result: dict[str, str] = {}
    for sheet in workbook.findall(f"./{_S}sheets/{_S}sheet"):
        relation_id = sheet.get(f"{{{_REL}}}id", "")
        target = targets.get(relation_id, "").lstrip("/")
        if target.startswith("worksheets/"):
            target = f"xl/{target}"
        elif target.startswith("xl/worksheets/"):
            pass
        else:
            continue
        name = _text(sheet.get("name", ""))
        if name:
            result[target] = name
    return result


def _cell_value(cell: Element, shared: tuple[str, ...]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return _text("".join(node.text or "" for node in cell.iter(f"{_S}t")))
    value = cell.findtext(f"./{_S}v", default="")
    if kind == "s":
        try:
            return shared[int(value)]
        except (IndexError, ValueError) as error:
            raise ValueError("invalid shared string") from error
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE" if value == "0" else ""
    formula = _text(cell.findtext(f"./{_S}f", default=""))
    rendered = _text(value)
    if formula:
        return f"={formula}" + (f" ({rendered})" if rendered else "")
    return rendered


def extract_xlsx(data: bytes) -> SheetExtraction:
    """Extract sheet names and non-empty cell rows within fixed limits."""

    if len(data) > MAX_DRIVE_XLSX_BYTES:
        return SheetExtraction("too_large")
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_DRIVE_XLSX_MEMBERS
                or len({member.filename for member in members}) != len(members)
                or sum(member.file_size for member in members)
                > MAX_DRIVE_XLSX_EXPANDED_BYTES
            ):
                return SheetExtraction("archive_too_large")
            sheet_files = sorted(
                (name for name in archive.namelist() if _SHEET_FILE.fullmatch(name)),
                key=lambda name: int(_SHEET_FILE.fullmatch(name).group(1)),  # type: ignore[union-attr]
            )
            if len(sheet_files) > MAX_DRIVE_XLSX_SHEETS:
                return SheetExtraction("content_limit_exceeded")
            shared = _shared_strings(archive)
            names = _sheet_names(archive)
            sections: list[str] = []
            row_count = 0
            cell_count = 0
            for index, filename in enumerate(sheet_files, 1):
                root = _xml(archive, filename)
                rows: list[str] = []
                for row in root.findall(f"./{_S}sheetData/{_S}row"):
                    cells = [
                        _cell_value(cell, shared) for cell in row.findall(f"./{_S}c")
                    ]
                    cell_count += len(cells)
                    if cell_count > MAX_DRIVE_XLSX_CELLS:
                        return SheetExtraction(
                            "content_limit_exceeded", "", index, row_count, cell_count
                        )
                    if any(cells):
                        rows.append(" | ".join(cells))
                        row_count += 1
                if rows:
                    sections.append(
                        f"# Sheet: {names.get(filename, f'Sheet {index}')}\n\n"
                        + "\n".join(rows)
                    )
        text = "\n\n".join(sections)
        if len(text) > MAX_DRIVE_XLSX_TEXT_CHARACTERS:
            return SheetExtraction(
                "text_truncated",
                text[:MAX_DRIVE_XLSX_TEXT_CHARACTERS],
                len(sheet_files),
                row_count,
                cell_count,
            )
        return SheetExtraction(
            "extracted" if text else "empty",
            text,
            len(sheet_files),
            row_count,
            cell_count,
        )
    except _ArchiveTooLargeError:
        return SheetExtraction("archive_too_large")
    except _ContentLimitError:
        return SheetExtraction("content_limit_exceeded")
    except (
        BadZipFile,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return SheetExtraction("invalid")


def normalize_drive_sheet(
    item: DriveItem, extraction: SheetExtraction, *, logical_path: tuple[str, ...]
) -> NormalizedDocument:
    document = normalize_drive_item(item, logical_path=logical_path)
    metadata = dict(document.provider_metadata)
    metadata.update(
        {
            "document_source_kind": "google_sheets_export",
            "extraction_status": extraction.status,
            "extracted_character_count": len(extraction.text),
            "sheet_count": extraction.sheet_count,
            "row_count": extraction.row_count,
            "cell_count": extraction.cell_count,
        }
    )
    content = (
        f"{document.content}\nGoogle Sheets export extraction: {extraction.status}"
    )
    if extraction.text:
        content = f"{content}\n\n{extraction.text}"
    return document.model_copy(
        update={"content": content[:5_000_000], "provider_metadata": metadata}
    )
