from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from universal_ai_search.connections import drive_slides
from universal_ai_search.connections.drive import DriveItem
from universal_ai_search.connections.drive_slides import (
    DRIVE_GOOGLE_SLIDES_MIME_TYPE,
    MAX_DRIVE_PPTX_BYTES,
    MAX_DRIVE_PPTX_XML_BYTES,
    SlideExtraction,
    extract_pptx,
    normalize_drive_slides,
)


def pptx_bytes(slide_xml: str | None = None, *, include_notes: bool = True) -> bytes:
    slide = (
        slide_xml
        or """<p:sld
          xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <p:cSld><a:p><a:r><a:t>Launch Review</a:t></a:r></a:p>
          <a:p><a:r><a:t>Ship safely</a:t></a:r></a:p></p:cSld>
        </p:sld>"""
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
        if include_notes:
            archive.writestr(
                "ppt/notesSlides/notesSlide1.xml",
                """<p:notes
                  xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>Mention the August deadline</a:t></a:r></a:p>
                </p:notes>""",
            )
    return output.getvalue()


def item() -> DriveItem:
    return DriveItem(
        id="slides_1",
        name="Launch review",
        mime_type=DRIVE_GOOGLE_SLIDES_MIME_TYPE,
        modified_at=datetime(2026, 8, 21, tzinfo=UTC),
        parent_ids=("root",),
        owners=(),
        web_view_link=None,
        size=None,
        drive_id=None,
        shortcut_target_id=None,
        shortcut_target_mime_type=None,
    )


def test_pptx_extracts_ordered_slide_and_speaker_note_text() -> None:
    extraction = extract_pptx(pptx_bytes())
    document = normalize_drive_slides(item(), extraction, logical_path=("My Drive",))

    assert extraction == SlideExtraction(
        "extracted",
        "# Slide 1\n\nLaunch Review\nShip safely\n\nSpeaker notes:\n"
        "Mention the August deadline",
        1,
        1,
    )
    assert document.mime_type == DRIVE_GOOGLE_SLIDES_MIME_TYPE
    assert document.provider_metadata["document_source_kind"] == (
        "google_slides_export"
    )


def test_pptx_safely_classifies_empty_invalid_and_oversized_content() -> None:
    empty = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>"""
    assert extract_pptx(b"invalid") == SlideExtraction("invalid")
    assert extract_pptx(b"x" * (MAX_DRIVE_PPTX_BYTES + 1)).status == "too_large"
    assert extract_pptx(pptx_bytes(empty, include_notes=False)).status == "empty"
    assert extract_pptx(pptx_bytes("x" * (MAX_DRIVE_PPTX_XML_BYTES + 1))).status == (
        "archive_too_large"
    )


def test_pptx_rejects_xml_entities() -> None:
    entity = """<!DOCTYPE x [<!ENTITY value "blocked">]>
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <a:p><a:r><a:t>&value;</a:t></a:r></a:p>
    </p:sld>"""
    assert extract_pptx(pptx_bytes(entity)).status == "invalid"


def test_pptx_text_and_slide_counts_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drive_slides, "MAX_DRIVE_PPTX_TEXT_CHARACTERS", 10)
    assert extract_pptx(pptx_bytes()).status == "text_truncated"
    monkeypatch.setattr(drive_slides, "MAX_DRIVE_PPTX_SLIDES", 0)
    assert extract_pptx(pptx_bytes()).status == "content_limit_exceeded"
