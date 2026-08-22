"""Bounded PPTX extraction for native Google Slides exports."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from uas_connector_sdk import NormalizedDocument

from universal_ai_search.connections.drive import DriveItem, normalize_drive_item

DRIVE_GOOGLE_SLIDES_MIME_TYPE = "application/vnd.google-apps.presentation"
DRIVE_PPTX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
MAX_DRIVE_PPTX_BYTES = 20 * 1024 * 1024
MAX_DRIVE_PPTX_MEMBERS = 5_000
MAX_DRIVE_PPTX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_DRIVE_PPTX_XML_BYTES = 10 * 1024 * 1024
MAX_DRIVE_PPTX_TEXT_CHARACTERS = 4_900_000
MAX_DRIVE_PPTX_SLIDES = 1_000
_SLIDE_FILE = re.compile(r"ppt/slides/slide(\d+)\.xml\Z")
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

type SlideExtractionStatus = Literal[
    "extracted",
    "empty",
    "invalid",
    "too_large",
    "archive_too_large",
    "content_limit_exceeded",
    "text_truncated",
]


@dataclass(frozen=True)
class SlideExtraction:
    status: SlideExtractionStatus
    text: str = ""
    slide_count: int = 0
    note_count: int = 0


def _text_nodes(xml: bytes) -> list[str]:
    root = ElementTree.fromstring(xml)
    values: list[str] = []
    for paragraph in root.iter(f"{_DRAWING}p"):
        value = unicodedata.normalize(
            "NFC", "".join(node.text or "" for node in paragraph.iter(f"{_DRAWING}t"))
        )
        value = " ".join(
            "".join(
                character
                for character in value
                if unicodedata.category(character) != "Cc"
            ).split()
        )
        if value:
            values.append(value)
    return values


def extract_pptx(data: bytes) -> SlideExtraction:
    """Extract ordered slide and speaker-note text within fixed limits."""

    if len(data) > MAX_DRIVE_PPTX_BYTES:
        return SlideExtraction("too_large")
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_DRIVE_PPTX_MEMBERS
                or len({member.filename for member in members}) != len(members)
                or sum(member.file_size for member in members)
                > MAX_DRIVE_PPTX_EXPANDED_BYTES
            ):
                return SlideExtraction("archive_too_large")
            slides = sorted(
                (
                    (int(match.group(1)), member)
                    for member in members
                    if (match := _SLIDE_FILE.fullmatch(member.filename))
                ),
                key=lambda value: value[0],
            )
            if len(slides) > MAX_DRIVE_PPTX_SLIDES:
                return SlideExtraction("content_limit_exceeded")
            sections: list[str] = []
            note_count = 0
            for number, member in slides:
                if member.file_size > MAX_DRIVE_PPTX_XML_BYTES:
                    return SlideExtraction("archive_too_large")
                lines = _text_nodes(archive.read(member))
                note_name = f"ppt/notesSlides/notesSlide{number}.xml"
                notes: list[str] = []
                if note_name in archive.namelist():
                    note = archive.getinfo(note_name)
                    if note.file_size > MAX_DRIVE_PPTX_XML_BYTES:
                        return SlideExtraction("archive_too_large")
                    notes = _text_nodes(archive.read(note))
                    if notes:
                        note_count += 1
                body = "\n".join(lines)
                if notes:
                    body = f"{body}\n\nSpeaker notes:\n" + "\n".join(notes)
                if body:
                    sections.append(f"# Slide {number}\n\n{body}")
        text = "\n\n".join(sections)
        if len(text) > MAX_DRIVE_PPTX_TEXT_CHARACTERS:
            return SlideExtraction(
                "text_truncated",
                text[:MAX_DRIVE_PPTX_TEXT_CHARACTERS],
                len(slides),
                note_count,
            )
        return SlideExtraction(
            "extracted" if text else "empty", text, len(slides), note_count
        )
    except (BadZipFile, KeyError, OSError, RuntimeError, ValueError):
        return SlideExtraction("invalid")


def normalize_drive_slides(
    item: DriveItem, extraction: SlideExtraction, *, logical_path: tuple[str, ...]
) -> NormalizedDocument:
    document = normalize_drive_item(item, logical_path=logical_path)
    metadata = dict(document.provider_metadata)
    metadata.update(
        {
            "document_source_kind": "google_slides_export",
            "extraction_status": extraction.status,
            "extracted_character_count": len(extraction.text),
            "slide_count": extraction.slide_count,
            "speaker_note_slide_count": extraction.note_count,
        }
    )
    content = (
        f"{document.content}\nGoogle Slides export extraction: {extraction.status}"
    )
    if extraction.text:
        content = f"{content}\n\n{extraction.text}"
    return document.model_copy(
        update={"content": content[:5_000_000], "provider_metadata": metadata}
    )
