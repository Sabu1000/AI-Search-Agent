"""Bounded DOCX extraction for uploaded Word files and native Google Docs."""

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

DRIVE_DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
DRIVE_GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
MAX_DRIVE_DOCX_BYTES = 20 * 1024 * 1024
MAX_DRIVE_DOCX_MEMBERS = 2_000
MAX_DRIVE_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_DRIVE_DOCX_XML_BYTES = 10 * 1024 * 1024
MAX_DRIVE_DOCX_TEXT_CHARACTERS = 4_900_000
_DOCUMENT_XML = "word/document.xml"
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NAMESPACE}}}"
_HEADING_STYLE = re.compile(r"heading\s*([1-6])", re.IGNORECASE)

type DocxExtractionStatus = Literal[
    "extracted",
    "empty",
    "invalid",
    "too_large",
    "archive_too_large",
    "text_truncated",
]
type WordSourceKind = Literal["docx", "google_docs_export"]


@dataclass(frozen=True)
class DocxExtraction:
    status: DocxExtractionStatus
    text: str = ""
    paragraph_count: int = 0
    table_count: int = 0


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    visible = "".join(
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    return " ".join(visible.split())


def _paragraph_text(paragraph: Element) -> str:
    return _normalize_text(
        "".join(node.text or "" for node in paragraph.iter(f"{_W}t"))
    )


def _paragraph_line(paragraph: Element) -> str:
    text = _paragraph_text(paragraph)
    if not text:
        return ""
    style = paragraph.find(f"./{_W}pPr/{_W}pStyle")
    style_name = style.get(f"{_W}val", "") if style is not None else ""
    heading = _HEADING_STYLE.fullmatch(style_name)
    if heading:
        return f"{'#' * int(heading.group(1))} {text}"
    if paragraph.find(f"./{_W}pPr/{_W}numPr") is not None:
        return f"- {text}"
    return text


def extract_docx(data: bytes) -> DocxExtraction:
    """Extract ordered WordprocessingML text within fixed archive/XML limits."""

    if len(data) > MAX_DRIVE_DOCX_BYTES:
        return DocxExtraction("too_large")
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_DRIVE_DOCX_MEMBERS
                or sum(member.file_size for member in members)
                > MAX_DRIVE_DOCX_EXPANDED_BYTES
            ):
                return DocxExtraction("archive_too_large")
            document = archive.getinfo(_DOCUMENT_XML)
            if document.file_size > MAX_DRIVE_DOCX_XML_BYTES:
                return DocxExtraction("archive_too_large")
            xml = archive.read(document)
        root = ElementTree.fromstring(xml)
        body = root.find(f"./{_W}body")
        if body is None:
            return DocxExtraction("invalid")
        lines: list[str] = []
        paragraph_count = 0
        table_count = 0
        for child in body:
            if child.tag == f"{_W}p":
                paragraph_count += 1
                line = _paragraph_line(child)
                if line:
                    lines.append(line)
            elif child.tag == f"{_W}tbl":
                table_count += 1
                rows: list[str] = []
                for row in child.findall(f"./{_W}tr"):
                    cells: list[str] = []
                    for cell in row.findall(f"./{_W}tc"):
                        paragraphs = cell.findall(f"./{_W}p")
                        paragraph_count += len(paragraphs)
                        cells.append(
                            " ".join(
                                text
                                for text in map(_paragraph_text, paragraphs)
                                if text
                            )
                        )
                    rows.append(" | ".join(cells))
                if rows:
                    lines.extend(("[Table]", *rows, "[/Table]"))
        text = "\n\n".join(lines)
        if len(text) > MAX_DRIVE_DOCX_TEXT_CHARACTERS:
            return DocxExtraction(
                "text_truncated",
                text[:MAX_DRIVE_DOCX_TEXT_CHARACTERS],
                paragraph_count,
                table_count,
            )
        return DocxExtraction(
            "extracted" if text else "empty",
            text,
            paragraph_count,
            table_count,
        )
    # ZIP and XML libraries surface several exceptions for malformed input.
    except (BadZipFile, KeyError, OSError, RuntimeError, ValueError):
        return DocxExtraction("invalid")


def normalize_drive_word_document(
    item: DriveItem,
    extraction: DocxExtraction,
    *,
    logical_path: tuple[str, ...],
    source_kind: WordSourceKind,
) -> NormalizedDocument:
    """Attach extracted Word text and bounded parser metadata to a Drive source."""

    document = normalize_drive_item(item, logical_path=logical_path)
    metadata = dict(document.provider_metadata)
    metadata.update(
        {
            "document_source_kind": source_kind,
            "extraction_status": extraction.status,
            "extracted_character_count": len(extraction.text),
            "paragraph_count": extraction.paragraph_count,
            "table_count": extraction.table_count,
        }
    )
    label = "Google Docs export" if source_kind == "google_docs_export" else "DOCX"
    content = f"{document.content}\n{label} extraction: {extraction.status}"
    if extraction.text:
        content = f"{content}\n\n{extraction.text}"
    return document.model_copy(
        update={"content": content[:5_000_000], "provider_metadata": metadata}
    )
