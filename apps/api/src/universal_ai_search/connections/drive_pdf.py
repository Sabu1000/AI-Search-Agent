"""Bounded PDF text extraction for Google Drive files."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from pypdf import PdfReader
from uas_connector_sdk import NormalizedDocument

from universal_ai_search.connections.drive import DriveItem, normalize_drive_item

MAX_DRIVE_PDF_BYTES = 20 * 1024 * 1024
MAX_DRIVE_PDF_PAGES = 500
MAX_DRIVE_PDF_TEXT_CHARACTERS = 4_900_000
DRIVE_PDF_MIME_TYPE = "application/pdf"

type PdfExtractionStatus = Literal[
    "extracted",
    "empty",
    "encrypted",
    "invalid",
    "too_large",
    "too_many_pages",
    "text_truncated",
]


@dataclass(frozen=True)
class PdfExtraction:
    status: PdfExtractionStatus
    text: str = ""
    page_count: int | None = None


def _normalize_text(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    )
    visible = "".join(
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    return "\n".join(
        line
        for line in (" ".join(line.split()) for line in visible.split("\n"))
        if line
    )


def extract_pdf(data: bytes) -> PdfExtraction:
    """Extract deterministic plain text without allowing an unbounded document."""

    if len(data) > MAX_DRIVE_PDF_BYTES:
        return PdfExtraction("too_large")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            return PdfExtraction("encrypted")
        page_count = len(reader.pages)
        if page_count > MAX_DRIVE_PDF_PAGES:
            return PdfExtraction("too_many_pages", page_count=page_count)
        parts: list[str] = []
        character_count = 0
        for page in reader.pages:
            text = _normalize_text(page.extract_text() or "")
            if not text:
                continue
            separator_length = 2 if parts else 0
            remaining = (
                MAX_DRIVE_PDF_TEXT_CHARACTERS - character_count - separator_length
            )
            if len(text) > remaining:
                if remaining:
                    parts.append(text[:remaining])
                return PdfExtraction("text_truncated", "\n\n".join(parts), page_count)
            parts.append(text)
            character_count += separator_length + len(text)
        extracted = "\n\n".join(parts)
        return PdfExtraction(
            "extracted" if extracted else "empty", extracted, page_count
        )
    # pypdf can surface several parser-specific exceptions for hostile input.
    except Exception:
        return PdfExtraction("invalid")


def normalize_drive_pdf(
    item: DriveItem,
    extraction: PdfExtraction,
    *,
    logical_path: tuple[str, ...],
) -> NormalizedDocument:
    """Attach extracted text and bounded extraction metadata to a Drive source."""

    document = normalize_drive_item(item, logical_path=logical_path)
    metadata = dict(document.provider_metadata)
    metadata["extraction_status"] = extraction.status
    metadata["extracted_character_count"] = len(extraction.text)
    if extraction.page_count is not None:
        metadata["pdf_page_count"] = extraction.page_count
    content = f"{document.content}\nPDF extraction: {extraction.status}"
    if extraction.text:
        content = f"{content}\n\n{extraction.text}"
    return document.model_copy(
        update={"content": content[:5_000_000], "provider_metadata": metadata}
    )
